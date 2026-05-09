"""Render the report dict to JSON and Markdown artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .categorize import review_reason
from .scanner import empty_idle_breakdown
from .utils import now_utc, parse_iso

SHORT_ID_LEN = 8


def _utc_date(iso_ts: str | None) -> str | None:
    """Extract the UTC date (YYYY-MM-DD) from an ISO timestamp."""
    dt = parse_iso(iso_ts)
    return dt.astimezone(timezone.utc).date().isoformat() if dt else None


def _minutes_by_day(
    sessions: list[dict],
    window_start: datetime,
    window_end: datetime,
) -> dict:
    """Per-day session minutes / counts / category mix, keyed by UTC date.

    Pre-seeds every date in the window so consumers can iterate directly
    without filling gaps. Insertion order matches calendar order so a
    naive `Object.entries()` in JS reads the days in time order.
    """
    start_date = window_start.astimezone(timezone.utc).date()
    end_date = window_end.astimezone(timezone.utc).date()  # exclusive

    by_day: dict[str, dict] = {}
    d = start_date
    while d < end_date:
        by_day[d.isoformat()] = {
            "minutes": 0.0, "activeMinutes": 0.0, "idleMinutes": 0.0,
            "sessions": 0, "categories": {},
        }
        d += timedelta(days=1)

    first_day_iso = start_date.isoformat()
    for session in sessions:
        day = _utc_date(session.get("createdAt"))
        if not day:
            continue
        # Sessions whose `createdAt` is before the window can still be in the
        # report because their activity (lastActivityAt) falls inside the
        # window. Snap them to the first window day so the per-day counts
        # sum to totals.sessions instead of silently dropping them.
        if day < first_day_iso:
            day = first_day_iso
        if day not in by_day:
            continue  # post-window guard, defensive
        bucket = by_day[day]
        bucket["minutes"] += _session_duration(session, "durationMin")
        bucket["activeMinutes"] += _session_duration(session, "activeDurationMin")
        bucket["idleMinutes"] += _session_duration(session, "idleMinutes")
        bucket["sessions"] += 1
        cat = session.get("category", "other")
        bucket["categories"][cat] = bucket["categories"].get(cat, 0) + 1

    for bucket in by_day.values():
        bucket["minutes"] = round(bucket["minutes"], 1)
        bucket["activeMinutes"] = round(bucket["activeMinutes"], 1)
        bucket["idleMinutes"] = round(bucket["idleMinutes"], 1)
    return by_day


def _session_duration(session: dict, field: str) -> float:
    """Read a duration field with fallbacks for derived and pre-v1.2.0 reports."""
    if field == "idleMinutes":
        return float(session.get("idleSec") or 0) / 60
    val = session.get(field)
    if val is None and field != "durationMin":
        val = session.get("durationMin")
    return float(val or 0)


def _sum_minutes(
    sessions: list[dict], *, group_key: str, duration_key: str, default_group: str = "other",
) -> dict:
    """Sum a duration field grouped by a key field, sorted desc."""
    totals: dict[str, float] = {}
    for session in sessions:
        group = session.get(group_key) or default_group
        totals[group] = totals.get(group, 0.0) + _session_duration(session, duration_key)
    return {k: round(v, 1) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])}


def _prs_by_day(prs: list[dict], reviewed_prs: list[dict]) -> dict:
    """Merged PRs per UTC date, as summary objects."""
    by_day: dict[str, list[dict]] = {}
    for p in (prs or []) + (reviewed_prs or []):
        day = _utc_date(p.get("mergedAt"))
        if not day:
            continue
        by_day.setdefault(day, []).append(
            {
                "repo": p.get("repoShort"),
                "number": p.get("number"),
                "title": p.get("title"),
                "kind": p.get("kind"),
                "url": p.get("url"),
            }
        )
    return by_day


def _build_tickets(
    sessions: list[dict], prs: list[dict], reviewed_prs: list[dict]
) -> list[dict]:
    """Group sessions and PRs by Jira ID.

    `title` and `status` start as None — the optional Atlassian MCP refinement
    pass (documented in SKILL.md) can populate them in `report.json` and
    `--rerender` will preserve those values across re-emits because this
    builder only sets them when a ticket is *first* seen.
    """
    tickets: dict[str, dict] = {}
    for session in sessions:
        for jid in session.get("jiraIds") or []:
            t = tickets.setdefault(
                jid,
                {"id": jid, "sessionIds": [], "prKeys": [], "title": None, "status": None},
            )
            if session["sessionId"] not in t["sessionIds"]:
                t["sessionIds"].append(session["sessionId"])
    for p in (prs or []) + (reviewed_prs or []):
        for jid in p.get("jiraIds") or []:
            t = tickets.setdefault(
                jid,
                {"id": jid, "sessionIds": [], "prKeys": [], "title": None, "status": None},
            )
            key = f'{p.get("repo")}#{p.get("number")}'
            if key not in t["prKeys"]:
                t["prKeys"].append(key)
    return sorted(
        tickets.values(),
        key=lambda t: -(len(t["sessionIds"]) + len(t["prKeys"])),
    )


def _compute_totals(
    sessions: list[dict],
    prs: list[dict],
    reviewed_prs: list[dict],
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict:
    """Derive the totals block from sessions/prs. Used by both initial build
    and the --rerender path so the two stay in lock-step."""
    by_category: dict[str, int] = {}
    by_repo: dict[str, int] = {}
    for session in sessions:
        by_category[session["category"]] = by_category.get(session["category"], 0) + 1
        by_repo[session["repoShort"] or "unknown"] = by_repo.get(session["repoShort"] or "unknown", 0) + 1

    pr_by_repo: dict[str, int] = {}
    for p in prs:
        pr_by_repo[p["repoShort"]] = pr_by_repo.get(p["repoShort"], 0) + 1

    totals: dict = {
        "sessions": len(sessions),
        "prs": len(prs),
        "reviewedPrs": len(reviewed_prs),
        "categories": by_category,
        "sessionsByRepo": by_repo,
        "prsByRepo": pr_by_repo,
        "uncorrelatedSessions": sum(1 for session in sessions if not session.get("correlatedPRs")),
        "minutesByRepo": _sum_minutes(sessions, group_key="repoShort", duration_key="durationMin", default_group="unknown"),
        "categoryMinutes": _sum_minutes(sessions, group_key="category", duration_key="durationMin"),
        "activeMinutesByRepo": _sum_minutes(sessions, group_key="repoShort", duration_key="activeDurationMin", default_group="unknown"),
        "activeCategoryMinutes": _sum_minutes(sessions, group_key="category", duration_key="activeDurationMin"),
        "idleMinutesByRepo": _sum_minutes(sessions, group_key="repoShort", duration_key="idleMinutes", default_group="unknown"),
        "idleCategoryMinutes": _sum_minutes(sessions, group_key="category", duration_key="idleMinutes"),
    }
    if window_start and window_end:
        totals["minutesByDay"] = _minutes_by_day(sessions, window_start, window_end)
        totals["prsByDay"] = _prs_by_day(prs, reviewed_prs)
    return totals


def recompute_totals(report: dict) -> None:
    """Recompute the totals block (and tickets) in-place from the report's sessions/prs.

    Used by the --rerender path after the calling agent has edited session
    fields (e.g. categories) in the on-disk report.json. The `weekStart` and
    `workweekDays` fields are intentionally NOT touched here — they were
    locked in at initial generation and shouldn't shift on re-emit.
    """
    for session in report.get("sessions", []):
        if "needsReview" not in session:
            reason = review_reason(session.get("category", ""))
            session["needsReview"] = reason is not None
            session["reviewReason"] = reason
        # Backfill gap-classification fields for reports generated before a
        # given field existed, so --rerender round-trips validate. Real values
        # get recomputed on the next full generate run.
        session.setdefault("idleSec", 0.0)
        if "idleBreakdownSec" not in session:
            session["idleBreakdownSec"] = empty_idle_breakdown()
        session.setdefault("userPauseCount", 0)
        session.setdefault("longestUserPauseSec", 0.0)
        session.setdefault("gaps", [])
        session.setdefault("segments", [])
        session.setdefault("needsActiveReview", False)
        session.setdefault("activeReviewReason", None)
    ws = parse_iso(report.get("windowStart"))
    we = parse_iso(report.get("windowEnd"))
    report["totals"] = _compute_totals(
        report.get("sessions", []),
        report.get("prs", []),
        report.get("reviewedPrs") or [],
        window_start=ws,
        window_end=we,
    )
    report["tickets"] = _merge_tickets(
        existing=report.get("tickets") or [],
        rebuilt=_build_tickets(
            report.get("sessions", []),
            report.get("prs", []),
            report.get("reviewedPrs") or [],
        ),
    )


def _merge_tickets(existing: list[dict], rebuilt: list[dict]) -> list[dict]:
    """Preserve any title/status enrichment the calling agent injected via the
    Atlassian MCP refinement pass. The rebuilt list is the source of truth
    for sessionIds/prKeys; existing is just a lookup for enriched fields."""
    enriched = {t["id"]: t for t in existing if t.get("title") or t.get("status")}
    for t in rebuilt:
        if t["id"] in enriched:
            t["title"] = enriched[t["id"]].get("title")
            t["status"] = enriched[t["id"]].get("status")
    return rebuilt


def build_report(
    sessions: list[dict],
    prs: list[dict],
    reviewed_prs: list[dict],
    session_to_prs: dict,
    pr_to_sessions: dict,
    *,
    window_start: datetime,
    window_end: datetime,
    user: str,
    target_branches: set[str],
    week_start: str | None = None,
    workweek_days: int | None = None,
) -> dict:
    """Assemble the final report dict from raw inputs and correlation maps."""
    for session in sessions:
        session["correlatedPRs"] = session_to_prs.get(session["sessionId"], [])
    for p in prs:
        k = f'{p["repo"]}#{p["number"]}'
        p["correlatedSessions"] = pr_to_sessions.get(k, [])
    # Reviewed PRs aren't correlated by design (correlate() runs only against
    # authored PRs), but the field must still exist for schema consistency.
    for p in reviewed_prs:
        p.setdefault("correlatedSessions", [])

    return {
        "generatedAt": now_utc().isoformat(),
        "windowStart": window_start.isoformat(),
        "windowEnd": window_end.isoformat(),
        "user": user,
        "targetBranches": sorted(target_branches),
        "weekStart": week_start,
        "workweekDays": workweek_days,
        "totals": _compute_totals(
            sessions, prs, reviewed_prs,
            window_start=window_start, window_end=window_end,
        ),
        "tickets": _build_tickets(sessions, prs, reviewed_prs),
        "sessions": sessions,
        "prs": prs,
        "reviewedPrs": reviewed_prs,
    }


def write_json(report: dict, path: Path) -> None:
    path.write_text(json.dumps(report, indent=2, default=str))


def _format_jira_tags(jira_ids: list[str]) -> str:
    return " " + " ".join(f"`{j}`" for j in jira_ids) if jira_ids else ""


def _render_pr_section(lines: list[str], heading: str, prs: list[dict], *, show_sessions: bool) -> None:
    if not prs:
        return
    lines.append(f"## {heading}")
    by_repo: dict[str, list[dict]] = {}
    for p in prs:
        by_repo.setdefault(p["repoShort"], []).append(p)
    for repo, group in sorted(by_repo.items()):
        lines.append(f"### {repo}")
        for p in sorted(group, key=lambda x: x["mergedAt"], reverse=True):
            jira = _format_jira_tags(p.get("jiraIds", []))
            if show_sessions:
                sessions = p.get("correlatedSessions") or []
                if sessions:
                    suffix = " — sessions: " + ", ".join(
                        f"`{m['sessionId'][:SHORT_ID_LEN]}`(score {m['score']})" for m in sessions
                    )
                else:
                    suffix = " — _no matching session_"
            else:
                suffix = f" by `{p.get('author', '?')}`"
            lines.append(
                f"- [#{p['number']}]({p['url']}) `{p['head']}` → `{p['base']}`{jira} — {p['title']}{suffix}"
            )
        lines.append("")


def write_markdown(report: dict, path: Path) -> None:
    lines: list[str] = []
    lines.append(
        f"# Claude dev digest — {report['windowStart'][:10]} → {report['windowEnd'][:10]}"
    )
    lines.append("")
    t = report["totals"]
    lines.append(f"- User: **{report.get('user') or '?'}**")
    lines.append(f"- Sessions: **{t['sessions']}**")
    lines.append(
        f"- Merged PRs to {'/'.join(report.get('targetBranches', []))}: **{t['prs']}**"
    )
    if t.get("reviewedPrs"):
        lines.append(f"- PRs reviewed: **{t['reviewedPrs']}**")
    lines.append(f"- Sessions without a matching PR: **{t['uncorrelatedSessions']}**")
    lines.append("")

    lines.append("## Sessions by category")
    for cat, n in sorted(t["categories"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- {cat}: {n}")
    lines.append("")

    lines.append("## Sessions by repo")
    for repo, n in sorted(t["sessionsByRepo"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- {repo}: {n}")
    lines.append("")

    _render_pr_section(lines, "Merged PRs you authored", report["prs"], show_sessions=True)
    _render_pr_section(lines, "PRs you reviewed", report.get("reviewedPrs") or [], show_sessions=False)

    lines.append("## Sessions")
    for session in report["sessions"]:
        cat = session["category"]
        prs_part = ""
        if session["correlatedPRs"]:
            prs_part = " — PRs: " + ", ".join(
                f"{m['key']}(score {m['score']}, {'+'.join(m['reasons'])})" for m in session["correlatedPRs"]
            )
        jira_part = ""
        if session.get("jiraIds"):
            jira_part = "  jira: " + " ".join(f"`{j}`" for j in session["jiraIds"])
        lines.append(
            f"### [{cat}] {session['repoShort']} · {session['createdAt'][:16].replace('T', ' ')}"
        )
        lines.append(f"- session: `{session['sessionId']}`{prs_part}")
        lines.append(
            f"- branch: `{session['gitBranch'] or '?'}`  duration: {session['durationMin']}m  "
            f"msgs: {session['userMsgCount']}↑/{session['assistantMsgCount']}↓  files: {session['filesTouchedCount']}{jira_part}"
        )
        if session["firstPromptShort"]:
            lines.append(f"- prompt: {session['firstPromptShort']}")
        lines.append("")
    path.write_text("\n".join(lines))
