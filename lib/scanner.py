"""Scan ~/.claude/projects/**/*.jsonl for Claude Code sessions in a date window."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .jira import extract_jira_ids
from .utils import (
    git_root_for_cwd,
    parse_iso,
    repo_name,
    repo_short,
    shorten,
    text_from_content,
)

# .jsonl record types we never look at
SKIP_TYPES = {
    "progress",
    "file-history-snapshot",
    "queue-operation",
    "system",
    "last-prompt",
}

# Tools whose `input.file_path` (or `input.notebook_path`) marks a file as touched
FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})

# Bash sample is intentionally trimmed — full tool inputs/outputs are never
# captured at all (only tool names are counted), and the user/assistant text
# messages are kept in full so consumers can display the whole conversation.
MAX_BASH_CMDS = 8
BASH_CMD_PREVIEW_LEN = 200
FIRST_PROMPT_PREVIEW_LEN = 200

# Same-speaker inter-record gaps below this are logical continuations of one
# turn (e.g. thinking/text/tool_use blocks split into separate records), not a
# real pause.
SAME_TURN_MAX_SEC = 5.0

# Gap kinds (mirrored in report.schema.json Gap.kind enum).
GAP_SAME_TURN = "same_turn"
GAP_TOOL_RUNTIME = "tool_runtime"
GAP_INFERENCE = "inference"
GAP_USER_PAUSE = "user_pause"

# Thresholds for flagging `needsActiveReview`. Tuned from inspecting the
# historical sample report; see ai-docs/plans/002-active-duration-gap-classification.md.
LONG_PAUSE_SEC = 3600.0
HIGH_IDLE_RATIO = 0.5
MANY_LONG_PAUSES = 5

# Values for session.activeReviewReason (mirrored in report.schema.json).
REASON_LONG_PAUSE = "long_single_pause"
REASON_HIGH_IDLE = "high_idle_ratio"
REASON_MANY_PAUSES = "many_long_pauses"


def _file_path_from_input(name: str, inp: dict) -> str | None:
    if name == "NotebookEdit":
        return inp.get("notebook_path") or inp.get("file_path")
    if name in FILE_TOOLS:
        return inp.get("file_path")
    return None


def _relativize(absolute: str, git_root: Path | None) -> str:
    if not git_root:
        return absolute
    try:
        return str(Path(absolute).resolve().relative_to(git_root.resolve()))
    except (ValueError, OSError):
        return absolute


def _segment_dict(start: datetime, end: datetime, msg_count: int) -> dict:
    return {
        "startedAt": start.isoformat(),
        "endedAt": end.isoformat(),
        "sec": round((end - start).total_seconds(), 1),
        "messageCount": msg_count,
    }


def _classify_and_score_gaps(
    records: list[dict], *, cap_sec: float,
) -> dict:
    """Classify every inter-record gap and return active/idle totals plus detail.

    Each `records` entry is `{ts: datetime, speaker: "user"|"assistant",
    has_tool_use: bool}`. Gap kinds:

      - `same_turn`     — same speaker, gap < 5s (thinking/text/tool_use split
                          into separate records for the same turn). Full credit.
      - `tool_runtime`  — assistant-with-tool_use → user. Claude was running a
                          tool on the user's behalf. Full credit.
      - `inference`     — user → assistant. Claude was generating. Full credit.
      - `user_pause`    — assistant (no pending tool_use) → user. The user was
                          reading/typing or away. Credited up to `cap_sec`; the
                          excess becomes idle time.

    Returns ready-to-serialize fields: `activeSec`, `idleSec` (both rounded to
    1 dp), `gaps` (only over-cap user_pause gaps — under-cap and non-pause
    gaps aren't emitted to keep the array bounded), `segments` (contiguous
    bursts split by over-cap gaps), `userPauseCount`, `longestUserPauseSec`.
    Records must be in chronological order — violations are tolerated as
    no-op gaps (`gap_sec <= 0 → continue`).
    """
    if not records:
        return {
            "activeSec": 0.0, "idleSec": 0.0,
            "gaps": [], "segments": [],
            "userPauseCount": 0, "longestUserPauseSec": 0.0,
        }
    if len(records) < 2:
        return {
            "activeSec": 0.0, "idleSec": 0.0,
            "gaps": [],
            "segments": [_segment_dict(records[0]["ts"], records[0]["ts"], 1)],
            "userPauseCount": 0, "longestUserPauseSec": 0.0,
        }

    active_sec = 0.0
    idle_sec = 0.0
    gaps_out: list[dict] = []
    segments_out: list[dict] = []
    user_pause_count = 0
    longest_user_pause_sec = 0.0

    seg_start = records[0]["ts"]
    seg_end = records[0]["ts"]
    seg_msgs = 1

    for i in range(1, len(records)):
        prev, curr = records[i - 1], records[i]
        gap_sec = (curr["ts"] - prev["ts"]).total_seconds()
        if gap_sec <= 0:
            seg_end = curr["ts"]
            seg_msgs += 1
            continue

        if prev["speaker"] == curr["speaker"] and gap_sec < SAME_TURN_MAX_SEC:
            kind = GAP_SAME_TURN
        elif prev["speaker"] == "assistant" and prev["has_tool_use"]:
            kind = GAP_TOOL_RUNTIME
        elif prev["speaker"] == "user":
            kind = GAP_INFERENCE
        else:
            kind = GAP_USER_PAUSE

        if kind == GAP_USER_PAUSE:
            user_pause_count += 1
            if gap_sec > longest_user_pause_sec:
                longest_user_pause_sec = gap_sec
            credited = min(gap_sec, cap_sec)
            active_sec += credited
            idle_sec += max(0.0, gap_sec - credited)
            if gap_sec > cap_sec:
                gaps_out.append({
                    "startedAt": prev["ts"].isoformat(),
                    "endedAt": curr["ts"].isoformat(),
                    "sec": round(gap_sec, 1),
                    "kind": kind,
                    "creditedSec": round(credited, 1),
                })
                segments_out.append(_segment_dict(seg_start, seg_end, seg_msgs))
                seg_start = curr["ts"]
                seg_end = curr["ts"]
                seg_msgs = 1
                continue
        else:
            active_sec += gap_sec

        seg_end = curr["ts"]
        seg_msgs += 1

    segments_out.append(_segment_dict(seg_start, seg_end, seg_msgs))

    return {
        "activeSec": round(active_sec, 1),
        "idleSec": round(idle_sec, 1),
        "gaps": gaps_out,
        "segments": segments_out,
        "userPauseCount": user_pause_count,
        "longestUserPauseSec": round(longest_user_pause_sec, 1),
    }


def _active_review_flag(
    *, longest_user_pause_sec: float, idle_sec: float, duration_sec: float,
    gaps: list[dict],
) -> tuple[bool, str | None]:
    """Decide whether the session's active-duration calculation is worth a
    second look by the refinement pass. See plan 002 for rationale."""
    if longest_user_pause_sec > LONG_PAUSE_SEC:
        return True, REASON_LONG_PAUSE
    if duration_sec > 0 and idle_sec / duration_sec > HIGH_IDLE_RATIO:
        return True, REASON_HIGH_IDLE
    if len(gaps) >= MANY_LONG_PAUSES:
        return True, REASON_MANY_PAUSES
    return False, None


def parse_session_file(
    path: Path, window_start: datetime, window_end: datetime,
    *, user_pause_cap_min: float = 10.0,
) -> dict | None:
    """Parse one .jsonl session file. Returns None if it's empty, sidechain, or
    falls outside the window.

    The mtime gate in `scan_sessions` ensures we never get here for files clearly
    outside the window. We still re-check after parsing because the file's
    `created` (first user record) is the more accurate inclusion test.
    """
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

    first_user_ts: datetime | None = None
    last_ts: datetime | None = None
    first_prompt = ""
    cwd = ""
    git_branch = ""
    user_msgs = 0
    assistant_msgs = 0
    tool_counts: dict[str, int] = {}
    files_touched: set[str] = set()
    bash_cmds: list[str] = []
    user_messages: list[dict] = []
    assistant_texts: list[dict] = []
    gap_records: list[dict] = []

    try:
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type", "")
                if rtype in SKIP_TYPES:
                    continue
                if rec.get("isSidechain"):
                    continue
                ts = parse_iso(rec.get("timestamp", ""))
                if ts and (last_ts is None or ts > last_ts):
                    last_ts = ts
                msg = rec.get("message", {}) or {}
                ts_iso = ts.isoformat() if ts else ""

                if rtype == "user":
                    if rec.get("isMeta"):
                        continue
                    user_msgs += 1
                    if first_user_ts is None and ts:
                        first_user_ts = ts
                        cwd = rec.get("cwd", cwd) or cwd
                        git_branch = rec.get("gitBranch", git_branch) or git_branch
                    text = text_from_content(msg.get("content", "")).strip()
                    if text and not first_prompt:
                        first_prompt = text
                    if text:
                        user_messages.append({"ts": ts_iso, "text": text})
                    if ts:
                        gap_records.append({"ts": ts, "speaker": "user", "has_tool_use": False})

                elif rtype == "assistant":
                    assistant_msgs += 1
                    raw = msg.get("content", "")
                    has_tool_use = False
                    if isinstance(raw, list):
                        for b in raw:
                            if not isinstance(b, dict):
                                continue
                            btype = b.get("type", "")
                            if btype == "text":
                                txt = b.get("text", "").strip()
                                if txt:
                                    assistant_texts.append({"ts": ts_iso, "text": txt})
                            elif btype == "tool_use":
                                has_tool_use = True
                                name = b.get("name", "")
                                tool_counts[name] = tool_counts.get(name, 0) + 1
                                inp = b.get("input", {}) or {}
                                fp = _file_path_from_input(name, inp)
                                if fp:
                                    files_touched.add(fp)
                                elif name == "Bash" and len(bash_cmds) < MAX_BASH_CMDS:
                                    cmd = inp.get("command", "")
                                    if cmd:
                                        bash_cmds.append(cmd[:BASH_CMD_PREVIEW_LEN])
                    elif isinstance(raw, str):
                        stripped = raw.strip()
                        if stripped:
                            assistant_texts.append({"ts": ts_iso, "text": stripped})
                    if not has_tool_use and msg.get("stop_reason") == "tool_use":
                        # Defensive: some records store stop_reason without a
                        # corresponding block (content truncated / older format).
                        has_tool_use = True
                    if ts:
                        gap_records.append({"ts": ts, "speaker": "assistant", "has_tool_use": has_tool_use})
    except OSError as e:
        print(f"  ! failed to read {path}: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ! failed to parse {path}: {e}", file=sys.stderr)
        return None

    if user_msgs == 0:
        return None

    created = first_user_ts or modified
    if not (window_start <= modified < window_end or window_start <= created < window_end):
        return None

    git_root = git_root_for_cwd(cwd)
    files_sorted = sorted(files_touched)
    files_relative = [_relativize(f, git_root) for f in files_sorted]

    jira_ids = sorted(
        set(extract_jira_ids(first_prompt))
        | set(extract_jira_ids(git_branch))
        | {j for m in user_messages for j in extract_jira_ids(m["text"])}
        | {j for m in assistant_texts for j in extract_jira_ids(m["text"])}
    )

    classified = _classify_and_score_gaps(gap_records, cap_sec=user_pause_cap_min * 60)
    duration_sec = ((last_ts or modified) - created).total_seconds()
    needs_active_review, active_review_reason = _active_review_flag(
        longest_user_pause_sec=classified["longestUserPauseSec"],
        idle_sec=classified["idleSec"],
        duration_sec=duration_sec,
        gaps=classified["gaps"],
    )

    return {
        "sessionId": path.stem,
        "filePath": str(path),
        "cwd": cwd,
        "repo": repo_name(cwd),
        "repoShort": repo_short(cwd),
        "gitBranch": git_branch,
        "firstPrompt": first_prompt,
        "firstPromptShort": shorten(first_prompt, FIRST_PROMPT_PREVIEW_LEN),
        "createdAt": created.isoformat(),
        "lastActivityAt": (last_ts or modified).isoformat(),
        "modifiedAt": modified.isoformat(),
        "durationMin": round(duration_sec / 60, 1),
        "activeDurationMin": round(classified["activeSec"] / 60, 1),
        "idleSec": classified["idleSec"],
        "userPauseCount": classified["userPauseCount"],
        "longestUserPauseSec": classified["longestUserPauseSec"],
        "gaps": classified["gaps"],
        "segments": classified["segments"],
        "needsActiveReview": needs_active_review,
        "activeReviewReason": active_review_reason,
        "userMsgCount": user_msgs,
        "assistantMsgCount": assistant_msgs,
        "toolCounts": tool_counts,
        "filesTouched": files_sorted,
        "filesTouchedRelative": files_relative,
        "filesTouchedCount": len(files_sorted),
        "bashCmdSample": bash_cmds,
        "userMessages": user_messages,
        "assistantTexts": assistant_texts,
        "jiraIds": jira_ids,
    }


def scan_sessions(
    projects_dir: Path,
    window_start: datetime,
    window_end: datetime,
    *,
    user_pause_cap_min: float = 10.0,
) -> list[dict]:
    """Walk projects_dir/*/*.jsonl, return sessions whose activity falls in the window.

    A file's mtime is the upper bound on any activity it could contain, so we use
    `path.stat().st_mtime < window_start` as a cheap pre-filter to avoid parsing
    files that are clearly stale (the dominant cost on machines with months of
    history). Subagent files are skipped entirely.
    """
    window_start_ts = window_start.timestamp()
    sessions: list[dict] = []
    for p in projects_dir.glob("*/*.jsonl"):
        if "subagents" in p.parts:
            continue
        # Skip symlinks — glob follows them by default and a symlinked .jsonl
        # could point outside ~/.claude/projects/ to an arbitrary file.
        if p.is_symlink():
            continue
        try:
            if p.stat().st_mtime < window_start_ts:
                continue
        except OSError:
            continue
        session = parse_session_file(p, window_start, window_end, user_pause_cap_min=user_pause_cap_min)
        if session:
            sessions.append(session)
    sessions.sort(key=lambda session: session["createdAt"], reverse=True)
    return sessions
