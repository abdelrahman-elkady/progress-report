"""Heuristic session categorization based on first-prompt keywords + tool usage.

The calling agent (Claude) can override these by patching `session.category` after
the report is generated; see SKILL.md for the optional refinement workflow.
"""

from __future__ import annotations

import re

CATEGORIES = frozenset({
    "implementation",
    "refactor",
    "debugging",
    "exploration",
    "planning",
    "docs",
    "review",
    "devops",
    "testing",
    "meta",
    "ask",
    "other",
})

# (category, keyword regexes against the first prompt)
CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("planning", [r"\bplan\b", r"\bbrainstorm", r"\bdesign\b", r"\bproposal\b", r"approach for"]),
    (
        "debugging",
        [
            r"\bdebug",
            r"\bfix\b",
            r"\berror",
            r"\bbug\b",
            r"failing",
            r"fails?\b",
            r"\bissue\b",
            r"\bbroken\b",
            r"investigate",
            r"why is",
        ],
    ),
    ("refactor", [r"\brefactor", r"\bcleanup\b", r"clean up", r"\brewrite", r"\bsimplif", r"\brestructure"]),
    ("docs", [r"\bREADME", r"\bdocumentation", r"\bdocs?\b", r"\bdocument(\s|ing|s)\b"]),
    ("review", [r"\breview\b", r"code review", r"PR review", r"\bcritique"]),
    ("devops", [r"\bdeploy", r"\bCI\b", r"\bpipeline", r"\bdocker", r"\bkube", r"\bgithub action", r"\bworkflow"]),
    ("testing", [r"\btest", r"\bspec\b", r"\bcoverage"]),
    (
        "exploration",
        [
            r"\bexplain",
            r"\bunderstand",
            r"how does",
            r"what is",
            r"walk me through",
            r"explore",
            r"\bsearch for",
            r"\bfind\b",
        ],
    ),
    (
        "implementation",
        [r"\bimplement", r"\badd\b", r"\bcreate", r"\bbuild", r"\bfeature", r"\bnew\b"],
    ),
    (
        "meta",
        [r"\bclaude code\b", r"\bsettings\.json", r"\bhook\b", r"\bskill\b", r"keybinding", r"\btmux\b", r"\bconfig"],
    ),
]


def categorize(session: dict) -> str:
    """Pick the most likely category for a session. Returns one of `CATEGORIES`."""
    prompt = (session.get("firstPrompt") or "").lower()
    tools = session.get("toolCounts") or {}

    edit_count = sum(tools.get(t, 0) for t in ("Edit", "Write", "MultiEdit"))
    read_count = tools.get("Read", 0)
    grep_count = tools.get("Grep", 0)
    bash_count = tools.get("Bash", 0)
    total_tools = sum(tools.values())
    user_msg_count = session.get("userMsgCount", 0) or 0
    duration_min = session.get("durationMin", 0) or 0

    scores: dict[str, int] = {}
    for cat, patterns in CATEGORY_RULES:
        for p in patterns:
            if re.search(p, prompt):
                scores[cat] = scores.get(cat, 0) + 2

    # Quick Q&A / lookup: short, few turns, no edits, barely any tool use.
    # Strong score so it beats keyword-based exploration ("what is", "explain").
    if (
        edit_count == 0
        and total_tools <= 3
        and user_msg_count <= 2
        and duration_min < 5
    ):
        scores["ask"] = scores.get("ask", 0) + 5

    if edit_count == 0 and read_count + grep_count > 5:
        scores["exploration"] = scores.get("exploration", 0) + 3
    if edit_count >= 3:
        scores["implementation"] = scores.get("implementation", 0) + 2
    if bash_count > 5 and edit_count == 0:
        scores["devops"] = scores.get("devops", 0) + 1

    cwd = session.get("cwd", "")
    if "/dotfiles" in cwd or "/.claude" in cwd:
        scores["meta"] = scores.get("meta", 0) + 1

    if not scores:
        if edit_count > 0:
            return "implementation"
        if read_count + grep_count > 0:
            return "exploration"
        return "other"
    result = max(scores.items(), key=lambda kv: kv[1])[0]
    assert result in CATEGORIES, f"categorizer produced unknown category: {result!r}"
    return result
