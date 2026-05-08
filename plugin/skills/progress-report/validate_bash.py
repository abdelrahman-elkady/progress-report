#!/usr/bin/env python3
"""
PreToolUse hook for the dev-digest skill.

Reads a hook payload from stdin and decides whether to allow the Bash command.
The skill's bundled `generate.py` runs silently; any other python invocation
is downgraded to an explicit user prompt rather than hard-rejected, so users
can still approve unexpected one-off commands.

Non-python commands pass through (the harness still consults the regular
permission rules — `gh *` from the skill's allowed-tools).

Contract: print a JSON PreToolUse hook response with permissionDecision set
to "ask" and exit 0 to force a prompt. Allow by exiting 0 with no output.
"""
import json
import os
import re
import shlex
import sys

ALLOWED_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "generate.py")
_HOOK_PREFIX = "dev-digest hook"


def ask(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
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

    if not re.search(r"\bpython[23]?\b", command):
        return

    # Chained commands, redirects, substitutions, or backgrounding all fall
    # outside the skill's legitimate single-invocation use. Surface them for
    # explicit approval instead of running silently.
    suspicious_tokens = ("&&", "||", ";", "|", "`", "$(", ">", "<", "&")
    for tok in suspicious_tokens:
        if tok in command:
            ask(
                f"{_HOOK_PREFIX}: shell metacharacter {tok!r} in python "
                f"invocation — approve only if you intend this: {command}"
            )

    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        ask(f"{_HOOK_PREFIX}: could not parse command ({exc}): {command}")

    # First token must be exactly python or python3 — no env-var prefixes
    # (PYTHONPATH=...), no wrappers (sudo python3), no -c, etc.
    if not tokens or tokens[0] not in ("python", "python3"):
        ask(
            f"{_HOOK_PREFIX}: python is not the first command word "
            f"(prefix or wrapper detected) — approve to proceed: {command}"
        )

    # The bundled generate.py path must be the *script* token (index 1),
    # not just present anywhere in the argument list. Checking `in tokens`
    # would allow `python3 /tmp/evil.py /path/to/skill/generate.py` where
    # generate.py is merely passed as a CLI argument to a different script.
    script_token = tokens[1] if len(tokens) > 1 else ""
    if script_token != ALLOWED_SCRIPT:
        ask(
            f"{_HOOK_PREFIX}: python3 invocation targets a script other than "
            f"the bundled {ALLOWED_SCRIPT} — approve to proceed: {command}"
        )


if __name__ == "__main__":
    main()
