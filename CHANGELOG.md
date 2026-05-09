# Changelog

All notable schema changes to the dev digest output are documented here.
The version in each heading matches the `$id` field in `report.schema.json`
(e.g. `claude-dev-digest/report/v2.0.0`). This project uses
[Semantic Versioning](https://semver.org/): MAJOR for breaking/removing fields,
MINOR for additive changes, PATCH for description or docs-only fixes.

Consumers of `report.json` should review this when updating.

## Unreleased

_(nothing yet)_

## 2.0.0

### ⚠️ Changed — `activeDurationMin` formula (loud callout, **breaking**)

**Numbers will shift on re-run, often substantially downward.** v1.2.0
credited `tool_runtime` and `inference` gaps in full under the assumption that
tools complete in bounded time. That assumption breaks whenever a `tool_use`
needs user approval and the user walks away: the classifier saw tool-runtime
shape and credited multi-hour (or multi-day) spans as active time. In practice
this silently inflated `activeDurationMin` on long sessions — outliers in the
test dataset carried hundreds of hours of mis-credited idle time.

**After (v2.0.0):** all non-`same_turn` gap kinds are capped.

- `user_pause` → `--user-pause-cap-min` (default **10 min**, unchanged)
- `tool_runtime` and `inference` → new `--tool-runtime-cap-min` (default **30 min**)
- `same_turn` → uncapped (same-speaker < 5 s is a logical continuation)

Every over-cap gap (any kind) is now emitted into `gaps[]` and splits
`segments[]`. `idleSec` sums stripped time across every kind, so the
conservation invariant `durationMin - activeDurationMin == idleSec / 60` holds
end-to-end. The new `idleBreakdownSec` field exposes the per-kind split.

### Added

- **`idleBreakdownSec`** (Session, required object) — `{user_pause,
  tool_runtime, inference}` breakdown of stripped idle time, summing to
  `idleSec`. Lets consumers label e.g. "67 h of tool-runtime was stripped"
  separately from "2 h of user pauses was stripped".
- **`--tool-runtime-cap-min MINUTES`** (CLI flag, default 30) — caps each
  `tool_runtime` and `inference` gap. Excess becomes idle.

### Changed (breaking)

- **`activeDurationMin` semantics** — see loud callout above. Cached historical
  values will not match; re-run `generate.py` for fresh numbers.
- **`idleSec` semantics** — now sums stripped time across every capped kind
  (`user_pause` + `tool_runtime` + `inference`), not only `user_pause`.
- **`gaps[]` contents** — `tool_runtime` and `inference` gaps are now emitted
  when over their cap. Previously `gaps[]` only ever contained `user_pause`
  entries regardless of kind.
- **`segments[]` splitting** — segments split on over-cap gaps of any kind,
  not only `user_pause`. Fixes the v1.2.0 case where a multi-hour `tool_runtime`
  gap glued two real bursts into a single segment with a misleading `sec`
  value.
- **`Segment.messageCount` semantics** — now counts conversational turns
  (user records with typed content / slash commands + assistant records with
  at least one non-empty text block). Synthetic `tool_result`-only user
  records and `tool_use`-only assistant records are excluded. Values drop for
  tool-heavy sessions.
- **`activeReviewReason` enum** — `many_long_pauses` renamed to
  `many_long_gaps`, since the rule now counts over-cap gaps of any kind.

### Backward compatibility

- `--rerender` on a v1.x `report.json` backfills `idleBreakdownSec` with zeros
  so the re-emit validates against the v2.0.0 schema. For fresh, correct
  numbers, re-run `generate.py`.
- Pre-v1.2.0 reports passed through `--rerender` still get the v1.2.0 session
  fields backfilled (unchanged) plus the new v2.0.0 `idleBreakdownSec` field.

## 1.2.0

### ⚠️ Changed — `activeDurationMin` formula (loud callout)

**Numbers will shift on re-run.** Downstream dashboards that cached historical
values should recompute, and migration/diff checks against pre-v1.2.0 reports
will not match.

**Before (v1.0.0 – v1.1.0):** a single idle threshold (default 45 min) was
applied to every inter-message gap. Gaps below the threshold were credited in
full; gaps above were dropped entirely. This conflated *user-away* time with
*Claude working on the user's behalf* — a 46 min tool run was treated the same
as a 46 min coffee break (0 active minutes), and a 44 min coffee break was
treated the same as 44 min of engaged conversation (44 active minutes).

**After (v1.2.0):** gaps are classified by endpoint shape (`tool_runtime`,
`inference`, `user_pause`, `same_turn`). Only `user_pause` gaps are stripped,
and they're **capped** at `--user-pause-cap-min` (default 10 min) rather than
dropped — a 20 min pause contributes 10 min of active time, not 0. Tool runtime
and inference are always fully credited.

Practical effects on existing sessions:

- Long tool/build sessions **rise** — previously-discarded tool-runtime gaps
  now count as active time.
- Sessions with many mid-size user pauses **fall** — previously fully-credited
  short pauses now get capped at 10 min each (most small gaps are unchanged
  since they fall below the cap).
- Opaque stripped outliers (e.g. overnight sessions) stay low, but the stripped
  time is now visible in `idleSec` / `gaps[]` rather than silently dropped.

The field name and type are unchanged; only the formula is different.

### Added

- **`idleSec`** (Session, number) — seconds stripped from active duration
  because of over-cap `user_pause` gaps. Equals `sum(g.sec - g.creditedSec)`
  over `gaps[]`.
- **`userPauseCount`** (Session, integer) — total number of `user_pause` gaps
  observed in the session (any duration, not just over-cap).
- **`longestUserPauseSec`** (Session, number) — duration of the single longest
  `user_pause` gap, in seconds.
- **`gaps`** (Session, array of `Gap`) — over-cap `user_pause` gaps only, each
  with `startedAt`, `endedAt`, `sec`, `kind`, `creditedSec`. Designed for
  auditing / `--rerender` edits; the array stays bounded because under-cap and
  non-pause gaps are never emitted.
- **`segments`** (Session, array of `Segment`) — contiguous activity bursts
  split by over-cap gaps. Useful for timeline visualizers.
- **`needsActiveReview`** (Session, boolean) — `true` when the active-duration
  calculation is worth a second look (long single pause, high idle ratio, or
  many long pauses). Independent of the category-review `needsReview`.
- **`activeReviewReason`** (Session, string | null) — one of
  `long_single_pause`, `high_idle_ratio`, `many_long_pauses`; null otherwise.
- **`totals.idleMinutesByRepo`** and **`totals.idleCategoryMinutes`** — idle
  minute totals mirroring `activeMinutesByRepo` / `activeCategoryMinutes`.
- **`minutesByDay[*].idleMinutes`** — per-day idle total, parallels
  `activeMinutes`.

### Changed

- **`--idle-threshold MINUTES`** → **`--user-pause-cap-min MINUTES`**
  (default changed from 45 → 10). The old flag is removed. The new flag caps
  each `user_pause` gap (excess becomes idle) instead of hard-dropping gaps
  above a single threshold.

Additive schema fields + loud formula change. Pre-v1.2.0 reports passed
through `--rerender` auto-backfill the new session fields with defaults so the
re-emit is self-consistent; for fresh, correct numbers re-run `generate.py`.

## 1.1.0

### Added

- **`needsReview`** (Session, boolean) — `true` iff the final category is one of
  `other`, `discarded`, `meta`, `ask`. Flags the four uncertain heuristic buckets
  for the optional post-generation refinement pass documented in SKILL.md.

- **`reviewReason`** (Session, string | null) — Short, per-category hint
  describing what the heuristic was uncertain about. Non-null iff `needsReview`
  is true; `null` otherwise.

Additive only; no breaking changes. Pre-v1.1.0 reports passed through
`--rerender` are auto-backfilled from each session's existing `category`.

## 1.0.0

### Added

- **`discarded` category** — New session category for sessions that never produced
  meaningful work. Detected when a session has zero tool calls, or is ultra-short
  (< 0.5 min) with minimal assistant output (< 50 chars). Previously these were
  misclassified as `ask`.

- **`activeDurationMin`** (Session) — Active duration in minutes, computed by
  subtracting idle gaps from wall-clock duration. A gap between consecutive messages
  exceeding the idle threshold (default 45 min, configurable via `--idle-threshold`)
  is counted as idle time. Complements the existing `durationMin` (wall-clock).

- **`activeMinutesByRepo`** (Totals) — Total active minutes per repo, parallels
  `minutesByRepo`.

- **`activeCategoryMinutes`** (Totals) — Total active minutes per category, parallels
  `categoryMinutes`.

- **`activeMinutes`** (DayBucket in `minutesByDay`) — Active session minutes per day,
  parallels the existing `minutes` field.

- **`--idle-threshold MINUTES`** CLI argument — Configures the idle gap threshold
  used to compute active duration fields. Default: 45 minutes.

### Changed

- **`durationMin`** description clarified to "wall-clock duration" to distinguish
  from the new `activeDurationMin`.

- **`minutes`** in DayBucket description clarified to "wall-clock session minutes".

- **`categoryMinutes`** description clarified to "wall-clock minutes per category".

