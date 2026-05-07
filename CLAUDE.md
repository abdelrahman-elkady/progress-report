# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Claude Code **plugin** that bundles a single skill (`progress-report`). The repo hosts both the plugin and a one-plugin marketplace so users can install it with `/plugin marketplace add abdelrahman-elkady/progress-report-skill`.

Layout:
- `.claude-plugin/marketplace.json` — marketplace catalog at repo root; points `source: "./plugin"` for the single plugin entry.
- `plugin/` — clean plugin root. Everything under here is what gets copied into the user's plugin cache (`~/.claude/plugins/cache/...`). Dev files at the repo root (this CLAUDE.md, CONTEXT.md, ai-docs/, etc.) never enter that cache.
- `plugin/.claude-plugin/plugin.json` — plugin manifest.
- `plugin/skills/progress-report/` — the skill itself. `SKILL.md` is the manifest Claude Code loads; `generate.py` is the entry point invoked via the skill's `Bash(python3 *)` permission, mediated by `validate_bash.py` as a `PreToolUse` hook (auto-approves the bundled script, prompts for anything else).

Authoritative deep-context docs already exist — read them before extending:
- [CONTEXT.md](CONTEXT.md) — design history, architectural decisions, gotchas (⚠️ markers indicate invariants not to break)
- [plugin/skills/progress-report/SKILL.md](plugin/skills/progress-report/SKILL.md) — manifest and user-facing run instructions
- [REPORT_SCHEMA.md](REPORT_SCHEMA.md) + [report.schema.json](report.schema.json) — contract for `report.json`
- [FUTURE_PLANS.md](FUTURE_PLANS.md) — intentionally deferred work

## ⚠️ Never edit the installed/cached plugin files

