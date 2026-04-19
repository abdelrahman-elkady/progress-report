---
name: progress-report-skill
description: Generate a progress report correlating Claude Code sessions with the user's GitHub PR activity. Scans local Claude sessions in a configurable date window (default last 7 days), fetches the user's authored + reviewed PRs targeting master/main (configurable via --branches) via gh CLI, correlates them by repo/branch/file overlap/Jira ID/time, categorizes each session, and outputs structured JSON and Markdown. Use when the user wants to see what they worked on, get a Claude+GitHub activity summary, generate a progress digest, or correlate Claude sessions with shipped PRs. See report.schema.json for the formal contract and REPORT_SCHEMA.md for consumer guidance.
argument-hint: "[--days N | --from YYYY-MM-DD --to YYYY-MM-DD] [--week-start mon|tue|wed|thu|fri|sat|sun] [--user LOGIN] [--branches a,b,c] [--output-dir PATH] [--format json|md|all] [--no-reviews] [--clear-cache] [--user-pause-cap-min MINUTES] [--rerender]"
allowed-tools: Bash(python3 *), Bash(gh *)
hooks:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: "python3 ${CLAUDE_SKILL_DIR}/validate_bash.py"
---

# Progress report

Correlates Claude Code sessions with the user's merged GitHub PRs over a date window (default: last 7 days) and writes structured JSON + Markdown. Visualization is out of scope — `report.json` is consumed by a separate dashboard (see [REPORT_SCHEMA.md](REPORT_SCHEMA.md)).

## What it does

1. **Scans** `~/.claude/projects/**/*.jsonl` for sessions in the window. Repo is resolved via `git remote get-url` (not directory name), so monorepo nesting works. Subagent and sidechain sessions are skipped.
2. **Fetches** authored (`gh search prs --author=...`) and reviewed (`gh search prs --reviewed-by=...`) PRs, enriched with head/base refs, `mergedAt`, file lists, and Jira IDs extracted from titles. Concurrent via thread pool; persistent cache at `<output-dir>/_pr-cache.json`.
3. **Filters** PRs to those merged into the target branches inside the window.
4. **Correlates** sessions ↔ authored PRs with a confidence score:
   - `branch` (+5) — session's `gitBranch` matches PR head ref
   - `files(N)` (+N+1) — session-touched files overlap with PR files (full repo-relative path)
   - `basename(N)` (+1) — basename overlap as a softer fallback
   - `jira(IDS)` (+3) — Jira ID from session prompt/messages also appears in PR title (handles squash/rebase aliasing)
   - `time` (+2) — session activity falls inside PR's open→merge window
   - Score < 2 is dropped; sessions starting > 2h after merge are hard-rejected.
5. **Categorizes** each session with keyword + tool-usage rules: `implementation`, `refactor`, `debugging`, `exploration`, `planning`, `docs`, `review`, `devops`, `testing`, `meta`, `ask`, `other`.
6. **Writes** JSON / Markdown artifacts.

## Outputs

| File | Purpose |
|------|---------|
| `<output-dir>/report.json` | Full structured data — sessions, PRs (authored + reviewed), correlation scores+reasons, Jira IDs, totals. Conforms to [`report.schema.json`](report.schema.json); see [REPORT_SCHEMA.md](REPORT_SCHEMA.md). |
| `<output-dir>/report.md` | Readable digest grouped by repo and category |
| `<output-dir>/_pr-cache.json` | Persistent cache of PR detail+file fetches, keyed by `repo#number` |

Default `<output-dir>` is `~/claude-progress-report/`.

## Run

### Interactive prompt (no arguments)

