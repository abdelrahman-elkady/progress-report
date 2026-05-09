# Context — design history and architectural notes

Durable memory of the `claude-dev-digest` skill. Read this once before extending — it captures *why* the code looks the way it does so you don't re-derive the same gotchas.

> Using the skill? Read [SKILL.md](plugin/skills/claude-dev-digest/SKILL.md).
> Wondering what's missing? Read [FUTURE_PLANS.md](FUTURE_PLANS.md).

## ⚠️ Portability is non-negotiable

This skill ships to many machines. Every change must work for *anyone* who installs it.

- **No hardcoded absolute paths.** No `/Users/<someone>/...`, no baked-in usernames or install locations. Use `Path.home()`, `Path(__file__).parent`, `${CLAUDE_PLUGIN_ROOT}` (in SKILL.md content and hook commands — see the **Other gotchas** section below), or a CLI flag with a runtime-derived default.
- **No assumptions about user identity.** No hardcoded GitHub login, email, Jira account, or org. `--user` correctly defaults to `gh api user` — preserve that pattern.
- **No assumptions about environment.** No specific shell, OS, timezone, locale, or repo layout beyond `~/.claude/projects/`.
- **No hidden tooling deps.** Only `python3` (stdlib only — no pip), `gh`, and optionally the Atlassian MCP (gracefully skipped). Don't add `jq`, `rg`, `fd`, `gum`, etc.
- **MCPs and non-core agent tools must be optional with a graceful fallback**, like the Jira enrichment pass already is.
- **Defaults must be runtime-derivable.** `Path.home()`, `gh api user`, `git remote get-url`, `datetime.now().astimezone().tzinfo`. Anything "configured once and forgotten" is a portability bug.
- **Paths inside output artifacts must be the runtime user's paths**, computed at scan time. Never bake a contributor path into a template, fixture, or test.

### When in doubt, ask — don't guess

If a path looks machine-specific, a default looks user-specific, a SKILL.md sample uses an absolute path, you're about to copy a path from `git status` into committed code, or a refactor would replace a runtime value with a constant — **stop and ask via `AskUserQuestion`** with concrete options. This rule overrides the general "be terse, don't ask" disposition. Cost of asking: one round-trip. Cost of guessing wrong: every future installer breaks.

## High-level data flow

```
   ~/.claude/projects/**/*.jsonl   git remote   gh search prs (author + reviewed-by)
            │                          │                 │
            ▼                          ▼                 ▼
       lib/scanner.py  ──── lib/utils.repo_name ──── lib/github.py
            │                                              │
            │                                              ▼
            │                                       gh api .../pulls/N (concurrent + cache)
            ▼                                              │
   sessions: list[dict]      ┌──── lib/jira.extract_jira_ids
            │                │                            │
            ▼                │                            ▼
   lib/categorize.py ────────┘                    enriched PRs (authored + reviewed)
            │                                              │
            ▼                                              ▼
            └────────────► lib/correlate.py ◄──────────────┘
                                  │
                                  ▼
                          report dict (build_report)
                                  │
              ┌───────────────────┤
              ▼                   ▼
      lib/report.write_json   .write_markdown
```

The join key is **`session.repo == pr.repoShort`** (case-insensitive). Everything else is a confidence signal layered on top.

## Module map

```
progress-report/                       (repo root — dev files stay here)
├── .claude-plugin/marketplace.json    ← one-plugin marketplace catalog
├── report.schema.json                 ← formal JSON Schema for report.json (machine-readable contract)
├── REPORT_SCHEMA.md                   ← human context for dashboard consumers
├── CONTEXT.md                         ← this file
├── FUTURE_PLANS.md                    ← unimplemented improvements
└── plugin/                            ← plugin root; everything below ships to users
    ├── .claude-plugin/plugin.json     ← plugin manifest
    └── skills/claude-dev-digest/
        ├── SKILL.md                   ← manifest, run instructions, refinement passes
        ├── generate.py                ← thin CLI orchestrator (argparse, run order, --rerender)
        ├── validate_bash.py           ← Bash PreToolUse hook (resolves generate.py from __file__)
        └── lib/
            ├── utils.py               ← parse_iso, repo_name (authoritative, cached), repo_relative_path, shorten
            ├── jira.py                ← JIRA_RE = \b([A-Z][A-Z0-9]{1,9}-\d+)\b, extract_jira_ids
            ├── scanner.py             ← parse_session_file, scan_sessions; skips subagents/, isSidechain, SKIP_TYPES
            ├── categorize.py          ← keyword + tool-usage rules → CATEGORIES string
            ├── github.py              ← gh wrappers, ThreadPoolExecutor(8), persistent _pr-cache.json
            ├── correlate.py           ← scoring, hard-rejects, per-side capping
            └── report.py              ← build_report, recompute_totals, write_json/md
```

`generate.py` imports from `lib.*` only — no business logic. `recompute_totals` is shared between `build_report` and `--rerender` so totals stay consistent after in-place edits to `report.json`.

