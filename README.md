# claude-dev-digest

A [Claude Code](https://claude.ai/code) plugin that shows you what you worked on. It correlates your Claude Code sessions with your GitHub PR activity and produces a structured report.

## Prerequisites

- [Claude Code](https://claude.ai/code)
- [`gh` CLI](https://cli.github.com/) authenticated (`gh auth login`)
- Python 3.8+

## Install

In Claude Code, run:

```
/plugin marketplace add abdelrahman-elkady/progress-report
/plugin install claude-dev-digest@claude-dev-digest
```

> The marketplace lives in the `abdelrahman-elkady/progress-report` GitHub repo (legacy name; will be renamed in a follow-up). The installed plugin is `claude-dev-digest`.

To update later, run `/plugin marketplace update claude-dev-digest`.

## Usage

In Claude Code, run:

```
/claude-dev-digest
```

Or just ask naturally -- "what did I work on this week?", "generate a dev digest", etc.

### Options

```
/claude-dev-digest --days 14
/claude-dev-digest --from 2026-03-01 --to 2026-03-31
/claude-dev-digest --branches master,main,staging
/claude-dev-digest --format md
```

| Flag | Default | Description |
|------|---------|-------------|
| `--days N` | `7` | How many days back to look |
| `--from` / `--to` | -- | Explicit date range (`YYYY-MM-DD`) |
| `--week-start DAY` | -- | Align to a weekday (e.g. `sun`) |
| `--user LOGIN` | current `gh` user | GitHub user to report on |
| `--branches` | `master,main` | Branches that count as "shipped" |
| `--output-dir PATH` | current directory (or `~/claude-dev-digest/` if Claude can't resolve it) | Where to write output |
| `--format` | `all` | `json`, `md`, or `all` |
| `--no-reviews` | off | Skip reviewed PRs |

## What you get

The report is written to your current working directory by default (override with `--output-dir`):

- **`report.json`** -- structured data with your sessions, PRs (authored + reviewed), how they correlate, Jira ticket IDs, and summary totals
- **`report.md`** -- a readable Markdown digest grouped by repo and category

Each session is auto-categorized (`implementation`, `debugging`, `refactor`, `exploration`, `planning`, `docs`, `review`, `devops`, `testing`, `meta`, `ask`, `other`) and linked to the PRs it contributed to.

If the Atlassian MCP is connected, Claude can also enrich the report with real Jira ticket summaries.

To visualize `report.json` in a browser, see [weekly-report-visualizer](https://github.com/abdelrahman-elkady/weekly-report-visualizer).

## Development

Install from a local checkout instead of GitHub:

```
/plugin marketplace add /absolute/path/to/progress-report
/plugin install claude-dev-digest@claude-dev-digest
```

Refresh the installed copy after editing:

```
/plugin marketplace update claude-dev-digest
```

Uninstall:

```
/plugin uninstall claude-dev-digest@claude-dev-digest
/plugin marketplace remove claude-dev-digest
```
