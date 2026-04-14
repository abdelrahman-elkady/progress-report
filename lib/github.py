"""GitHub data fetching via the `gh` CLI.

Provides:
  - current user lookup
  - PR search (authored or reviewed)
  - per-PR detail + file enrichment with concurrent fetching and a persistent cache
"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from .jira import extract_jira_ids
from .utils import now_utc, parse_iso

PR_DETAIL_WORKERS = 8

# `kind` field on enriched PR rows. Used in report.py and by downstream consumers.
PR_KIND_AUTHORED = "authored"
PR_KIND_REVIEW = "review"


# ----- gh CLI primitives -----------------------------------------------------

def gh_current_user() -> str:
    """Return the currently-authenticated gh login, or '' on failure."""
    try:
        return subprocess.check_output(
            ["gh", "api", "user", "--jq", ".login"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _gh_search_prs(filter_flag: str, user: str, since_iso_date: str) -> list[dict]:
    """Run `gh search prs` with one of --author/--reviewed-by and return raw results."""
    try:
        out = subprocess.check_output(
            [
                "gh", "search", "prs",
                f"--{filter_flag}={user}",
                f"--merged-at=>={since_iso_date}",
                "--json", "repository,number,title,url,closedAt,updatedAt,createdAt,state,author",
                "--limit", "100",
            ],
            text=True,
            stderr=subprocess.STDOUT,
        )
        return json.loads(out)
    except subprocess.CalledProcessError as e:
        print(f"[warn] gh search prs --{filter_flag} failed: {e.output}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"[warn] gh search prs --{filter_flag} crashed: {e}", file=sys.stderr)
        return []


def search_authored_prs(user: str, since_date: str) -> list[dict]:
    return _gh_search_prs("author", user, since_date)


def search_reviewed_prs(user: str, since_date: str) -> list[dict]:
    """Note: gh's search filter is `reviewed-by`, but it returns PRs the user reviewed."""
    return _gh_search_prs("reviewed-by", user, since_date)


# ----- Per-PR detail with cache + concurrency -------------------------------

def _fetch_one_pr(repo_full: str, number: int) -> dict | None:
    """Fetch PR detail + file list via two `gh api` calls. Returns None if the
    detail call fails (PR is then skipped). File-list failures are warned and
    leave `files=[]` — file-overlap correlation will be weaker for that PR.
    """
    try:
        out = subprocess.check_output(
            [
                "gh", "api", f"repos/{repo_full}/pulls/{number}",
                "--jq",
                "{merged_at, base: .base.ref, head: .head.ref, title, html_url, "
                "user: .user.login, additions, deletions}",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        data = json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None

    files: list[str] = []
    try:
        files_out = subprocess.check_output(
            ["gh", "api", f"repos/{repo_full}/pulls/{number}/files", "--jq", "[.[].filename]"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        files = json.loads(files_out)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        print(f"[warn] {repo_full}#{number}: file list fetch failed ({e}); correlation will be weaker", file=sys.stderr)
    data["files"] = files
    return data


def fetch_pr_details_concurrent(
    raw_prs: list[dict],
    cache_path: Path,
) -> dict[str, dict]:
    """Fetch PR details for many PRs in parallel, using a persistent cache.

    Cache key: f"{repo}#{number}". Cache entries are dicts containing the
    fetched detail blob plus a `_cached_at` timestamp.

    Returns a dict mapping `f"{repo}#{number}"` -> details (with .files).
    """
    cache: dict[str, dict] = {}
    try:
        cache = json.loads(cache_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    results: dict[str, dict] = {}
    todo: list[tuple[str, str, int]] = []
    for pr in raw_prs:
        repo_full = pr["repository"].get("nameWithOwner") or pr["repository"].get("fullName")
        if not repo_full:
            continue
        key = f"{repo_full}#{pr['number']}"
        if key in cache:
            results[key] = cache[key]
        else:
            todo.append((key, repo_full, pr["number"]))

    if todo:
        with ThreadPoolExecutor(max_workers=PR_DETAIL_WORKERS) as ex:
            future_to_key = {
                ex.submit(_fetch_one_pr, repo_full, number): key
                for key, repo_full, number in todo
            }
            for fut in as_completed(future_to_key):
                key = future_to_key[fut]
                try:
                    detail = fut.result()
                except Exception:
                    detail = None
                if detail is None:
                    continue
                detail["_cached_at"] = now_utc().isoformat()
                results[key] = detail
                cache[key] = detail

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=2))
        except OSError as e:
            print(f"[warn] failed to write PR cache: {e}", file=sys.stderr)

    return results


# ----- Enrichment ------------------------------------------------------------

def enrich_prs(
    raw_prs: list[dict],
    target_branches: set[str],
    window_start: datetime,
    window_end: datetime,
    cache_path: Path,
    *,
    review_mode: bool = False,
) -> list[dict]:
    """Fetch details and keep PRs that were merged into a target branch within the window.

    `review_mode=True` flags the output rows as reviews-given (target branch filter
    still applies, but the user is the reviewer rather than the author).
    """
    details = fetch_pr_details_concurrent(raw_prs, cache_path)
    enriched: list[dict] = []
    for pr in raw_prs:
        repo_full = pr["repository"].get("nameWithOwner") or pr["repository"].get("fullName")
        if not repo_full:
            continue
        key = f"{repo_full}#{pr['number']}"
        d = details.get(key)
        if not d:
            continue
        merged_at = d.get("merged_at")
        if not merged_at:
            continue
        merged_dt = parse_iso(merged_at)
        if not merged_dt or not (window_start <= merged_dt < window_end):
            continue
        base = (d.get("base") or "").lower()
        if base not in target_branches:
            continue
        title = pr.get("title", "") or ""
        enriched.append({
            "repo": repo_full,
            "repoShort": repo_full.split("/")[-1],
            "number": pr["number"],
            "title": title,
            "url": pr["url"],
            "createdAt": pr.get("createdAt"),
            "closedAt": pr.get("closedAt"),
            "mergedAt": merged_at,
            "base": base,
            "head": d.get("head", ""),
            "additions": d.get("additions", 0),
            "deletions": d.get("deletions", 0),
            "files": d.get("files", []),
            "author": (pr.get("author") or {}).get("login", ""),
            "jiraIds": extract_jira_ids(title),
            "kind": PR_KIND_REVIEW if review_mode else PR_KIND_AUTHORED,
        })
    enriched.sort(key=lambda p: p["mergedAt"], reverse=True)
    return enriched
