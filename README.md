# progress-report

A [Claude Code](https://claude.ai/code) plugin that shows you what you worked on. It correlates your Claude Code sessions with your GitHub PR activity and produces a structured report.

## Prerequisites

- [Claude Code](https://claude.ai/code)
- [`gh` CLI](https://cli.github.com/) authenticated (`gh auth login`)
- Python 3.8+

## Install

In Claude Code, run:

```
/plugin marketplace add abdelrahman-elkady/progress-report
/plugin install progress-report@progress-report
```

To update later, run `/plugin marketplace update progress-report`.

## Usage

In Claude Code, run:

```
/progress-report
```

Or just ask naturally -- "what did I work on this week?", "generate a progress report", etc.

### Options

```
/progress-report --days 14
/progress-report --from 2026-03-01 --to 2026-03-31
/progress-report --branches master,main,staging
/progress-report --format md
```

| Flag | Default | Description |
|------|---------|-------------|
| `--days N` | `7` | How many days back to look |
| `--from` / `--to` | -- | Explicit date range (`YYYY-MM-DD`) |
| `--week-start DAY` | -- | Align to a weekday (e.g. `sun`) |
| `--user LOGIN` | current `gh` user | GitHub user to report on |
| `--branches` | `master,main` | Branches that count as "shipped" |
| `--output-dir PATH` | current directory (or `~/claude-progress-report/` if Claude can't resolve it) | Where to write output |
| `--format` | `all` | `json`, `md`, or `all` |
| `--no-reviews` | off | Skip reviewed PRs |

## What you get

The report is written to your current working directory by default (override with `--output-dir`):

- **`report.json`** -- structured data with your sessions, PRs (authored + reviewed), how they correlate, Jira ticket IDs, and summary totals
- **`report.md`** -- a readable Markdown digest grouped by repo and category

Each session is auto-categorized (`implementation`, `debugging`, `refactor`, `exploration`, `planning`, `docs`, `review`, `devops`, `testing`, `meta`, `ask`, `other`) and linked to the PRs it contributed to.

If the Atlassian MCP is connected, Claude can also enrich the report with real Jira ticket summaries.

## Development

Install from a local checkout instead of GitHub:

```
/plugin marketplace add /absolute/path/to/progress-report
/plugin install progress-report@progress-report
```

Refresh the installed copy after editing:

```
/plugin marketplace update progress-report
```

Uninstall:

```
/plugin uninstall progress-report@progress-report
/plugin marketplace remove progress-report
```
