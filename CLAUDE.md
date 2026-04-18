# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Claude Code **skill** (not a standalone Python package). It ships as a directory that users copy into `~/.claude/skills/`. `SKILL.md` is the manifest Claude Code loads; `generate.py` is the entry point invoked via the skill's `Bash(python3 *)` permission, gated by `validate_bash.py` as a `PreToolUse` hook.

Authoritative deep-context docs already exist — read them before extending:
- [CONTEXT.md](CONTEXT.md) — design history, architectural decisions, gotchas (⚠️ markers indicate invariants not to break)
- [SKILL.md](SKILL.md) — manifest and user-facing run instructions
- [REPORT_SCHEMA.md](REPORT_SCHEMA.md) + [report.schema.json](report.schema.json) — contract for `report.json`
- [FUTURE_PLANS.md](FUTURE_PLANS.md) — intentionally deferred work

## Commands

Generate a report (primary dev loop):
```bash
python3 generate.py --output-dir /tmp/pr-test
```

Re-emit artifacts from an edited `report.json` (used after in-place category edits or Jira enrichment):
```bash
python3 generate.py --rerender --output-dir /tmp/pr-test --format md
```

Smoke test (no test suite yet — see [CONTEXT.md](CONTEXT.md#smoke-testing-changes)):
```bash
python3 generate.py --output-dir /tmp/pr-test --format json
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

## Architecture

`generate.py` is a thin CLI orchestrator that imports only from `lib/` — no business logic. Data flows one direction: scan → fetch/enrich → categorize → correlate → build report → write artifacts. The join key is `session.repo == pr.repoShort` (case-insensitive); everything else (branch, files, jira, time) is a confidence signal layered on that join.

Module boundaries (see [CONTEXT.md](CONTEXT.md) for the full map):
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
- **The script never calls an LLM.** Richer categorization / Jira enrichment is delegated to the calling agent via the `--rerender` post-pass pattern. Don't embed multi-line Python in `SKILL.md` — `validate_bash.py` will reject it, and it can't be linted or tested.
- **Window in local TZ, output in UTC.** Don't leak local TZ into `report.json`.
- **Keep correlation `reasons` populated and preserve the score-inequality invariants** called out in [CONTEXT.md](CONTEXT.md) (ask > exploration, etc.). Uncorrelated sessions stay in the report.

## ⚠️ The schema is a public contract

[report.schema.json](report.schema.json) and [REPORT_SCHEMA.md](REPORT_SCHEMA.md) are the **primary contract with every downstream consumer** of this skill (dashboards, visualizers, anything loading `report.json`). They are not internal docs — they are the API surface.

Any change that affects the shape of `report.json` — new field, renamed key, changed type, added/removed enum value, tightened `required`, changed `additionalProperties` — **must be reflected in `report.schema.json` in the same commit**, and in [REPORT_SCHEMA.md](REPORT_SCHEMA.md) when the human context needs updating (new correlation signal, new category, new timestamp semantics, etc.). Silent shape changes will break consumers without warning.

When reviewing a change, ask: "does this alter what a consumer sees in `report.json`?" If yes, the schema update is part of the work, not a follow-up.

### Versioning & CHANGELOG

The schema carries a semantic version in its `$id` field (e.g. `progress-report-skill/report/v1.0.0`). Any schema change **must** also:

1. **Bump the version in `$id`** following [semver](https://semver.org/) — MAJOR for breaking/removing fields, MINOR for additive changes, PATCH for description-only fixes.
2. **Add an entry in [CHANGELOG.md](CHANGELOG.md)** under a new heading matching the bumped version.

## Implementation plans

Implementation plans live in `ai-docs/plans/` and are prefixed with a 3-digit zero-padded sequential number, e.g. `001-schema-v1-update.md`, `002-foo.md`. Increment from the highest existing number when adding a new plan.

## The Bash hook

`validate_bash.py` is a `PreToolUse` hook that rejects any `python3` invocation other than `generate.py`. `allowed-tools: Bash(python3 *)` in `SKILL.md` is intentionally broad — the hook is the real security boundary. Don't try to tighten `allowed-tools` to a path-specific matcher; glob matching does not expand `${CLAUDE_SKILL_DIR}` and baking an absolute path breaks portability.
