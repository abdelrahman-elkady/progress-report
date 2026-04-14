"""Extract Jira-style issue IDs from text. Used to surface tickets in the report
and as a fallback signal in correlation when branch names get squashed/rebased.
"""

from __future__ import annotations

import re

# Match common Jira ID styles: B10-12345, ABC-123, etc. Two-or-more uppercase letters/digits,
# a dash, and at least one digit. Anchored on word boundaries.
JIRA_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")


def extract_jira_ids(text: str | None) -> list[str]:
    """Return a deduped, ordered list of Jira IDs found in `text`."""
    if not text:
        return []
    seen: list[str] = []
    for m in JIRA_RE.finditer(text):
        jid = m.group(1)
        if jid not in seen:
            seen.append(jid)
    return seen
