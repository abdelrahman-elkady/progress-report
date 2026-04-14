"""Pure helpers shared across modules: time parsing, text shortening, repo resolution."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# Strip <ide_opened_file>...</ide_opened_file>, <ide_selection>..., <command-name>...,
# etc. — IDE/CLI launchers prepend these wrappers to user prompts and they hide the
# real text behind a wall of XML-ish noise. We don't try to parse them, just remove
# them and any trailing whitespace.
_XML_WRAPPER_RE = re.compile(r"<([a-zA-Z][\w-]*)\b[^>]*>.*?</\1>", re.DOTALL)
_BARE_TAG_RE = re.compile(r"<[^>]+>")


def parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO-8601 string to a tz-aware datetime, or None on failure."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def shorten(text: str | None, n: int = 140) -> str:
    """Strip IDE/CLI XML wrappers, collapse whitespace, clip to n chars (with ellipsis)."""
    if not text:
        return ""
    text = _XML_WRAPPER_RE.sub("", text)
    text = _BARE_TAG_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def text_from_content(content) -> str:
    """Extract plain text from a Claude message content field (str | list[block])."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
        return "\n".join(out)
    return ""


def repo_short(cwd: str) -> str:
    """Derive a short, human-readable repo label from a cwd path.

    Strips the user's home directory prefix so the result is portable across
    machines, then takes the trailing one or two segments. Used as a display
    fallback; the authoritative repo identity comes from `repo_name`.
    """
    if not cwd:
        return "unknown"
    try:
        rel = Path(cwd).resolve().relative_to(Path.home().resolve())
        parts = list(rel.parts)
    except (ValueError, OSError):
        parts = [p for p in cwd.strip("/").split("/") if p]
    if not parts:
        return cwd
    cleaned = []
    for p in parts:
        if p.startswith(".claude") or p == "worktrees" or p.startswith("agent-"):
            break
        cleaned.append(p)
    if len(cleaned) >= 2:
        return "/".join(cleaned[-2:])
    if cleaned:
        return cleaned[-1]
    return parts[-1]


# Per-cwd → git root cache. Shared by repo_name and repo_relative_path so neither
# walks the directory tree more than once per cwd. Sentinel `False` means "looked
# but no .git found"; absence of key means "not yet looked".
_GIT_ROOT_CACHE: dict[str, Path | None] = {}
_REPO_CACHE: dict[str, str] = {}


def git_root_for_cwd(cwd: str) -> Path | None:
    """Return the .git-containing root above `cwd`, or None. Cached per cwd."""
    if not cwd:
        return None
    if cwd in _GIT_ROOT_CACHE:
        return _GIT_ROOT_CACHE[cwd]
    root: Path | None = None
    p = Path(cwd)
    while p != p.parent:
        if (p / ".git").exists():
            root = p
            break
        p = p.parent
    _GIT_ROOT_CACHE[cwd] = root
    return root


def repo_name(cwd: str) -> str:
    """Resolve a cwd to its GitHub repo name (e.g. 'node-app').

    Uses `git_root_for_cwd` to find the repo, then reads `remote.origin.url` and
    parses out `owner/repo`. Falls back to the deepest non-worktree path segment.
    Cached per cwd.
    """
    if not cwd:
        return ""
    if cwd in _REPO_CACHE:
        return _REPO_CACHE[cwd]

    git_root = git_root_for_cwd(cwd)
    name = ""
    if git_root:
        try:
            url = subprocess.check_output(
                ["git", "-C", str(git_root), "config", "--get", "remote.origin.url"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
            if m:
                name = m.group(2)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        if not name:
            name = git_root.name

    if not name:
        parts = [p for p in cwd.strip("/").split("/") if p]
        cleaned = []
        for seg in parts:
            if (
                seg.startswith(".claude")
                or seg == "worktrees"
                or seg.startswith("agent-")
            ):
                break
            cleaned.append(seg)
        name = cleaned[-1] if cleaned else (parts[-1] if parts else "")

    _REPO_CACHE[cwd] = name
    return name


def repo_relative_path(full_path: str, cwd: str) -> str:
    """Convert an absolute file path to repo-relative, using `cwd` to find the root."""
    if not full_path or not cwd:
        return full_path
    git_root = git_root_for_cwd(cwd)
    if not git_root:
        return full_path
    try:
        return str(Path(full_path).resolve().relative_to(git_root.resolve()))
    except (ValueError, OSError):
        return full_path


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
