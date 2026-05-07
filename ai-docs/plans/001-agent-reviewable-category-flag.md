# 001 — Agent-reviewable category flag on sessions

## Context

`lib/categorize.py` uses deterministic keyword + tool-usage rules to assign one of 12 categories. Four of those buckets are known-noisy:

- **`other`** — heuristic had no confident signal; essentially "I don't know".
- **`discarded`** — trivially short or zero-tool sessions; the 0.5min / <50 char rule can false-positive on a legitimately terse `ask`.
- **`meta`** — fires on keywords like `claude code`, `hook`, `skill`, `config`; a short meta session is often really `discarded`.
- **`ask`** — shape-based (+5) rule wins on short, few-turn, no-edit sessions; can swallow things that are actually `discarded` or `exploration`.

The `--rerender` pipeline already supports post-pass re-categorization: the calling agent edits `session.category` in `report.json`, re-runs `generate.py --rerender`, and `recompute_totals` refreshes `totals.categories` / `categoryMinutes` / `minutesByDay[*].categories`. What's missing is a **machine-readable signal** telling the agent which sessions to inspect — SKILL.md currently relies on fuzzy prose ("sessions where the heuristic is likely wrong").

The goal is to flag each of these four buckets at generation time so the parent agent can iterate ambiguous sessions, re-inspect them from data already in `report.json` (firstPrompt, userMessages, toolCounts, filesTouched), and bidirectionally re-categorize — including promoting a `discarded` session back to e.g. `ask`, or demoting a `meta`/`ask` to `discarded`.

Locked-in decisions:
- **Session-level boolean flag** (not prose-only, not a separate artifact).
- **Bidirectional** review — agent can demote *and* promote.
- **`report.json` is sufficient context** — no new data pipeline; agent can optionally read the raw `.jsonl` via `session.filePath` for any hard case.
- **Executor is caller-inline for now.** Subagent architecture deferred to the plugin migration (see [FUTURE_PLANS.md](../../FUTURE_PLANS.md) — "Refinement-pass executor").

## Design

Add two new per-session fields:

- **`needsReview: boolean`** — `true` iff the final category is one of `{other, discarded, meta, ask}`. Set at generation time.
- **`reviewReason: string | null`** — short, per-category hint the agent can read to know what the heuristic was uncertain about. Null when `needsReview` is false.

Agent refinement loop: `for s in report.sessions if s.needsReview → re-inspect → optionally overwrite s.category → --rerender`.

### Why these semantics

- Derived-from-category only, not a separate confidence score. Keeps the rule trivial and auditable.
- `reviewReason` is descriptive, not prescriptive — the agent decides what to reclassify to.
- Survives `--rerender` trivially: `recompute_totals` doesn't touch per-session fields.
- No new dependencies, no LLM call in the script — preserves the core invariant from CONTEXT.md.

### `reviewReason` strings

| category | reviewReason |
|---|---|
| `other` | `"heuristic could not pick a category"` |
| `discarded` | `"flagged as trivial — consider promoting if the short session was actually meaningful"` |
| `meta` | `"keyword match — may actually be discarded if the work was trivial"` |
| `ask` | `"shape-based match — may actually be discarded or exploration"` |

## Critical files to modify

1. **`lib/categorize.py`** — Add a sibling function `review_reason(category: str) -> str | None` that returns the reason for the four flagged categories and `None` otherwise. Keep `categorize()` signature unchanged.

2. **`generate.py:234`** — The single call site for `categorize()`. Extend:
   ```python
   cat = categorize(session)
   session["category"] = cat
   reason = review_reason(cat)
   session["needsReview"] = reason is not None
   session["reviewReason"] = reason
   ```

3. **`report.schema.json`** — Add `needsReview` (boolean) and `reviewReason` (string | null) to the `Session` `$defs`. Add both to `Session.required`. Bump `$id` from `progress-report/report/v1.0.0` to `progress-report/report/v1.1.0` (additive MINOR).

