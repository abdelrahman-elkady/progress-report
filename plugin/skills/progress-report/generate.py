#!/usr/bin/env python3
"""
Generate a Claude Code dev digest.

Scans ~/.claude/projects for sessions in a date window, fetches the configured
GitHub user's authored + reviewed PRs, correlates them, categorizes the sessions,
and emits JSON / Markdown to an output directory.

Run with `--help` for the full list of options.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.categorize import categorize, review_reason
from lib.correlate import correlate
from lib.github import (
    enrich_prs,
    gh_current_user,
    search_authored_prs,
    search_reviewed_prs,
)
from lib.report import (
    build_report,
    recompute_totals,
    write_json,
    write_markdown,
)
from lib.scanner import scan_sessions

DEFAULT_BRANCHES = "master,main"
DEFAULT_OUTPUT_DIR = Path.home() / "claude-dev-digest"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
WINDOW_PADDING_DAYS = 1

# Maps the --week-start CLI flag to Python's weekday() index (Mon=0..Sun=6).
# Used by _compute_window to snap the window's start to the user's workweek.
DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# ----- CLI -------------------------------------------------------------------

def _local_tz():
    return datetime.now().astimezone().tzinfo


def _parse_date_local(s: str) -> datetime:
    """Parse YYYY-MM-DD in the local timezone, returned as a tz-aware datetime."""
    dt = datetime.strptime(s, "%Y-%m-%d")
    return dt.replace(tzinfo=_local_tz())


def _compute_window(args) -> tuple[datetime, datetime]:
    """Compute (start, end) UTC datetimes from CLI args, in local-timezone-aware fashion.

    Three modes, in priority order:
    1. **Explicit dates** (`--from` / `--to`) — always win, calendar-agnostic.
    2. **Workweek-aligned** (`--week-start`) — snap the start to the most recent
       occurrence of the chosen weekday on or before today, then run `--days`
       days forward, capped so we never include the future.
    3. **Default** — end = midnight after today, start = end - (days + padding).

    The padding (`WINDOW_PADDING_DAYS`) is intentional so sessions started near
    midnight aren't dropped — it's applied to the start in all three modes.
    """
    tz = _local_tz()
    now_local = datetime.now(tz)
    today_end = (now_local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    if args.from_date or args.to:
        # Explicit dates always win
        end_local = (
            _parse_date_local(args.to) + timedelta(days=1) if args.to else today_end
        )
        start_local = (
            _parse_date_local(args.from_date)
            if args.from_date
            else end_local - timedelta(days=args.days + WINDOW_PADDING_DAYS)
        )
    elif args.week_start:
        # Snap start to the most recent occurrence of <week-start>
        target_idx = DAY_INDEX[args.week_start]
        today_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        days_back = (today_midnight.weekday() - target_idx) % 7
        workweek_start = today_midnight - timedelta(days=days_back)
        natural_end = workweek_start + timedelta(days=args.days)
        end_local = min(natural_end, today_end)
        start_local = workweek_start - timedelta(days=WINDOW_PADDING_DAYS)
    else:
        # Default — end = today+1, start = end - days - padding
        end_local = today_end
        start_local = end_local - timedelta(days=args.days + WINDOW_PADDING_DAYS)

    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="claude-dev-digest",
        description="Generate a Claude+GitHub dev digest.",
    )
    p.add_argument("--days", type=int, default=7,
                   help="Window size in days, ending today (default: 7). Ignored if --from is set.")
    p.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD",
                   help="Explicit window start (local time). Overrides --days and --week-start.")
    p.add_argument("--to", metavar="YYYY-MM-DD",
                   help="Explicit window end (local time, inclusive). Defaults to today.")
    p.add_argument("--week-start", dest="week_start", choices=list(DAY_INDEX.keys()),
                   default=None,
                   help="Snap the window start to the most recent <day-of-week> on or before "
                        "today. Combine with --days for a workweek report (e.g. "
                        "`--week-start sun --days 5` for Sun→Thu). Default: no snapping.")
    p.add_argument("--user", default=None,
                   help="GitHub login to fetch PRs for. Defaults to `gh api user`.")
    p.add_argument("--branches", default=DEFAULT_BRANCHES,
                   help=f"Comma-separated branches that count as 'shipped' (default: {DEFAULT_BRANCHES}).")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                   help="Directory for outputs (default: ~/claude-dev-digest).")
    p.add_argument("--format", choices=["all", "json", "md"], default="all",
                   help="Which artifacts to write (default: all).")
    p.add_argument("--no-reviews", action="store_true",
                   help="Skip the 'PRs you reviewed' fetch.")
    p.add_argument("--clear-cache", action="store_true",
                   help="Discard the cached PR detail file before fetching.")
    p.add_argument("--user-pause-cap-min", dest="user_pause_cap_min",
                   type=float, default=10, metavar="MINUTES",
                   help="Cap each user_pause gap (assistant → user, with no pending "
                        "tool_use) at this many minutes; excess becomes idle. Default: 10.")
    p.add_argument("--tool-runtime-cap-min", dest="tool_runtime_cap_min",
                   type=float, default=30, metavar="MINUTES",
                   help="Cap each tool_runtime and inference gap at this many minutes; "
                        "excess becomes idle. Protects against tool_use approval waits "
                        "being counted as active time. Default: 30.")
    p.add_argument("--rerender", action="store_true",
                   help="Skip scan/fetch/correlate. Load <output-dir>/report.json, "
                        "recompute totals, and re-emit the artifacts selected by --format. "
                        "Use after editing session categories or other report fields in place.")
    return p.parse_args(argv)


def _rerender(output_dir: Path, fmt: str) -> int:
    """Reload an existing report.json and re-emit md/json from it.

    Categories or other session fields may have been edited in place between
    runs; recompute the totals block so the rendered artifacts stay consistent.
    """
    src = output_dir / "report.json"
    if not src.exists():
        print(f"[error] --rerender: {src} does not exist. Run generate.py first.",
              file=sys.stderr)
        return 1

    report = json.loads(src.read_text())
    recompute_totals(report)

    written: list[Path] = []
    if fmt in ("all", "json"):
        write_json(report, src)
        written.append(src)
    if fmt in ("all", "md"):
        path = output_dir / "report.md"
        write_markdown(report, path)
        written.append(path)

    print("Re-rendered from", src)
    for p in written:
        print(f"  {p}")
    return 0


# ----- Main ------------------------------------------------------------------

def main(argv=None) -> int:
    args = _parse_args(argv)

    if args.rerender:
        return _rerender(args.output_dir, args.format)

    user = args.user or gh_current_user()
    if not user:
        print("[error] could not determine GitHub user. Pass --user or run `gh auth login`.",
              file=sys.stderr)
        return 1

    target_branches = {b.strip().lower() for b in args.branches.split(",") if b.strip()}

    window_start, window_end = _compute_window(args)

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "_pr-cache.json"
    if args.clear_cache:
        cache_path.unlink(missing_ok=True)

    print(
        f"[window] {window_start.astimezone().date()} → {window_end.astimezone().date()}  "
        f"(user: {user}, branches: {sorted(target_branches)})"
    )

    # Sessions
    print(f"[scan] sessions in {PROJECTS_DIR}")
    sessions = scan_sessions(
        PROJECTS_DIR, window_start, window_end,
        user_pause_cap_min=args.user_pause_cap_min,
        tool_runtime_cap_min=args.tool_runtime_cap_min,
    )
    print(f"  found {len(sessions)} sessions in window")

    # PR searches: authored + reviewed are independent gh subprocesses, run in parallel
    since = window_start.date().isoformat()
    print(f"[fetch] PR searches since {since} (authored{' + reviewed' if not args.no_reviews else ''})")
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_authored = ex.submit(search_authored_prs, user, since)
        f_reviewed = ex.submit(search_reviewed_prs, user, since) if not args.no_reviews else None
        raw_authored = f_authored.result()
        raw_reviewed = f_reviewed.result() if f_reviewed else []
    print(f"  {len(raw_authored)} authored / {len(raw_reviewed)} reviewed candidates")

    prs = enrich_prs(
        raw_authored, target_branches, window_start, window_end, cache_path,
        review_mode=False,
    )
    print(f"  kept {len(prs)} authored, merged into {sorted(target_branches)}")

    reviewed_prs: list[dict] = []
    if raw_reviewed:
        reviewed_prs = enrich_prs(
            raw_reviewed, target_branches, window_start, window_end, cache_path,
            review_mode=True,
        )
        reviewed_prs = [p for p in reviewed_prs if (p.get("author") or "").lower() != user.lower()]
        print(f"  kept {len(reviewed_prs)} reviewed")

    # Categorize sessions
    for session in sessions:
        cat = categorize(session)
        session["category"] = cat
        reason = review_reason(cat)
        session["needsReview"] = reason is not None
        session["reviewReason"] = reason

    # Correlate
    print("[correlate] sessions ↔ authored PRs")
    session_to_prs, pr_to_sessions = correlate(sessions, prs)
    print(f"  matched {len(session_to_prs)} sessions, {len(pr_to_sessions)} PRs")

    # Build report
    report = build_report(
        sessions, prs, reviewed_prs, session_to_prs, pr_to_sessions,
        window_start=window_start,
        window_end=window_end,
        user=user,
        target_branches=target_branches,
        week_start=args.week_start,
        workweek_days=args.days,
    )

    # Write artifacts
    written: list[Path] = []
    if args.format in ("all", "json"):
        path = output_dir / "report.json"
        write_json(report, path)
        written.append(path)
    if args.format in ("all", "md"):
        path = output_dir / "report.md"
        write_markdown(report, path)
        written.append(path)

    print()
    print("Done. Outputs:")
    for p in written:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
