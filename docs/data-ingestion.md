# Daily price ingestion

Phase 2 replaces the legacy global-lock downloader with a bounded,
provider-neutral pipeline.

## Data flow

```text
NYSE completed-session clock
  → DuckDB latest date per symbol
  → group by incremental start date
  → bounded provider batches
  → rate limit + daily budget + retry/backoff
  → canonical DailyPrice validation
  → immutable Parquet append
  → DuckDB view refresh
  → quality report and downstream gate
```

Yahoo Finance is the first adapter, not a domain dependency. The service only
knows the `DailyPriceProvider` protocol, so another licensed source can be added
without changing storage or downstream strategy code.

## Completed-session rule

The XNYS exchange calendar supplies actual session closes, including holidays
and early closes. A daily bar becomes eligible 30 minutes after its official
close. Provider rows after the latest eligible session are discarded.

The provider `end` date is treated as exclusive. Incremental downloads overlap
the previous two sessions so upstream revisions can be appended as new raw
versions. The DuckDB view selects the newest fetched version while preserving
all raw history.

## Failure behavior

- Provider calls use configured batches, bounded workers, a sliding one-minute
  rate limit, a daily call budget, exponential backoff, and jitter.
- A failed batch never deletes or replaces last-known-good Parquet.
- SPY and QQQ are mandatory core symbols and are automatically included.
- If a core symbol is not current, the run is `BLOCKED`.
- If the configured fraction of requested symbols fails, the run is `BLOCKED`.
- Non-core partial failures or stale symbols produce `DEGRADED`.
- Clean coverage produces `SUCCESS`.

`BLOCKED` causes the CLI to exit with status 2 so schedulers cannot mistake the
run for success.

## Configuration

Operational limits live in `configs/sources/prices.yaml`:

```yaml
batch_size: 25
max_workers: 2
calls_per_minute: 30
daily_call_budget: 500
overlap_sessions: 2
stale_after_sessions: 1
max_failure_fraction: 0.10
```

The configured market proxies and sector ETFs are stored in the same
`daily_prices` contract. Point-in-time economic releases such as FRED series
will use `MacroObservation` through a separate future adapter.

## Commands

Update the configured market and sector symbols:

```bash
uv run quant data update-prices
```

Update selected symbols; SPY and QQQ are included automatically:

```bash
uv run quant data update-prices --symbols AAPL,MSFT,NVDA
```

Update every symbol already stored:

```bash
uv run quant data update-prices --all-stored
```

Each run writes
`data/reports/quality/price-update-<run_id>.json` with requested, updated,
unchanged, empty, stale, and failed symbols; missing-session counts; provider
calls; rows and files written; gate status; and blocking reasons.
