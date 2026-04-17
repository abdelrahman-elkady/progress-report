#!/usr/bin/env python3
"""
PreToolUse hook for the progress-report-skill skill.

Reads a hook payload from stdin and decides whether to allow the Bash command.
The skill only legitimately invokes python3 to run its bundled generate.py;
any other python invocation while the skill is active is rejected.

Non-python commands pass through (the harness still consults the regular
permission rules — `gh *` from the skill's allowed-tools).

Block contract: print a JSON PreToolUse hook response with permissionDecision
set to "deny" and exit 0. Allow by exiting 0 with no output.
"""
import json
import os
import re
import shlex
import sys

ALLOWED_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generate.py")
_HOOK_PREFIX = "progress-report-skill hook"


def deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Malformed payload — don't block, let the harness handle it
        return

    if payload.get("tool_name") != "Bash":
        return

    command = payload.get("tool_input", {}).get("command", "")

    # Only constrain commands that would invoke python at all.
    if not re.search(r"\bpython[23]?\b", command):
        return

    # Reject anything that could chain commands, redirect, substitute, or
    # background. The skill's only legitimate python use is a single direct
    # foreground invocation, so we can be strict.
    forbidden_tokens = ("&&", "||", ";", "|", "`", "$(", ">", "<", "&")
    for tok in forbidden_tokens:
        if tok in command:
            deny(
                f"{_HOOK_PREFIX}: forbidden shell metacharacter "
                f"{tok!r} in python invocation: {command}"
            )

    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        deny(f"{_HOOK_PREFIX}: could not parse command ({exc}): {command}")

    # First token must be exactly python or python3 — no env-var prefixes
    # (PYTHONPATH=...), no wrappers (sudo python3), no -c, etc.
    if not tokens or tokens[0] not in ("python", "python3"):
        deny(
            f"{_HOOK_PREFIX}: python must be the first command word "
            f"with no prefix or wrapper: {command}"
        )

    # The bundled generate.py path must be the *script* token (index 1),
    # not just present anywhere in the argument list. Checking `in tokens`
    # would allow `python3 /tmp/evil.py /path/to/skill/generate.py` where
    # generate.py is merely passed as a CLI argument to a different script.
    script_token = tokens[1] if len(tokens) > 1 else ""
    if script_token != ALLOWED_SCRIPT:
        deny(
            f"{_HOOK_PREFIX}: only {ALLOWED_SCRIPT} may be run "
            f"via python3 while this skill is active. Refusing: {command}"
        )

    # Validation passed — exit 0 with no output to allow.


if __name__ == "__main__":
    main()
