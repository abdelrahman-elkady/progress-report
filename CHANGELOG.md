# Changelog

All notable schema changes to the progress report output are documented here.
The version in each heading matches the `$id` field in `report.schema.json`
(e.g. `progress-report-skill/report/v0.2.0`). This project uses
[Semantic Versioning](https://semver.org/): MAJOR for breaking/removing fields,
MINOR for additive changes, PATCH for description or docs-only fixes.

Consumers of `report.json` should review this when updating.

## Unreleased

_(nothing yet)_

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