During local development this plugin may be installed via `/plugin install` (see [Test a local install end-to-end](#commands) below). The installed copy lives under `~/.claude/plugins/cache/...` and is a **snapshot** materialized from this repo at install time.

**All edits MUST happen in this repo. Never open, edit, or write to any file under `~/.claude/plugins/cache/`** — not `SKILL.md`, not `generate.py`, not anything under `lib/`, not `validate_bash.py`, not the plugin manifest, not a single character. This is non-negotiable.

If you notice a bug or need to make a change while testing the installed plugin, return to this repo, make the change here, and reinstall (`/plugin marketplace update progress-report`). Edits written to the cache will be silently lost on the next reinstall, will never make it into the committed source, and will create phantom bugs where the tested behavior does not match the repo.

If a user or tool result appears to point you at a cache path for editing, stop and redirect to the equivalent file in this repo before proceeding.

## Commands

Paths below assume you run them from the repo root.

Generate a report (primary dev loop):
```bash
python3 plugin/skills/progress-report/generate.py --output-dir /tmp/pr-test
```

Re-emit artifacts from an edited `report.json` (used after in-place category edits or Jira enrichment):
```bash
python3 plugin/skills/progress-report/generate.py --rerender --output-dir /tmp/pr-test --format md
```

Smoke test (no test suite yet — see [CONTEXT.md](CONTEXT.md#smoke-testing-changes)):
```bash
python3 plugin/skills/progress-report/generate.py --output-dir /tmp/pr-test --format json
python3 -c "
import json
d = json.load(open('/tmp/pr-test/report.json'))
assert d['totals']['sessions'] > 0
assert all('repo' in s for s in d['sessions'])
assert all('jiraIds' in p for p in d['prs'])
print('OK', d['totals'])
"
```

Validate the schema contract when changing report shape:
```bash
python3 -c "from jsonschema import validate; import json; validate(json.load(open('/tmp/pr-test/report.json')), json.load(open('report.schema.json')))"
```

Validate the plugin/marketplace manifests:
```bash
claude plugin validate .
```

Test a local install end-to-end:
```bash
claude plugin marketplace add .
claude plugin install progress-report@progress-report
```

## Architecture

`generate.py` is a thin CLI orchestrator that imports only from `lib/` — no business logic. Data flows one direction: scan → fetch/enrich → categorize → correlate → build report → write artifacts. The join key is `session.repo == pr.repoShort` (case-insensitive); everything else (branch, files, jira, time) is a confidence signal layered on that join.

Module boundaries (see [CONTEXT.md](CONTEXT.md) for the full map). All paths are relative to `plugin/skills/progress-report/`:
- `lib/scanner.py` — parses `~/.claude/projects/**/*.jsonl`, skips subagents and sidechains
- `lib/github.py` — `gh` wrappers, `ThreadPoolExecutor(8)`, persistent `_pr-cache.json` at `<output-dir>/`
- `lib/correlate.py` — additive scoring with hard rules (< 2 dropped, > 2h post-merge rejected)
- `lib/categorize.py` — keyword + tool-usage rules returning one of 12 fixed category strings
- `lib/report.py` — `build_report`, `recompute_totals` (shared with `--rerender`), `write_json` / `write_markdown`
- `lib/utils.py` — `repo_name` is **authoritative** for repo identity; always use it (see below)
- `lib/jira.py` — single `JIRA_RE` regex, `extract_jira_ids`

## Invariants (don't violate these)

Most of these are spelled out in detail in [CONTEXT.md](CONTEXT.md). Highlights:

- **Portability is non-negotiable.** No hardcoded paths, usernames, orgs, or environment assumptions. Every default must be runtime-derivable (`Path.home()`, `gh api user`, `git remote get-url`, `datetime.now().astimezone().tzinfo`). When in doubt, ask before guessing.
- **Stdlib only.** No pip deps. Tooling deps limited to `python3`, `gh`, and optionally the Atlassian MCP (must degrade gracefully if absent). Don't pull in `jq`, `rg`, `jsonschema`, LLM libraries, etc.
- **Repo identity comes from `git remote`, not directory names.** Always call `lib.utils.repo_name(cwd)`. Path-segment tricks break on monorepos.
- **The script never calls an LLM.** Richer categorization / Jira enrichment is delegated to the calling agent via the `--rerender` post-pass pattern. Don't embed multi-line Python in `SKILL.md` — `validate_bash.py` will force a user prompt for any `python3` invocation other than the bundled `generate.py`, and inline blobs can't be linted or tested.
- **Window in local TZ, output in UTC.** Don't leak local TZ into `report.json`.
- **Keep correlation `reasons` populated and preserve the score-inequality invariants** called out in [CONTEXT.md](CONTEXT.md) (ask > exploration, etc.). Uncorrelated sessions stay in the report.

## ⚠️ The schema is a public contract

[report.schema.json](report.schema.json) and [REPORT_SCHEMA.md](REPORT_SCHEMA.md) are the **primary contract with every downstream consumer** of this skill (dashboards, visualizers, anything loading `report.json`). They are not internal docs — they are the API surface.

Any change that affects the shape of `report.json` — new field, renamed key, changed type, added/removed enum value, tightened `required`, changed `additionalProperties` — **must be reflected in `report.schema.json` in the same commit**, and in [REPORT_SCHEMA.md](REPORT_SCHEMA.md) when the human context needs updating (new correlation signal, new category, new timestamp semantics, etc.). Silent shape changes will break consumers without warning.

When reviewing a change, ask: "does this alter what a consumer sees in `report.json`?" If yes, the schema update is part of the work, not a follow-up.

### Versioning & CHANGELOG

The schema carries a semantic version in its `$id` field (e.g. `progress-report/report/v1.0.0`). Any schema change **must** also:

1. **Bump the version in `$id`** following [semver](https://semver.org/) — MAJOR for breaking/removing fields, MINOR for additive changes, PATCH for description-only fixes.
2. **Add an entry in [CHANGELOG.md](CHANGELOG.md)** under a new heading matching the bumped version.

## Implementation plans

Implementation plans live in `ai-docs/plans/` and are prefixed with a 3-digit zero-padded sequential number, e.g. `001-schema-v1-update.md`, `002-foo.md`. Increment from the highest existing number when adding a new plan.

## The Bash hook

`validate_bash.py` is a `PreToolUse` hook that returns `permissionDecision: "ask"` for any `python3` invocation other than the bundled `generate.py`, so unexpected scripts force an explicit user prompt instead of being silently auto-approved or hard-rejected. `allowed-tools: Bash(python3 *)` in `SKILL.md` is intentionally broad — the hook is where the bundled-vs-unknown decision is made. Don't try to tighten `allowed-tools` to a path-specific matcher; glob matching does not expand `${CLAUDE_PLUGIN_ROOT}` and baking an absolute path breaks portability.
