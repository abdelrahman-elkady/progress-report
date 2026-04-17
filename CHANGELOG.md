# Changelog

All notable schema changes to the progress report output are documented here.
The version in each heading matches the `$id` field in `report.schema.json`
(e.g. `progress-report-skill/report/v0.2.0`). This project uses
[Semantic Versioning](https://semver.org/): MAJOR for breaking/removing fields,
MINOR for additive changes, PATCH for description or docs-only fixes.

Consumers of `report.json` should review this when updating.

## Unreleased

_(nothing yet)_

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

