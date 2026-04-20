# Unify the gap cap across all kinds (v2.0.0)

## Context

[Plan 002](002-active-duration-gap-classification.md) introduced gap classification (`tool_runtime` / `inference` / `user_pause` / `same_turn`) and capped only `user_pause`. Tool runtime and inference were credited *in full* on the assumption they complete in bounded time.

That assumption breaks whenever a `tool_use` needs user approval and the user walks away. The `assistant(tool_use) → user(tool_result)` gap is then semantically "user away", not "tool running" — but the classifier sees tool-runtime shape and credits every second.

### Evidence in the current dev-data report

Running the classifier over every session in [dev-data/report.json](../../dev-data/report.json):

- 28 gaps > 10 min are silently non-`user_pause` (so they're missing from `gaps[]` and fully credited).
- They account for **~467 hours** of mis-credited "active" time across 156 sessions.
- Top offenders: `43621413` (236.5 h `tool_runtime`), `94da9c27` (72.5 h `inference`), `140a5ddb` (67.7 h `tool_runtime`), `503e9ed7` (48.6 h `tool_runtime`).
- The conservation invariant `durationMin - activeDurationMin == idleMin` is preserved *by construction* but only for `user_pause` excess, so over-cap tool_runtime/inference contribute 0 to idle and disappear entirely from downstream views.
- `Segment.messageCount` counts synthetic `tool_result`-only user records and `tool_use`-only assistant records, inflating "messages per burst" counts.

### What we're changing

1. **Cap applies to every gap kind except `same_turn`.**
   - `user_pause` → `--user-pause-cap-min` (default **10**, unchanged)
   - `tool_runtime` and `inference` → new `--tool-runtime-cap-min` (default **30**)
   - `same_turn` → uncapped (stays fully credited; same-speaker < 5 s is a logical continuation, never a pause)
2. **All over-cap gaps are emitted** into `gaps[]` with their `kind`. Under-cap gaps are still suppressed to keep the array bounded.
3. **`segments[]` splits on any over-cap gap**, not only `user_pause`. A 67-hour tool-runtime gap no longer glues two bursts into a single fake segment.
4. **`idleSec` is the sum of stripped time across every kind** (restores `duration − active == idle` conservation end-to-end).
5. **`idleBreakdownSec`** (new, required) breaks idle down per kind: `{user_pause, tool_runtime, inference}`. Lets the visualizer label "we threw away 67 h of tool runtime" separately from "we threw away 2 h of user pause".
6. **`Segment.messageCount` excludes synthetic records.** A user record is counted only if it carries real user-authored content (not a `tool_result`-only block); an assistant record is counted only if it has at least one non-empty `text` block.
7. **`needsActiveReview` reason rename:** `many_long_pauses` → `many_long_gaps` (now counts over-cap gaps of any kind, not only `user_pause`).

`userPauseCount` and `longestUserPauseSec` stay as `user_pause`-specific counters (they're still useful signals on their own).

### Why v2.0.0 and not v1.3.0

- `activeDurationMin` semantics change — consumers with cached historical values will see them shift (usually downward — the `~467 h` of silently-credited idle time gets stripped).
- `Segment.messageCount` semantics change — values drop for sessions with many tool calls.
- `needsActiveReview.activeReviewReason` enum changes (`many_long_pauses` → `many_long_gaps`).
- New required field `idleBreakdownSec`.
- `gaps[].kind` values broaden (`tool_runtime` / `inference` are now emitted, not only `user_pause`).

All of these are breaking changes for strict consumers. MAJOR bump is correct.

## Files to change

- **[lib/scanner.py](../../lib/scanner.py)** — core algorithm:
  - `_classify_and_score_gaps` takes `user_pause_cap_sec` *and* `tool_runtime_cap_sec`.
  - Per-gap cap lookup by kind; `same_turn` uncapped.
  - Emit every over-cap gap (any kind). Split segments on every over-cap gap.
  - Track a `has_text` flag per record (user: content has non-`tool_result` block or non-empty string; assistant: at least one non-empty `text` block). Feed it into segment `messageCount`.
  - Return `idleBreakdownSec` in the result dict.
  - Rename `REASON_MANY_PAUSES = "many_long_pauses"` → `"many_long_gaps"`.
  - `parse_session_file` / `scan_sessions` get a `tool_runtime_cap_min` kwarg.

- **[generate.py](../../generate.py)** — add `--tool-runtime-cap-min MINUTES` (default 30), thread into `scan_sessions`.

- **[lib/report.py](../../lib/report.py)** — in `recompute_totals`, backfill `idleBreakdownSec` with zeros when missing (forward-compat for an existing report being rerendered).

- **[report.schema.json](../../report.schema.json)** — bump `$id` to `v2.0.0`:
  - `idleBreakdownSec` added as a required session field.
  - `idleSec`, `activeDurationMin`, `gaps[]`, `Segment.messageCount` descriptions rewritten.
  - `Gap.kind` description drops the "Only `user_pause` gaps appear in `gaps[]`" line.
  - `activeReviewReason` enum list updated (`many_long_gaps`).

- **[REPORT_SCHEMA.md](../../REPORT_SCHEMA.md)** — rewrite the "Active duration" section: two-cap table, `idleBreakdownSec`, segment-count semantics.

- **[CHANGELOG.md](../../CHANGELOG.md)** — v2.0.0 entry with a loud callout (formula + semantic changes + enum rename).

- **[CONTEXT.md](../../CONTEXT.md)** — tweak any residual references to "tool runtime always credited".

- **[SKILL.md](../../SKILL.md)** — argument-hint gains `--tool-runtime-cap-min MINUTES`.

## Verification (acceptance criteria)

Run `python3 generate.py --output-dir /tmp/pr-test` with defaults, then assert:

1. **Target session** (`140a5ddb-96c5-4197-8462-39e5de291b59`):
   - `activeDurationMin` ∈ [60, 120]  (was 4148.3; the 67 h tool-runtime gap now contributes only 30 min of credit, and the 2.55 h user-pause contributes only 10 min — wall clock is 71.5 h, expected active ≈ initial 36 min burst + 30 min re-engagement + segment tail).
   - Some entry in `gaps[]` covers `2026-04-16T18:11:33Z → 2026-04-19T13:50:53Z` with `kind: "tool_runtime"` and `creditedSec: 1800`.
   - `segments[]` has ≥ 3 entries (split at both the 2.55 h user_pause and the 67 h tool_runtime).

2. **Schema validation** against the v2.0.0 schema passes.

3. **Conservation invariant**: for every session, `durationMin * 60 − activeDurationMin * 60 ≈ idleSec` (within rounding).

4. **`idleBreakdownSec` sum equals `idleSec`** for every session.

5. **No absurd segments**: for every `segments[]` entry, `sec / max(messageCount, 1) < 600` (no segment implying an average of >10 min per record, which would indicate an unsplit over-cap gap).

6. **`--rerender` round-trip** succeeds on an existing v1.x report (`idleBreakdownSec` backfilled with zeros).
