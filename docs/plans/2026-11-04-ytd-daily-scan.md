# YTD Daily Scan Implementation Plan

> Execute task-by-task using the executing-plans workflow; the review checkpoint is after the requested run and verification. The user has authorized this work. No deployment, Telegram delivery, parameter tuning, or commits.

**Goal:** Replay every NYSE session from 2026-01-01 through 2026-11-03 and evaluate canonical daily signals without fabricating intraday fills.

**Architecture:** Load immutable price versions once, reconstruct canonical point-in-time features, and use the existing hierarchy score and BuyTheDipStrategy. Export two explicitly separate populations: membership eligible on each date, and today's membership applied retrospectively (selection bias). Historical T+30/T+60 execution remains no-fill: outcome labels are next-open to future-close research returns, never portfolio performance.

**Tech Stack:** Python, pandas, Parquet, DuckDB, NYSE calendar, Typer, pytest.

### Task 1: Replay and outcome contracts

- Add `src/quant_system/backtest/daily_scan.py` and `tests/unit/test_daily_scan_research.py`.
- Tests: revisions excluded before availability, weekend/holiday horizons, incomplete horizons null, stale/missing prices block labels, current membership distinct from historical eligibility, and no-fill result cannot be presented as strategy profit.
- Use canonical feature/rule/score implementations without modifying strategy or raw data. Keep every session including no-signal days. Store all source versions and configuration snapshots in the report bundle.
- Run `.venv/bin/pytest tests/unit/test_daily_scan_research.py`.

### Task 2: Reproducible command and end-to-end checks

- Add `backtest daily-scan --start --end` to `src/quant_system/cli.py`.
- Report CSV/JSON/Parquet, daily JSON files, and Chinese Markdown summary under ignored `data/reports/backtests/` in a new run directory.
- Run relevant cutoff, strategy, execution, digest tests; check lint. Verify command help and an integration fixture path from raw Parquet through DuckDB and canonical features to daily JSON.

### Task 3: Requested YTD run and review

- Run from Jan 1 through the latest completed NYSE session (Nov 3). Use pre-period warmup history, all configured production watchlists, and existing context ETFs.
- Audit session counts, null/missing coverage, candidate parity, provenance, outcome horizons, and no-fill labeling. Record limitations from inferred availability and current universe selection.
- Present results and artifact links. This is the review checkpoint; do not widen into optimization or alternative strategies.
