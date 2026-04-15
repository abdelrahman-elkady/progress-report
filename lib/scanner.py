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


def _active_duration_minutes(
    timestamps: list[datetime],
    idle_threshold_min: float = 45.0,
) -> float:
    """Compute active duration by subtracting gaps > threshold from wall-clock span.

    Walks consecutive message timestamps; only gaps within the threshold
    contribute to active time. Gaps exceeding the threshold are idle time.
    """
    if len(timestamps) < 2:
        return 0.0
    sorted_ts = sorted(timestamps)
    threshold_sec = idle_threshold_min * 60
    total_active_sec = 0.0
    for i in range(1, len(sorted_ts)):
        gap = (sorted_ts[i] - sorted_ts[i - 1]).total_seconds()
        if gap <= threshold_sec:
            total_active_sec += gap
    return round(total_active_sec / 60, 1)


def parse_session_file(
    path: Path, window_start: datetime, window_end: datetime,
    *, idle_threshold_min: float = 45.0,
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
    all_msg_timestamps: list[datetime] = []

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

                if ts and rtype in ("user", "assistant"):
                    all_msg_timestamps.append(ts)

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

                elif rtype == "assistant":
                    assistant_msgs += 1
                    raw = msg.get("content", "")
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
        "durationMin": round(((last_ts or modified) - created).total_seconds() / 60, 1),
        "activeDurationMin": _active_duration_minutes(all_msg_timestamps, idle_threshold_min),
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
    idle_threshold_min: float = 45.0,
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
        try:
            if p.stat().st_mtime < window_start_ts:
                continue
        except OSError:
            continue
        session = parse_session_file(p, window_start, window_end, idle_threshold_min=idle_threshold_min)
        if session:
            sessions.append(session)
    sessions.sort(key=lambda session: session["createdAt"], reverse=True)
    return sessions
