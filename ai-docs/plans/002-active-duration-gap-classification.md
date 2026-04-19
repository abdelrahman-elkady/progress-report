# Active duration via gap classification

## Context

Today [lib/scanner.py:57-75](../../lib/scanner.py#L57-L75) computes `activeDurationMin` by walking consecutive user/assistant timestamps and summing every gap ≤ `idle_threshold_min` (default 45 min). The model is binary and blind:

- A 44-min gap where the user went to lunch → 44 active minutes.
- A 46-min gap where Claude ran a long tool → 0 active minutes.
- A single threshold conflates **user-away time** with **Claude working on your behalf**, even though they're structurally distinguishable in the raw `.jsonl`.
- Nothing about the idle cuts is preserved in `report.json`, so consumers can't audit, visualize, or correct.

The sample report shows the cost: [plan-and-dev-assets/report.json](../../plan-and-dev-assets/report.json) has sessions like `breadfast/nestjs-boilerplate` at 14 276 min wall / 86.9 min active — a correct but opaque 164× reduction; the session at [line 1807](../../plan-and-dev-assets/report.json#L1807) shows 207 min wall / 54.7 min active, where a single 2h 33min gap was correctly dropped but many shorter 15–40 min gaps remain fully credited as active without evidence they were.

The desired semantic (confirmed): **work-on-this-task time** = user engagement **plus** Claude working on the user's behalf (tool runtime + inference). The algorithm must strip only the genuine "user was away" time, and surface enough information for the main agent to re-review outliers during `--rerender`.

## Approach

Replace the single-threshold subtraction with **gap classification + selective capping**, then emit the full gap/segment breakdown in the report so consumers (and the main agent's `--rerender` pass) can audit and adjust.

### Gap taxonomy

A jsonl record's `timestamp` marks when that turn *completed* (records are written on completion). Each inter-record gap is therefore classifiable from the two endpoints alone:

| Gap kind | From → To | Meaning | Active credit |
|---|---|---|---|
| `tool_runtime` | assistant (contains `tool_use`) → user (contains `tool_result`, linked via `sourceToolAssistantUUID`) | Claude is running a tool | **Full gap** |
| `inference` | user → assistant | Claude is thinking / generating | **Full gap** |
| `user_pause` | assistant (no pending `tool_use`, i.e. `stop_reason` ≠ `tool_use`) → user (real prompt, not `tool_result`) | User is reading / typing / **or away** | **`min(gap, cap)`** — cap = 10 min |
| `same_turn` | same speaker, < 5 s apart (e.g. thinking → text → tool_use blocks split into separate records) | Not a real gap | Full gap, no classification needed |

Only `user_pause` is a candidate for idle stripping. Tool runtime and inference are *always* fully credited — this directly addresses the biggest current bias (long builds / background agent runs being identical to coffee breaks).

Classification signals available on every record (verified by inspecting the jsonl):

- `type` ∈ {`user`, `assistant`}
- `message.content` — array of blocks; check for `tool_use` / `tool_result` block presence
- `message.stop_reason` on assistant records (`"tool_use"` vs `"end_turn"`) for cheap disambiguation
- `sourceToolAssistantUUID` on user tool_result records for explicit linkage
- `isSidechain`, `isMeta` — already filtered today

### Cap policy for `user_pause`

**Hard cap at 10 min.** A user_pause of `g` seconds contributes `min(g, 600)` seconds to active time; any excess (`g − 600` if > 600) is emitted as an idle segment.

Rationale: 10 min is a generous ceiling on "reasonable read + think + type" for a single reply. Longer pauses almost always include real away-time; by capping (not dropping) we still credit the engaged portion at the edges of the pause.

### Schema additions (report.schema.json)

Add to each session object (all additive, schema MINOR bump):

```json
{
  "idleSec": 0,
  "userPauseCount": 0,
  "longestUserPauseSec": 0,
  "gaps": [
    { "startedAt": "ISO8601", "endedAt": "ISO8601", "sec": 0, "kind": "user_pause", "creditedSec": 600 }
  ],
  "segments": [
    { "startedAt": "ISO8601", "endedAt": "ISO8601", "sec": 0, "messageCount": 0 }
  ],
  "needsActiveReview": false,
  "reviewReason": null
}
```

- `gaps` contains **only `user_pause` gaps whose raw `sec` > cap** — not every inter-message gap, or the array would balloon. `creditedSec` records how many seconds of that gap were credited (always equals the cap for over-cap gaps, so consumers can reconstruct `idleSec`).
- `segments` is the contiguous bursts between over-cap gaps. Useful for timeline visualizers; cheap to compute once we have `gaps`.
- `activeDurationMin` keeps the same name but its formula is now `(sum of all gaps' credited time) / 60` — tool runtime + inference fully counted, user pauses capped.

### Flagging for `--rerender` review

A session sets `needsActiveReview: true` (with a short `reviewReason` string) when **any** of:

- `longestUserPauseSec > 3600` (single gap over 1 hour)
- `idleRatio > 0.5` where `idleRatio = idleSec / durationSec`
- At least 5 `user_pause` gaps each exceeding 10 min (many medium pauses — likely interleaved with other work)

The main agent, during `--rerender`, reads the conversation around each suspect gap (the `gaps[]` list gives exact timestamps to locate) and can edit `activeDurationMin` and/or drop specific entries before `build_report` → `recompute_totals` fires. This reuses the existing Jira-enrichment / category-override pattern described in [CONTEXT.md](../../CONTEXT.md) and [CLAUDE.md](../../CLAUDE.md).

## Files to change

- **[lib/scanner.py](../../lib/scanner.py)** — core algorithm change:
  - Replace `_active_duration_minutes` with `_classify_and_score_gaps(records)` returning `(active_sec, idle_sec, gaps, segments, longest_user_pause_sec, user_pause_count)`.
  - Collect per-record tuples `(ts, speaker, has_tool_use, has_tool_result)` during the existing single-pass read loop (line 107-170) — no extra file reads.
  - Collapse same-speaker records within 5 s into logical turns before classification (purely a pairwise walk, O(n)).
  - Populate the new fields on the returned dict at [scanner.py:196-220](../../lib/scanner.py#L196-L220).

- **[lib/report.py](../../lib/report.py)** — propagate new fields through `build_report`; `recompute_totals` should include `idleSec` aggregates by repo/day/category (mirroring the existing `activeMinutesByRepo` pattern so downstream consumers get the stripped-time totals).

- **[report.schema.json](../../report.schema.json)** — add the new fields under the session definition with appropriate types and `required` status (`gaps`/`segments` required but may be empty; `needsActiveReview` required; `reviewReason` nullable). Bump `$id` **MINOR** — the new fields are additive with no removals or enum changes, and `activeDurationMin` keeps its name and type. The formula change for `activeDurationMin` is a semantic shift (tool runtimes now always credited; user pauses capped at 10 min instead of dropped at 45 min), but the field's meaning ("active duration in minutes") hasn't changed.

- **[REPORT_SCHEMA.md](../../REPORT_SCHEMA.md)** — document the new fields, the gap taxonomy, the cap policy, and the flagging rules. This is the consumer-facing contract per [CLAUDE.md §"The schema is a public contract"](../../CLAUDE.md).

- **[CHANGELOG.md](../../CHANGELOG.md)** — new version entry. The formula change for `activeDurationMin` must be **loudly highlighted** (bold callout, explicit before/after description, migration note for consumers that cached historical values) — numbers will shift on re-run and downstream dashboards need to know. Additive fields go in a standard "Added" section below the highlight.

- **[generate.py](../../generate.py)** — rename `--idle-threshold` → `--user-pause-cap-min` (default 10). The old flag is deleted; the skill ships as a bundle and has no external callers that parameterize it. Update help text.

- **[SKILL.md](../../SKILL.md)** — update any flag reference. `validate_bash.py` doesn't inspect flag names so no hook change needed.

## Reused utilities

- `parse_iso`, `repo_name`, `text_from_content` from [lib/utils.py](../../lib/utils.py) — unchanged.
- `extract_jira_ids` from [lib/jira.py](../../lib/jira.py) — unchanged.
- `SKIP_TYPES`, `FILE_TOOLS` constants at [scanner.py:21-30](../../lib/scanner.py#L21-L30) — unchanged.

## Verification

1. **Unit-level sanity**: write a small in-process test (not a test suite, just a scripted assertion block per [CLAUDE.md §"Smoke testing changes"](../../CLAUDE.md)) on a synthetic record list containing one of each gap kind — confirm each contributes the expected credit.

2. **Real-data regression**: run against a window that includes the historical report:
   ```bash
   python3 generate.py --output-dir /tmp/pr-test
   python3 -c "
   import json
   d = json.load(open('/tmp/pr-test/report.json'))
   s = d['sessions']
   assert all('gaps' in x and 'segments' in x for x in s)
   assert all('needsActiveReview' in x for x in s)
   # activeDurationMin should not exceed durationMin for any session
   assert all(x['activeDurationMin'] <= x['durationMin'] + 0.1 for x in s)
   # cap enforcement: no single user_pause contributes > 10 min
   for x in s:
     for g in x['gaps']:
       if g['kind'] == 'user_pause':
         assert g['creditedSec'] <= 600
   print('OK')
   "
   ```

3. **Diff before/after** on the sample run: compare old `activeDurationMin` vs new for a handful of sessions. Expect the nestjs-boilerplate-style outlier to stay low, mid-length sessions with long tool runs to rise, sessions with many mid-size user pauses to drop.

4. **Schema validation** against the bumped `$id`:
   ```bash
   python3 -c "from jsonschema import validate; import json; validate(json.load(open('/tmp/pr-test/report.json')), json.load(open('report.schema.json')))"
   ```

5. **`--rerender` round-trip**: run `--rerender` after edits to the gap list / `activeDurationMin` and confirm `recompute_totals` picks up the changes correctly.

6. **Flagging calibration**: inspect `needsActiveReview` counts on the historical run. If > ~30 % of sessions flag, tighten thresholds; if < ~5 %, consider loosening. The three flagging rules are cheap to tune in isolation.