Default output dir: `~/claude-dev-digest/`. Contains `report.{json,md}` (regenerated each run) and `_pr-cache.json` (incremental).

## Key design decisions

### 1. Repo identity comes from `git remote`, not the directory name

`lib/utils.repo_name` walks up from the session's `cwd` to find `.git`, reads `remote.origin.url`, parses `owner/repo`, caches per cwd. Path-segment tricks break on monorepos like `~/code/myorg/platform-mono/services/api` where the parent dir name (`platform-mono`) doesn't match the GitHub repo (`api`).

⚠️ **Never bypass `repo_name`.** If you need a repo identifier, call `lib.utils.repo_name(cwd)`. If you ever change this function, verify against a monorepo path where the parent dir differs from the GitHub repo.

### 2. Correlation is a confidence score, not a boolean

A long session can legitimately contribute to several PRs and overlap with a sync-master PR. Boolean matching either over-collects or drops obvious matches.

Scoring (`lib/correlate.py`):

| Signal | Weight | Notes |
|---|---|---|
| `branch` | +5 | session `gitBranch` matches PR `head` ref (case-insensitive substring) |
| `files(N)` | +N+1, capped at 5 | full **repo-relative** path overlap |
| `basename(N)` | +1 | basename overlap, fallback when full-path overlap is 0 |
| `jira(IDS)` | +3 | Jira ID in session prompt/messages also appears in PR title |
| `time` | +2 | session activity inside PR open→merge window (12h leading, 2h trailing buffer) |

**Hard rules:**
- Score < 2 dropped (otherwise every same-repo same-week session matches every PR)
- Sessions starting > 2h **after** PR `mergedAt` hard-rejected (post-merge, not a contribution)
- Per-session matches capped at 8, per-PR at 12 (sorted by score desc) for readability

⚠️ **Always preserve the `reasons` field** (`{key, score, reasons: ["branch", "files(4)", "time"]}`) — load-bearing for debugging false positives.

### 3. The `jiraIds` signal solves squash/rebase aliasing

When `feat/B10-31223-bull-board` is squash-merged with a different head ref, the `branch` signal misses it. Matching the Jira ID across session prompt and PR title creates a stable identifier across the rename.

Projects without Jira-style IDs degrade gracefully — set `JIRA_RE` narrower or accept that `extract_jira_ids` returns `[]`.

### 4. The script does not call any LLM — refinement is delegated to the calling agent

Heuristic categorization (`lib/categorize.py`) only. SKILL.md documents an optional **post-generation refinement pass**: the calling Claude reads `report.json`, edits ambiguous `category` fields in place, and re-emits via `python3 generate.py --rerender --output-dir <dir>`. Same pattern for **Jira enrichment** via the Atlassian MCP.

The canonical signal for which sessions to re-inspect is **`session.needsReview`** (added in schema v1.1.0) — a boolean derived from `category`, true for the four uncertain buckets (`other`, `discarded`, `meta`, `ask`). Refinement loops should iterate `sessions` filtered on this field rather than re-deriving their own ambiguity rules. `reviewReason` provides a short per-category hint for the agent.

This keeps **zero new Python deps** and the LLM intelligence comes "for free" from whichever agent invokes the skill.

⚠️ **Never embed multi-line scripts in SKILL.md.** Anything beyond a single shell call belongs behind a flag in `generate.py`. Inline `python3 -c` blobs (a) can't be linted or tested, (b) drift from real code, (c) get mangled on LLM rewrites due to nested quoting, and (d) `validate_bash.py` forces a user prompt for any `python3` invocation that doesn't target the bundled `generate.py`, so they'd require manual approval every run even if they technically worked. The `--rerender` flag exists specifically to replace one such inline blob.

⚠️ **Don't add a hard dep on any LLM library.** If you want richer categorization, gate it behind a flag and keep the SKILL.md refinement pass as the zero-dep default.

### 5. Sessions are kept even when uncorrelated

The original ask explicitly wanted "sessions without a direct GitHub contribution" included. Uncorrelated sessions still get a category and appear in `totals.uncorrelatedSessions`. ⚠️ **Don't drop them.**

### 6. Dashboard is a separate project; `REPORT_SCHEMA.md` is the contract

Visualization is decoupled from the skill. The skill produces `report.json` (+ optional `report.md`); a separate UI-only project loads the JSON and renders it. `report.schema.json` is the formal JSON Schema that defines every field, type, and constraint. `REPORT_SCHEMA.md` is the human companion with usage notes, correlation scoring details, and consumer tips.

⚠️ **Keep `report.schema.json` in sync with code changes.** Any change to the shape of `report.json` (new fields, renamed keys, changed types) must be reflected there. It's the machine-readable source of truth for downstream consumers.

### 7. Persistent PR cache, keyed by `repo#number`

