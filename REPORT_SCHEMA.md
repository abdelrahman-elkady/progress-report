# Report schema

Contract for consumers of the `progress-report-skill` skill's output artifacts.

> **Field definitions live in [`report.schema.json`](report.schema.json)** — the formal JSON Schema. This document covers the human context that a schema file can't express: what the fields mean in practice, how to interpret them, and what to watch out for.
>
> Building the skill itself? Read [CONTEXT.md](CONTEXT.md).
> Running it? Read [SKILL.md](SKILL.md).

## Output artifacts

| File | Always produced | Description |
|------|-----------------|-------------|
| `report.json` | Yes (unless `--format md`) | Full structured data. This is the primary artifact and the one dashboards should consume. Conforms to `report.schema.json`. |
| `report.md` | Yes (unless `--format json`) | Human-readable Markdown digest. Lossy — not all fields are rendered. |
| `_pr-cache.json` | Yes | Internal cache of PR detail fetches. **Not a public contract** — do not consume. |

## Timestamps

All timestamps in `report.json` are **UTC ISO 8601**. This includes `generatedAt`, `windowStart`, `windowEnd`, all session/PR timestamps, and the date keys in `minutesByDay` / `prsByDay` (which are UTC `YYYY-MM-DD` strings). Consumers are responsible for converting to local time for display.

The scan window is computed from the user's local timezone (so "last 7 days" aligns with their calendar), but it's stored as UTC in the output.

## Correlation scoring

Sessions are correlated to **authored PRs only** (not reviewed PRs) using an additive confidence score. Matches below score 2 are dropped.

| Signal | Weight | Description |
|--------|--------|-------------|
| `branch` | +5 | Session's `gitBranch` matches PR `head` ref (case-insensitive substring) |
| `files(N)` | +N+1 (max 5) | N full repo-relative file paths overlap between session and PR |
| `basename(N)` | +1 | N basenames overlap (softer fallback when full-path overlap is 0) |
| `jira(IDS)` | +3 | Jira ID(s) appear in both the session and the PR title |
| `time` | +2 | Session activity falls within the PR's open-to-merge window (12h leading, 2h trailing buffer) |

Hard rules:
- Sessions starting > 2h **after** PR merge are rejected (post-merge, not a contribution)
- Per-session matches capped at 8, per-PR at 12 (sorted by score desc)

The `reasons` array on each `CorrelationMatch` lists exactly which signals fired (e.g. `["branch", "files(4)", "time"]`). This is load-bearing for debugging false positives.

## Categories

Sessions are categorized with heuristic keyword + tool-usage rules into one of 12 values: `implementation`, `refactor`, `debugging`, `exploration`, `planning`, `docs`, `review`, `devops`, `testing`, `meta`, `ask`, `other`.

The optional LLM refinement pass (documented in [SKILL.md](SKILL.md)) can override categories in place before a `--rerender`.

## Notes for dashboard consumers

- **`minutesByDay` keys are pre-seeded** for every date in the window (including days with no activity), so you can iterate them directly without filling gaps.
- **`minutesByDay` insertion order is calendar order** — `Object.entries()` / `Object.keys()` in JS will yield dates in time order.
- **Reviewed PRs have an empty `correlatedSessions`** array by design — correlation only runs against authored PRs.
- **`filePath` is a local filesystem path** from the machine that generated the report. Don't use it for display — use `sessionId` instead.
- **The `_pr-cache.json` file is not part of this contract.** It's an internal optimization. Don't depend on its structure.
- To bound payload size: tool inputs/outputs are **not** captured — only tool names and counts. Bash commands are sampled (8 max, 200 chars each). User and assistant messages are kept in full.

## Using the schema

Validate a report:
```bash
# Python (jsonschema library)
pip install jsonschema
python3 -c "
from jsonschema import validate
import json
validate(json.load(open('report.json')), json.load(open('report.schema.json')))
print('valid')
"

# Node.js (ajv library)
npx ajv validate -s report.schema.json -d report.json
```

Generate TypeScript types:
```bash
npx json-schema-to-typescript report.schema.json > report.d.ts
```
