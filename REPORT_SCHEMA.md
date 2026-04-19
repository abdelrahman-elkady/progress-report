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

## Active duration (v1.2.0+)

`activeDurationMin` is **work-on-this-task time** — user engagement **plus** Claude working on the user's behalf. It's computed by classifying every inter-record gap and summing the credited portion.

### Gap taxonomy

Each record's `timestamp` marks when that turn completed, so every inter-record gap is classifiable from the two endpoints alone:

| `kind`         | From → To                                               | Meaning                                         | Active credit         |
|----------------|---------------------------------------------------------|-------------------------------------------------|-----------------------|
| `tool_runtime` | assistant-with-`tool_use` → user-with-`tool_result`     | Claude running a tool                           | **Full gap**          |
| `inference`    | user → assistant                                        | Claude thinking / generating                    | **Full gap**          |
| `user_pause`   | assistant (no pending tool_use) → user (real prompt)    | User reading / typing / **or away**             | **`min(gap, cap)`**   |
| `same_turn`    | same speaker, < 5 s apart                               | Logical continuation (one turn split in blocks) | Full gap              |

Only `user_pause` is capped. The default cap is **10 min** (configurable via `--user-pause-cap-min`); any excess becomes idle time.

### Session fields

| Field | Meaning |
|---|---|
| `activeDurationMin` | Sum of credited gap time, in minutes. |
| `idleSec` | Seconds stripped from active — the excess of over-cap `user_pause` gaps, in seconds. |
| `userPauseCount` | Number of `user_pause` gaps (any duration). |
| `longestUserPauseSec` | Longest single `user_pause`, seconds. |
| `gaps[]` | Only over-cap `user_pause` gaps. Each entry has `startedAt`, `endedAt`, `sec`, `kind`, `creditedSec`. `idleSec == sum(g.sec - g.creditedSec)`. |
| `segments[]` | Contiguous activity bursts split by over-cap gaps. Each has `startedAt`, `endedAt`, `sec`, `messageCount`. |

### Active-review flags

Each `Session` also carries active-duration review flags, **independent of the category review flags** (`needsReview` / `reviewReason`):

| Field | Type | Meaning |
|---|---|---|
| `needsActiveReview` | boolean | `true` when the active calculation is worth a second look (see rules below). |
| `activeReviewReason` | string \| null | One of `long_single_pause`, `high_idle_ratio`, `many_long_pauses`. Null when `needsActiveReview` is false. |

A session is flagged when **any** of:

- `longestUserPauseSec > 3600` — a single gap over 1 hour (`long_single_pause`)
- `idleSec / durationSec > 0.5` — more than half the window is stripped idle time (`high_idle_ratio`)
- `len(gaps) >= 5` — at least 5 over-cap `user_pause` gaps (`many_long_pauses`)

These flags feed the optional refinement pass documented in [SKILL.md](SKILL.md), which can edit `activeDurationMin` or `gaps` in place and re-emit via `--rerender`.

### Totals mirrors

- `totals.idleMinutesByRepo`, `totals.idleCategoryMinutes` — idle minute totals grouped the same way as `activeMinutesByRepo` / `activeCategoryMinutes`.
- `totals.minutesByDay[*].idleMinutes` — per-day idle total.

## Categories

Sessions are categorized with heuristic keyword + tool-usage rules into one of 12 values: `implementation`, `refactor`, `debugging`, `exploration`, `planning`, `docs`, `review`, `devops`, `testing`, `meta`, `ask`, `other`.

The optional LLM refinement pass (documented in [SKILL.md](SKILL.md)) can override categories in place before a `--rerender`.

### Review flags

Each `Session` carries two derived fields that signal whether the heuristic was uncertain:

| Field | Type | Meaning |
|-------|------|---------|
| `needsReview` | boolean | `true` iff `category` is one of `other`, `discarded`, `meta`, `ask` — the four buckets the heuristic is least confident about. |
| `reviewReason` | string \| null | Short hint describing what the heuristic was uncertain about. Non-null iff `needsReview` is `true`. |

**Contract for post-processing.** The optional refinement pass (see [SKILL.md](SKILL.md)) iterates `sessions` where `needsReview == true`, re-inspects each, and may overwrite `category` with any value in the enum (bidirectional — promote `discarded` → `ask`, demote `meta` → `discarded`, etc.). After edits, `--rerender` recomputes `totals.categories`, `totals.categoryMinutes`, and `minutesByDay[*].categories` from the updated per-session categories.

`needsReview` / `reviewReason` are **derived from `category` at generation time** and are not re-derived by `--rerender`. Consumers that want a fresh flag from a post-edit category should compute it themselves from the enum membership above. Pre-v1.1.0 reports passed through `--rerender` are auto-backfilled from each session's existing `category`.

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