If invoked with **no arguments**, collect preferences via a single `AskUserQuestion` call (4 questions — the tool's per-call cap) before running `generate.py`. Present the default as the first option on each question. Build the command using only the flags the user changed — if they keep a default, **omit that flag entirely** so the script's own default applies.

PR reviews are always included (authored + reviewed). Users who want authored-only can pass `--no-reviews` explicitly.

Questions:

1. **Time window** (header: `Window`)
   - `Last 7 days (default)` → no flag
   - `Last 14 days` → `--days 14`
   - `Last 30 days` → `--days 30`
   - Other: parse as `--days N`, or `--from YYYY-MM-DD --to YYYY-MM-DD` if a range.

2. **Output format** (header: `Format`)
   - `JSON + Markdown (default)` → no flag
   - `Markdown only` → `--format md`
   - `JSON only` → `--format json`

3. **Target branches** (header: `Branches`)
   - `master, main (default)` → no flag
   - `master, main, staging` → `--branches master,main,staging`
   - `master, main, develop` → `--branches master,main,develop`
   - Other: pass verbatim as `--branches <value>`.

4. **Output directory** (header: `Output dir`)
   - `<cwd> (default)` → `--output-dir <cwd>` — resolve to the runtime working directory so the report lands next to the current project.
   - `~/claude-progress-report` → no flag (script default).
   - Other: pass verbatim as `--output-dir <path>`.

If the user passed any argument or natural-language hint (e.g. `--days 14`, "last 30 days", an output path), skip the prompt and translate their input directly into flags.

### Default invocation

```bash
python3 ${CLAUDE_SKILL_DIR}/generate.py
```

With explicit args:

```bash
python3 ${CLAUDE_SKILL_DIR}/generate.py \
  --days 14 \
  --branches master,main,staging,develop \
  --output-dir ~/reports/sprint-23 \
  --format all
```

For an explicit date range:

```bash
python3 ${CLAUDE_SKILL_DIR}/generate.py --from 2026-03-01 --to 2026-03-31
```

After generation, tell the user the output paths and the headline counts (sessions, authored PRs, reviewed PRs, uncorrelated sessions).

## Refinement passes (run after generation)

Both passes below run **by default** after `generate.py` finishes. Skip only if the user explicitly opts out.

### LLM category refinement

`generate.py` sets `needsReview: true` with a `reviewReason` on the four uncertain buckets (`other`, `discarded`, `meta`, `ask`). Use these as the canonical signal — do not re-derive ambiguity rules. **Always run this pass** when any session has `needsReview: true`.

1. Read `<output-dir>/report.json`. For each session with `needsReview: true`, inspect `firstPrompt`, `userMessages`, `toolCounts`, and `filesTouched`. Read the source `.jsonl` at `session.filePath` if still ambiguous.
2. Decide bidirectionally — flagged sessions can be promoted *or* demoted. `reviewReason` says what the heuristic was unsure about:

   | `reviewReason` (current `category`) | What to consider |
   |---|---|
   | `other` | Heuristic gave up. Re-categorize to whichever bucket actually fits — frequently `implementation`, `exploration`, or `debugging`. |
   | `discarded` | Trivial-shape match. **Promote** to e.g. `ask` or `exploration` if the short session was actually meaningful. |
   | `meta` (keyword match) | Mentioned `claude code` / `hook` / `skill` / `config` but may be trivial. **Demote** to `discarded` if no real work happened. |
   | `ask` (shape-based match) | Short, few-turn, no-edits. **Demote** to `discarded` if empty, or `exploration` if many reads. |

3. Edit `category` in place (Edit tool) to one of: `implementation`, `refactor`, `debugging`, `exploration`, `planning`, `docs`, `review`, `devops`, `testing`, `meta`, `ask`, `discarded`, `other`. No need to clear `needsReview` / `reviewReason` — `--rerender` only recomputes totals.
4. Re-emit artifacts:

   ```bash
   python3 ${CLAUDE_SKILL_DIR}/generate.py --rerender --output-dir <output-dir>
   ```

   Pass `--format md` (or `json`) to limit which artifacts are rewritten.

Skip only if the user explicitly opts out, or if no sessions are flagged.

### Jira ticket enrichment via the Atlassian MCP

Each PR row in `report.json` has a `jiraIds` array extracted from the PR title. **Always run this pass** when the Atlassian MCP is connected (`mcp__claude_ai_Atlassian__getJiraIssue` available) and `report.prs[*].jiraIds` contains any IDs:

1. Collect all unique Jira IDs across `report.prs[*].jiraIds`.
2. For each ID, call `mcp__claude_ai_Atlassian__getJiraIssue` for summary, status, type, assignee.
3. Inject the result into a new top-level `jiraIssues` map in `report.json` (keyed by Jira ID) via the Edit tool.
4. Re-emit markdown:

   ```bash
   python3 ${CLAUDE_SKILL_DIR}/generate.py --rerender --output-dir <output-dir> --format md
   ```

Skip only if the Atlassian MCP is not connected (the regex-level Jira ID extraction stands on its own) or the user explicitly opts out.

## Notes

- Requires `gh` CLI authenticated as the target user.
- Date window is computed in **local time** (so "this week" matches the user's calendar week), but all output timestamps are **UTC**. Consumers handle local-TZ display.
- Concurrent gh fetches use 8 workers; the per-PR cache means reruns only re-hit gh for newly-merged PRs.
- Sessions without a matching PR are kept — they represent debugging, exploration, or non-merged work.

## See also

- [`report.schema.json`](report.schema.json) — formal JSON Schema contract for `report.json`
- [REPORT_SCHEMA.md](REPORT_SCHEMA.md) — consumer guidance
- [CONTEXT.md](CONTEXT.md) — design history, decisions, gotchas
- [FUTURE_PLANS.md](FUTURE_PLANS.md) — deferred work