4. **`lib/report.py:177` (`recompute_totals`)** — Add a tiny backfill so `--rerender` on a pre-v1.1.0 report auto-migrates: for each session, if `needsReview` is absent, set both fields from `category` using `review_reason`. Keeps old reports renderable without a full re-scan.

5. **`CHANGELOG.md`** — New `## 1.1.0` section under `Unreleased`. Entry: "Added `needsReview` and `reviewReason` (Session). Flags `other`, `discarded`, `meta`, `ask` sessions for the optional post-generation refinement pass documented in SKILL.md. Additive; no breaking changes."

6. **`SKILL.md` §"LLM category refinement"** (lines 69–88) — Rewrite to:
   - Key the loop off `session.needsReview`, not prose.
   - Spell out the bidirectional playbook (demote/promote) with one-line guidance per `reviewReason`.
   - Keep the "skip unless asked" default — we're making the refinement *well-defined*, not *mandatory*.

7. **`REPORT_SCHEMA.md`** — Add a "Review flags" subsection under Sessions describing both fields and the intended post-processing contract.

8. **`SCHEMA_SUMMARY.md` §3 (Sessions)** — Add `needsReview` / `reviewReason` to the Sessions bullet list.

9. **`CONTEXT.md`** — In the existing "post-pass pattern" section, add a note pointing to `needsReview` as the canonical signal the agent iterates over.

## Reuses existing code/patterns

- **`CATEGORIES` frozenset** at `lib/categorize.py:11` — reuse for validation.
- **`recompute_totals` preservation pattern** at `lib/report.py:177` — already preserves `category` across `--rerender`; `needsReview`/`reviewReason` ride on the same mechanism.
- **`_merge_tickets` enrichment-preservation idiom** at `lib/report.py:204` — precedent that "agent can enrich report.json in place and --rerender preserves it". Same contract for `category` edits.
- **CHANGELOG / schema `$id` bump convention** — matches the v1.0.0 precedent for `activeDurationMin` (additive MINOR).

## Non-goals

- **No confidence score.** Bool flag is enough; a numeric score invites tuning we don't need.
- **No `exploration` in the review set.** Scoped to four buckets; exploration is usually correct.
- **No review artifact.** Rejected a separate `review-queue.md` for complexity.
- **No mandatory LLM pass.** Refinement stays optional.
- **No reviewer state tracking.** YAGNI.
- **No subagent-based executor in this change.** Deferred — see [FUTURE_PLANS.md](../../FUTURE_PLANS.md).

## Verification

1. **Fresh generation emits the flags**:
   ```bash
   python3 generate.py --output-dir /tmp/pr-test
   python3 -c "
   import json
   d = json.load(open('/tmp/pr-test/report.json'))
   need = [s for s in d['sessions'] if s['needsReview']]
   not_need = [s for s in d['sessions'] if not s['needsReview']]
   assert all(s['reviewReason'] for s in need)
   assert all(s['reviewReason'] is None for s in not_need)
   assert all(s['category'] in {'other','discarded','meta','ask'} for s in need)
   print('OK', len(need), 'flagged /', len(d['sessions']), 'total')
   "
   ```

2. **Schema validates**:
   ```bash
   python3 -c "from jsonschema import validate; import json; validate(json.load(open('/tmp/pr-test/report.json')), json.load(open('report.schema.json')))"
   ```

3. **`--rerender` preserves agent edits AND backfills old reports**:
   - Edit one flagged session's `category` from `"other"` to `"implementation"` in `report.json`.
   - Run `python3 generate.py --rerender --output-dir /tmp/pr-test --format all`.
   - Assert the edited category survived and `totals.categories` reflects the change.
   - Also test against `plan-and-dev-assets/report.json` (v1.0.0, no flags): copy to a temp dir, run `--rerender`, verify flags were backfilled with the correct reason per category.

4. **Spot-check real data**: run against `plan-and-dev-assets/report.json`'s source window; confirm the 4 `other`, 24 `discarded`, 14 `meta`, 2 `ask` sessions all get `needsReview: true` and non-null reasons, and every other category gets `needsReview: false` / `reviewReason: null`.

5. **Markdown re-render** still works — run `--format md` and eyeball `report.md` for regressions.
