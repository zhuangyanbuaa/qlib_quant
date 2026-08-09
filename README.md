# qlib-quant

A reproducible daily research and decision-support system for a rules-first
Buy-the-Dip strategy. The project is being rebuilt as a modular Python
application; it does not place brokerage orders.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync --dev
uv run quant --help
```

Create local configuration only when needed:

```bash
cp .env.example .env
```

## Development

```bash
uv run pytest
uv run ruff check .
uv run quant --help
```

## Daily manual workflow

After the initial data backfill, the normal daily entry point is:

```bash
uv run quant data update-prices --all-stored
python scripts/run_daily_workbench.py --no-news-risk
```

Open the generated `daily_index.md` under
`data/reports/daily/<date>/<run_id>/`, or launch the local dashboard:

```bash
streamlit run apps/decision_dashboard.py
```

The daily command sequence and optional news, position, paper-trading, and
2x-overlay steps are documented in
[`docs/daily-runbook.md`](docs/daily-runbook.md) and
[`docs/daily-runbook.zh.md`](docs/daily-runbook.zh.md).

## Phase 1 data migration

Legacy CSV data stays local under `legacy/data/csv`. Inventory it before
migration:

```bash
uv run quant data migrate-legacy --dry-run
```

Then migrate valid OHLCV rows, quarantine invalid data, and build DuckDB views:

```bash
uv run quant data migrate-legacy
uv run quant data bootstrap-duckdb
uv run quant data price-range AAPL
```

Storage ownership and timestamp semantics are defined in
[`docs/data-contracts.md`](docs/data-contracts.md).

## Phase 2 incremental updates

Update selected symbols after the latest completed NYSE session:

```bash
uv run quant data update-prices --symbols AAPL,MSFT,NVDA
```

For the complete source, retry, stale-data, and failure-gate behavior, see
[`docs/data-ingestion.md`](docs/data-ingestion.md).

## Phase 3 rules and backtesting

Scan a completed session with the exact strategy code used by backtests:

```bash
uv run quant strategy scan --symbols AAPL,MSFT,NVDA --date 2026-06-26
```

Run the cash-aware baseline and its fixed robustness grid:

```bash
uv run quant backtest run \
  --symbols AAPL,MSFT,NVDA,AMD,AVGO \
  --start 2022-01-03 \
  --end 2026-06-26 \
  --stress
```

The precise hypothesis, timing, fills, portfolio constraints, and known
universe bias are documented in
[`docs/strategy-spec.md`](docs/strategy-spec.md).

Runtime datasets, model artifacts, reports, secrets, and virtual environments
are intentionally excluded from Git. The current architecture and phased
roadmap live in
[`docs/plans/2026-06-28-quant-repo-refactor.md`](docs/plans/2026-06-28-quant-repo-refactor.md).
