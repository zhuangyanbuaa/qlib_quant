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

Runtime datasets, model artifacts, reports, secrets, and virtual environments
are intentionally excluded from Git. The current architecture and phased
roadmap live in
[`docs/plans/2026-06-28-quant-repo-refactor.md`](docs/plans/2026-06-28-quant-repo-refactor.md).
