# progress-report-skill

A [Claude Code](https://claude.ai/code) skill that shows you what you worked on. It correlates your Claude Code sessions with your GitHub PR activity and produces a structured report.

## Prerequisites

- [Claude Code](https://claude.ai/code)
- [`gh` CLI](https://cli.github.com/) authenticated (`gh auth login`)
- Python 3.8+

## Install

Copy this directory into your Claude Code skills folder:

```bash
cp -r progress-report-skill ~/.claude/skills/progress-report-skill
```

Then ask Claude to fix the hook path — the skill's pre-tool hook uses `${CLAUDE_SKILL_DIR}` which doesn't resolve as a shell variable at hook execution time:

```
In the progress-report-skill SKILL.md, replace ${CLAUDE_SKILL_DIR} in the hook
command with the absolute path of the skill directory.
```

Claude Code discovers skills automatically after that.

## Usage

In Claude Code, run:

```
/progress-report-skill
```

Or just ask naturally -- "what did I work on this week?", "generate a progress report", etc.

### Options

```
/progress-report-skill --days 14
/progress-report-skill --from 2026-03-01 --to 2026-03-31
/progress-report-skill --branches master,main,integration
/progress-report-skill --format md
```

| Flag | Default | Description |
|------|---------|-------------|
| `--days N` | `7` | How many days back to look |
| `--from` / `--to` | -- | Explicit date range (`YYYY-MM-DD`) |
| `--week-start DAY` | -- | Align to a weekday (e.g. `sun`) |
| `--user LOGIN` | current `gh` user | GitHub user to report on |
| `--branches` | `master,main` | Branches that count as "shipped" |
| `--output-dir PATH` | `~/claude-progress-report/` | Where to write output |
| `--format` | `all` | `json`, `md`, or `all` |
| `--no-reviews` | off | Skip reviewed PRs |

## What you get

The report is written to `~/claude-progress-report/` by default:

- **`report.json`** -- structured data with your sessions, PRs (authored + reviewed), how they correlate, Jira ticket IDs, and summary totals
- **`report.md`** -- a readable Markdown digest grouped by repo and category

Each session is auto-categorized (`implementation`, `debugging`, `refactor`, `exploration`, `planning`, `docs`, `review`, `devops`, `testing`, `meta`, `ask`, `other`) and linked to the PRs it contributed to.

If the Atlassian MCP is connected, Claude can also enrich the report with real Jira ticket summaries.
