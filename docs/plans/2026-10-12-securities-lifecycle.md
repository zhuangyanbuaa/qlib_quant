# Securities Lifecycle Implementation Plan

> Execute task-by-task using the executing-plans workflow. This batch ends at the review checkpoint below.

**Goal:** Separate inactive securities from missing live prices, honor dated watchlists, and classify Yahoo responses accurately.

**Architecture:** Store reviewed lifecycle facts in append-only Parquet with effective sessions and availability timestamps. Resolve symbols using both dates; rebuild decisions from visible facts without joining old and successor price series. Unknown cessation dates conservatively take effect on the review date and retain an explicit flag.

**Tech Stack:** Python, Pydantic, Arrow/Parquet, DuckDB, pandas, pytest.

### Task 1: Lifecycle facts and point-in-time resolution

- Create `src/quant_system/domain/securities.py`, `src/quant_system/universe/lifecycle.py`, and `tests/unit/test_securities_lifecycle.py`.
- Extend `src/quant_system/storage/schemas.py` and `src/quant_system/storage/parquet.py` with an independent lifecycle dataset; preserve existing price schemas.
- Test append-only collisions, full provenance, late fact visibility, corrections, chained renames, conversions, cessation and resumption before implementing the resolver.
- Add a validated local-manifest import command in `src/quant_system/cli.py`; importing reviewed events appends facts and never edits price files.
- Verify with `.venv/bin/python -m pytest tests/unit/test_securities_lifecycle.py -q`.

### Task 2: Dated membership and lifecycle-aware consumers

- Add inclusive `start_date`/`end_date` selection to `src/quant_system/universe/config.py`; keep full inventory available for historical backfill.
- Apply dated selection in `decision/premarket.py`, `decision/rotation.py`, `decision/hierarchy.py`, CLI decision commands and `scripts/strategy_calibration_closure.py`.
- Update `ingestion/prices.py` and `quality/reports.py`: report original requests, resolved targets, skipped inactive symbols and rejected post-cessation bars. Exclude inactive noncritical symbols from provider-failure denominators; preserve critical-source blocking.
- Apply lifecycle quality masks in shared `backtest/workflow.py` loaders using each historical cutoff. Empty leader universes produce conservative context instead of a comparison exception.
- Add fixture-based update, membership and decision integration tests; run relevant suites.

### Task 3: Yahoo classification, local reconciliation and verification

- Bump `ingestion/yahoo.py` normalization version. Separate padded rows, action-only rows, missing columns and invalid/nonfinite OHLCV; retain zero-volume data with a review flag rather than assuming suspension.
- Extend `tests/unit/test_yahoo_adapter.py` with fixed mixed-symbol, action-only, malformed and nonfinite fixtures.
- Convert the previously reviewed local evidence into a validated runtime lifecycle manifest, preserving conservative availability. Import it only after tests pass; do not commit runtime evidence or data.
- Update `docs/data-contracts.md` and `docs/daily-runbook.zh.md` with boundaries and commands.
- Run `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check src tests scripts`, `git diff --check`, and a local-data → Parquet → DuckDB → canonical features/scan → JSON smoke test. Confirm existing raw price files remain untouched.

**Review checkpoint:** Report this batch and its tests; stop before Phase 7 scheduling, model work, automatic series stitching or further strategy changes. Do not commit the existing mixed working tree automatically.

## Execution record — 2026-10-12

- Tasks 1–3 completed. Added six lifecycle event types, immutable Parquet storage,
  DuckDB cutoff queries, local import with validation/dry-run/idempotence, and
  lifecycle-aware update/decision gates. Dated membership is inclusive.
- Fixed fixtures cover event availability, revisions, chained renames, conversion
  identities, suspensions/resumptions, file collisions, dates across every
  membership consumer, Yahoo padding/actions/invalid and infinite values, and
  the complete workbench path and inactive-symbol failure denominators. Full suite:
  202 passed; Ruff and `git diff --check` passed.
- Imported 41 previously reviewed facts into the ignored runtime dataset. Four
  unknown effective dates use review-date-only conservative flags. Historical
  cutoff visibility remains unchanged; this is not historical gap repair.
- Actual update: one provider call, 1,448 rows appended in 70 new files; nine
  mappings, 32 inactive skips, zero active stale/empty/failed symbols. DMC had
  one invalid price row discarded, so the outcome is correctly `DEGRADED`.
- Local October 9 workbench smoke: all four components completed; one candidate,
  five research rows, 48 leverage pairs. Original 2,164 raw price files retained
  identical size and modification times. Operational state was copied read-only
  into a smoke database; report/model runtime files remain ignored by Git.
- Review boundary reached. Phase 7, complete historical membership/security-master
  reconstruction and automatic price-series/holding conversion remain deferred.