`gh api .../pulls/N/files` is the slowest part (~200ms/PR). Cache lives at `<output-dir>/_pr-cache.json`. Key intentionally excludes `mergedAt` — once merged, file list and base/head don't change. Pass `--clear-cache` if a PR was reopened and re-merged.

### 8. Concurrent fetches with `ThreadPoolExecutor(8)`

Cuts a typical 30-PR run from ~30s to ~5s. Well under GitHub's 5000/hr authenticated limit. For monthly reports (~200+ PRs), the missing piece is pagination in `lib/github.search_*`, not worker count.

### 9. Window computation is local-TZ-aware, output is all UTC

The date window is computed in local time (`datetime.now().astimezone()`) so that "last 7 days" or `--week-start sun` aligns with the user's calendar. The window is intentionally one day **wider** than `--days` (start = end - days - 1) to catch sessions started near midnight. Once computed, boundaries are converted to UTC and all timestamps in the output (`windowStart`, `windowEnd`, `minutesByDay` keys, `prsByDay` keys) are UTC. Consumers handle local-TZ display.

⚠️ **Keep window computation in local TZ, output in UTC.** Don't leak local TZ into `report.json`.

### 10. The `ask` category is shape-based, not keyword-based

Detected from session shape: duration `< 5 min`, user turns `<= 2`, no edits, `total_tools <= 3`. Keyword detection can't separate "what is X" from `exploration` because they share vocabulary; only shape (a real exploration session has many Reads/Greps) distinguishes them.

Score is **+5**, intentionally higher than every keyword rule (+2) and the read-heavy exploration bonus (+3). Keep these inequalities intact when tuning:

- `ask (+5) > exploration keyword (+2)` — "what is X" with 1 Read lands in `ask`
- `ask (+5) > exploration read-heavy (+3)` — defensive margin against threshold drift
- `total_tools <= 3` is the most fragile threshold — relaxing past ~5 swallows real exploration

If `ask` fails to fire in practice, the most likely cause is the `total_tools` cap.

## What's NOT in the JSON payload

To bound report size:

- **No** tool inputs or tool results — only tool *names* and counts
- Bash command sample capped (8 cmds × 200 chars) in `bashCmdSample`
- File **paths**, not file contents

User and assistant text messages are kept **in full** so consumers can display the entire conversation. (The original 50/50 cap made long sessions look truncated.) For raw tool inputs/outputs, parse the source `.jsonl` from `session.filePath` — always preserved.

## Other gotchas

- **`SKIP_TYPES` in `scanner.py`** is a denylist for `.jsonl` record types we never care about. New unknown record types go here, not into the parsing branches.
- **Don't try to "tighten" `allowed-tools` to a script-specific path.** The matcher does literal glob matching against the command string and does **not** expand `${CLAUDE_PLUGIN_ROOT}` or any env var. A rule like `Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/claude-dev-digest/generate.py *)` would never match; a hardcoded absolute path is non-portable; `Bash(python3 */generate.py *)` is security theater. The skill intentionally pairs broad `allowed-tools: Bash(python3 *)` with the [`validate_bash.py`](plugin/skills/claude-dev-digest/validate_bash.py) PreToolUse hook, which resolves the script path from `__file__` and lets *this* skill's `generate.py` through while returning `permissionDecision: "ask"` for anything else — so unexpected `python3` invocations force a user prompt rather than running silently. **The hook is where the bundled-vs-unknown decision is made; `allowed-tools` is just the gate that lets requests reach the hook.**

## Smoke-testing changes

No test suite yet (see [FUTURE_PLANS.md](FUTURE_PLANS.md)). Minimum manual smoke:

```bash
python3 plugin/skills/claude-dev-digest/generate.py --output-dir /tmp/pr-test --format json
python3 -c "
import json
d = json.load(open('/tmp/pr-test/report.json'))
assert d['totals']['sessions'] > 0, 'no sessions found'
assert d['totals']['prs'] >= 0
assert all('repo' in s for s in d['sessions'])
assert all('jiraIds' in p for p in d['prs'])
print('OK', d['totals'])
"
```

Failure triage: sessions == 0 → check `repo_name` resolution. PRs == 0 unexpectedly → check `gh auth status` and the date window. Both populated but correlation == 0 → the `repo == repoShort` join is failing; log both sides.

## Glossary

- **session** — one Claude Code conversation, `~/.claude/projects/<projectId>/<sessionId>.jsonl`
- **subagent session** — child sessions under `<sessionId>/subagents/`. Always skipped.
- **sidechain** — branch of conversation that doesn't continue the main thread (`isSidechain: true`). Skipped.
- **window** — date range the report covers (default: last 7 days). Computed from local TZ, stored as UTC.
- **target branches** — branches that count as "shipped" (default: `master,main`; widen via `--branches`)
- **authored PR** — user opened it, merged in window
- **reviewed PR** — user reviewed it, merged in window. Author filtered out so own PRs don't double-count.
- **correlation match** — `{key, score, reasons}` linking a session to a PR
- **uncorrelated session** — no PR matches above threshold. Still kept.
