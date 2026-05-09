"""Correlate Claude sessions with GitHub PRs using a confidence-scored set of signals.

Signals (additive):
  - branch (+5): session.gitBranch matches PR head ref
  - files(N) (+N+1, capped at 5): full repo-relative path overlap
  - basename(N) (+1): basename overlap (used as a softer fallback)
  - jira (+3): a Jira ID found in the session also appears in the PR title
  - time (+2): session activity falls inside the PR's open->merge window

Score < 2 is dropped. Sessions whose activity ended >2h after the PR merged are
hard-rejected (post-merge work, not a contribution).
"""

from __future__ import annotations

import os
from datetime import timedelta

from .utils import parse_iso

MIN_SCORE = 2
POST_MERGE_GRACE = timedelta(hours=2)
PRE_OPEN_GRACE = timedelta(hours=12)
MAX_FILE_OVERLAP_BONUS = 4
MAX_PRS_PER_SESSION = 8
MAX_SESSIONS_PER_PR = 12


def _precompute_pr(p: dict) -> dict:
    """Build the per-PR cache of derived values used by the inner loop."""
    files_full = set(p.get("files") or [])
    return {
        "repoLc": p["repoShort"].lower(),
        "headLc": (p.get("head") or "").lower(),
        "filesFull": files_full,
        "filesBase": {os.path.basename(f) for f in files_full},
        "jiraSet": set(p.get("jiraIds") or []),
        "merged": parse_iso(p["mergedAt"]),
        "created": parse_iso(p.get("createdAt") or ""),
        "key": f'{p["repo"]}#{p["number"]}',
    }


def correlate(sessions: list[dict], prs: list[dict]) -> tuple[dict, dict]:
    """Returns (session_id -> [match dicts], pr_key -> [match dicts])."""
    session_to_prs: dict[str, list[dict]] = {}
    pr_to_sessions: dict[str, list[dict]] = {}

    pr_helpers = [_precompute_pr(p) for p in prs]

    for session in sessions:
        session_repo = (session.get("repo") or "").lower()
        if not session_repo:
            continue
        session_files_full = set(session.get("filesTouchedRelative") or [])
        session_files_base = {os.path.basename(f) for f in session.get("filesTouched", [])}
        session_branch = (session.get("gitBranch") or "").lower()
        session_created = parse_iso(session["createdAt"])
        session_last = parse_iso(session["lastActivityAt"])
        session_jira = set(session.get("jiraIds") or [])
        session_id = session["sessionId"]

        for h in pr_helpers:
            if session_repo != h["repoLc"]:
                continue

            # Hard reject: session started > grace AFTER the PR merged
            if h["merged"] and session_created and session_created > h["merged"] + POST_MERGE_GRACE:
                continue

            score = 0
            reasons: list[str] = []

            head_lc = h["headLc"]
            if session_branch and head_lc and (
                session_branch == head_lc or head_lc in session_branch or session_branch in head_lc
            ):
                score += 5
                reasons.append("branch")

            full_overlap = len(session_files_full & h["filesFull"])
            if full_overlap > 0:
                score += min(full_overlap, MAX_FILE_OVERLAP_BONUS) + 1
                reasons.append(f"files({full_overlap})")
            else:
                base_overlap = len(session_files_base & h["filesBase"])
                if base_overlap > 0:
                    score += 1
                    reasons.append(f"basename({base_overlap})")

            if session_jira and h["jiraSet"] and session_jira & h["jiraSet"]:
                score += 3
                shared = sorted(session_jira & h["jiraSet"])
                reasons.append(f"jira({','.join(shared)})")

            if h["merged"] and h["created"] and session_last:
                if h["created"] - PRE_OPEN_GRACE <= session_last <= h["merged"] + POST_MERGE_GRACE:
                    score += 2
                    reasons.append("time")

            if score < MIN_SCORE:
                continue

            session_to_prs.setdefault(session_id, []).append(
                {"key": h["key"], "score": score, "reasons": reasons}
            )
            pr_to_sessions.setdefault(h["key"], []).append(
                {"sessionId": session_id, "score": score, "reasons": reasons}
            )

    for v in session_to_prs.values():
        v.sort(key=lambda x: -x["score"])
        del v[MAX_PRS_PER_SESSION:]
    for v in pr_to_sessions.values():
        v.sort(key=lambda x: -x["score"])
        del v[MAX_SESSIONS_PER_PR:]

    return session_to_prs, pr_to_sessions
