# Price cutoff and quality gates implementation plan

> Execute task-by-task using the local `executing-plans` skill. The user has authorized implementation in the current workspace; preserve the existing Yahoo adapter changes.

**Goal:** Prevent historical decisions from consuming price versions unavailable at their cutoff, and block decision workflows when critical daily prices are missing or stale.

**Architecture:** Keep raw Parquet append-only. Query raw versions before deduplication, construct causal features at each exchange session's close-plus-30-minute cutoff, and share a critical-price gate across scans, backtests, model datasets and daily decision workflows. Latest-version analytical views remain available for operational inventory only.

**Tech Stack:** Python, pandas, PyArrow, DuckDB, exchange-calendars, Typer, pytest.

## Scope and boundaries

- Implement only Tasks 1–3 below in this batch. Stop at the review checkpoint.
- No provider fetches, raw rewrites, lifecycle implementation, snapshot changes, scheduling, model promotion, or new strategy thresholds.
- Preserve source metadata through analytical queries and feature construction.
- Use `available_at_utc <= cutoff_utc` before selecting the latest fetched version.
- Use the same point-in-time feature implementation for scan and backtest. A backtest-wide end cutoff alone is insufficient for earlier decisions.
- Daily bar cutoff is the exchange close plus the configured 30-minute availability delay. Signal logical time may equal cutoff; earliest order remains the next session's open.
- Existing inferred availability and retroactively adjusted provider data cannot become reliable historical publication records through this change. Document that limitation; do not relabel old raw facts.
- Work in the existing authorized workspace to preserve the current uncommitted adapter patch. Do not commit runtime artifacts or unrelated changes.

## Task 1: Snapshot queries before deduplication

**Files:** Modify `src/quant_system/storage/duckdb.py`; test `tests/integration/test_duckdb_views.py`.

1. Add failing fixed-fixture tests with two versions of one bar: original close 101 available before cutoff, revised close 103 available after cutoff. Also test exact-boundary inclusion, invisible-only rows, timezone validation, lineage preservation, and unchanged latest inventory behavior.
2. Run `.venv/bin/python -m pytest tests/integration/test_duckdb_views.py -q`; verify the new API tests fail.
3. Add required `cutoff_utc` to historical price queries and a raw-version query for causal features. Filter raw facts by availability before the row-number selection. Keep `query_daily_prices` latest mode for operational inspection, with an optional explicit cutoff for historical reads.
4. Run the targeted tests and `.venv/bin/ruff check src/quant_system/storage/duckdb.py tests/integration/test_duckdb_views.py`.

## Task 2: Canonical causal features and shared quality gate

**Files:** Create `src/quant_system/features/point_in_time.py`, `src/quant_system/quality/prices.py`, `tests/integration/test_price_decision_cutoff.py`; modify `src/quant_system/backtest/workflow.py`, `src/quant_system/strategy/buy_the_dip.py` and timeline tests.

1. Add failing tests proving a later revision cannot alter an earlier decision's MA/ATR, but can affect decisions after availability. Compare the canonical result against individually queried snapshots. Test missing SPY/QQQ, stale critical prices, stale candidate rows, and scan/backtest signal parity.
2. Load raw price versions once. Build feature segments separated by late-version activation sessions; within each segment, include only versions visible to each row's cutoff. Reuse `build_technical_features` for all indicator calculations.
3. Preserve row lineage and attach `data_cutoff_utc`, `price_quality_pass`, and critical-price availability. Require SPY, QQQ and the selected market benchmark on requested decision sessions. Raise a structured `PriceDataBlocked` before recommendations when critical rows fail.
4. Make the canonical strategy reject quality-blocked rows, including the previous dip row. Align signal cutoff with the bar availability cutoff without changing earliest-order or fill timing.
5. Run targeted cutoff, strategy, backtest, feature and timing tests.

## Task 3: Workflow integration, CLI and end-to-end validation

**Files:** Modify historical readers in `src/quant_system/decision/`, `src/quant_system/cli.py`, and `scripts/strategy_calibration_closure.py`; extend integration/CLI tests and `docs/data-contracts.md` / `docs/daily-runbook.zh.md`.

1. Pass explicit cutoffs to hierarchy/rotation, positions, paper and calibration price reads; share critical-price checks for decision context. Align report metadata with actual data cutoffs.
2. Return machine-readable `BLOCKED` JSON and exit code 2 from CLI price-gate failures. Never report a blocked workbench as completed. Verify through `CliRunner` and a subprocess.
3. Add a fixed-fixture smoke path through Parquet, temporary DuckDB, causal features, scan/backtest and daily workbench JSON, including a missing-critical-price failure case. Keep paper/manual operational writes isolated in temporary directories.
4. Run `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check .`, and `git diff --check`.
5. Run a read-only local-data scan for 2026-10-09 with a temporary analytical database; verify source raw data is untouched. Record results here and summarize outstanding limitations.

## Review checkpoint

Stop after Task 3. Report the implemented behavior, test evidence, raw-data preservation and known inferred-availability limitations. Review before beginning securities lifecycle, snapshot validation or Phase 7.

## Execution status

- Task 1: completed; four DuckDB tests pass, including revision visibility and boundary inclusion; targeted Ruff passes.
- Task 2: completed; 19 targeted tests pass, including snapshot-equivalent features, late initial data, stale candidate rejection, and a nonempty scan/backtest signal parity fixture.
- Task 3: completed; full daily-workbench fixture and blocked CLI/runner subprocess cases pass. Missing previous-session data cannot be treated as yesterday's dip; stale underlying prices cannot authorize leverage review.

## Validation at the review checkpoint

- Full suite: 179 tests passed; Ruff and `git diff --check` passed.
- Fixed-fixture smoke produces one canonical candidate through scan/backtest and a complete daily index; missing core prices block before report creation.
- Local 2026-10-09 smoke: five-symbol scan completes; full workbench completes with premarket, research, leverage and positions components. Report cutoff is `2026-10-09T20:30:00+00:00`.
- Local smoke uses temporary DuckDB/report paths and a read-only SQLite backup. All 2,164 raw Parquet files retain their original sizes and modification times. No providers are called.
- Changes are left uncommitted for review, including the pre-existing Yahoo adapter fix.

## Remaining work and limitations

- Securities lifecycle, effective watchlist membership, all-NaN/action-only provider classification, suspended zero-volume bars, snapshot validation, durable budgets and Phase 7 remain outside this batch.
- Historical replay trusts recorded availability; inferred legacy/backfill timestamps and retroactive adjusted prices remain research limitations.
- During smoke construction, a watchlist without any `mega`/`high` AI leader exposed an existing `None` breadth comparison in hierarchy calibration. The end-to-end fixture uses an explicit leader, as does the main local universe. Add a conservative empty-leader fallback in a subsequent scoped batch; do not silently widen this plan.
