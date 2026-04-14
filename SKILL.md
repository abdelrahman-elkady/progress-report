---
name: progress-report-skill
description: Generate a progress report correlating Claude Code sessions with the user's GitHub PR activity. Scans local Claude sessions in a configurable date window (default last 7 days), fetches the user's authored + reviewed PRs targeting master/main (configurable via --branches) via gh CLI, correlates them by repo/branch/file overlap/Jira ID/time, categorizes each session, and outputs structured JSON and Markdown. Use when the user wants to see what they worked on, get a Claude+GitHub activity summary, generate a progress digest, or correlate Claude sessions with shipped PRs. See report.schema.json for the formal contract and REPORT_SCHEMA.md for consumer guidance.
argument-hint: "[--days N | --from YYYY-MM-DD --to YYYY-MM-DD] [--user LOGIN] [--branches a,b,c] [--output-dir PATH] [--format json|md|all] [--no-reviews] [--clear-cache] [--rerender]"
allowed-tools: Bash(python3 *), Bash(gh *)
hooks:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: "python3 ${CLAUDE_SKILL_DIR}/validate_bash.py"
---

# Progress report

Generates a report of what the user worked on in Claude Code over a date window (default: last 7 days), correlated with their GitHub PR activity that landed on `master` / `main` / `integration` branches. Outputs structured JSON and Markdown — visualization is handled by a separate dashboard project that consumes `report.json` (see [REPORT_SCHEMA.md](REPORT_SCHEMA.md) for the contract). Optionally enriches the report with Jira ticket context and LLM-refined session categories.

## What it does

1. **Scans** `~/.claude/projects/**/*.jsonl` for sessions in the window. Resolves each session's repo via `git remote get-url` (not directory name) so monorepo nesting works. Subagent and sidechain sessions are skipped.
2. **Fetches** the GitHub user's authored PRs (`gh search prs --author=...`) and reviewed PRs (`gh search prs --reviewed-by=...`), enriches each with `head`, `base`, `mergedAt`, file list, and Jira IDs extracted from the title. Concurrent fetching via a thread pool, with a persistent cache at `<output-dir>/_pr-cache.json`.
3. **Filters** PRs to those merged into the configured target branches (default `master,main` — pass `--branches` to widen, e.g. `--branches master,main,integration`) inside the window.
4. **Correlates** sessions ↔ authored PRs with a confidence score:
   - `branch` (+5) — session's `gitBranch` matches PR head ref
   - `files(N)` (+N+1) — session-touched files overlap with PR files (full repo-relative path)
   - `basename(N)` (+1) — basename overlap as a softer fallback
   - `jira(IDS)` (+3) — Jira ID from session prompt/messages also appears in PR title (handles squash/rebase aliasing)
   - `time` (+2) — session activity falls inside PR's open→merge window
   - Score < 2 is dropped, sessions starting > 2h after merge are hard-rejected
5. **Categorizes** each session with keyword + tool-usage rules: `implementation`, `refactor`, `debugging`, `exploration`, `planning`, `docs`, `review`, `devops`, `testing`, `meta`, `ask`, `other`.
6. **Writes** JSON / Markdown artifacts to the output directory.

## Outputs

| File | Purpose |
|------|---------|
| `<output-dir>/report.json` | Full structured data — sessions, PRs (authored + reviewed), correlation scores+reasons, Jira IDs, totals. Conforms to [`report.schema.json`](report.schema.json); see [REPORT_SCHEMA.md](REPORT_SCHEMA.md) for usage notes. |
| `<output-dir>/report.md` | Readable digest grouped by repo and category |
| `<output-dir>/_pr-cache.json` | Persistent cache of PR detail+file fetches, keyed by `repo#number` |

Default `<output-dir>` is `~/claude-progress-report/`.

## Run

Default invocation (last 7 days, current `gh` user, all formats):

```bash
python3 ${CLAUDE_SKILL_DIR}/generate.py
```

With explicit args:

```bash
python3 ${CLAUDE_SKILL_DIR}/generate.py \
  --days 14 \
  --branches master,main,integration,develop \
  --output-dir ~/reports/sprint-23 \
  --format all
```

For an explicit date range:

```bash
python3 ${CLAUDE_SKILL_DIR}/generate.py --from 2026-03-01 --to 2026-03-31
```

After generation, tell the user the output paths and the headline counts (sessions, authored PRs, reviewed PRs, uncorrelated sessions).

## Optional refinement passes (run after generation)

These passes use Claude's own intelligence and tools — they do **not** require any new dependencies in the script.

### LLM category refinement

After running `generate.py`, the report has heuristic categories. For sessions where the heuristic is likely wrong (`category == "other"`, very long sessions, sessions whose `firstPrompt` starts with `<ide_opened_file>` and has little useful text), you should:

1. Read `<output-dir>/report.json` (Read tool).
2. For each ambiguous session, look at its `firstPrompt`, `userMessages`, and `toolCounts`. If you have access to the source `.jsonl` (path is in `session.filePath`), you can read more of the conversation.
3. Edit the session's `category` field in `report.json` in place (Edit tool) to one of: `implementation`, `refactor`, `debugging`, `exploration`, `planning`, `docs`, `review`, `devops`, `testing`, `meta`, `ask`, `other`.
4. Re-emit the markdown / re-totalled JSON via the `--rerender` mode — it reads `report.json`, recomputes totals, and writes the artifacts you ask for:

   ```bash
   python3 ${CLAUDE_SKILL_DIR}/generate.py --rerender --output-dir <output-dir>
   ```

   Pass `--format md` (or `json`) to limit which artifacts are rewritten.

Skip this pass unless the user explicitly asks for richer categorization or you can see most categories landed on `other`.

### Jira ticket enrichment via the Atlassian MCP

Each PR row in `report.json` already has a `jiraIds` array (extracted from the PR title). To enrich with real ticket data:

1. Collect all unique Jira IDs across `report.prs[*].jiraIds`.
2. For each ID, call `mcp__claude_ai_Atlassian__getJiraIssue` to fetch summary, status, type, assignee.
3. Inject the result into a new `jiraIssues` map at the top level of `report.json` (keyed by Jira ID), via the Edit tool.
4. Re-emit the markdown via `--rerender`:

   ```bash
   python3 ${CLAUDE_SKILL_DIR}/generate.py --rerender --output-dir <output-dir> --format md
   ```

**Prerequisite:** the Atlassian MCP must be connected — i.e. `mcp__claude_ai_Atlassian__getJiraIssue` is available as a tool. If it isn't, skip this pass entirely; the script-level Jira ID extraction is regex-only and the report is already complete without enrichment.

Skip this pass unless the user asks for "what business work shipped this week" or wants ticket summaries inline.

## Notes

- Requires `gh` CLI authenticated as the user whose activity should be reported.
- Date window is computed in **local time** so "this week" lines up with the user's calendar week, but all timestamps in the output are **UTC**. Consumers handle local-TZ display.
- Concurrent gh fetches use 8 workers; the per-PR cache means reruns within the same window only re-hit gh for newly-merged PRs.
- Sessions without a matching PR are kept in the report — they represent debugging, exploration, or non-merged work.

## See also

- `report.schema.json` — formal JSON Schema for `report.json` (machine-readable contract)
- `REPORT_SCHEMA.md` — human context for dashboard/visualizer consumers
- `CONTEXT.md` — design history, key decisions, and gotchas from the session that built this skill
- `FUTURE_PLANS.md` — planned improvements (direct-commit tracking, multi-week views, heatmaps, etc.)
