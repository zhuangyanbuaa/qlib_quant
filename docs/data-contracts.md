# Data contracts

This document defines storage ownership for Phase 1. Raw facts are immutable;
all analytical and operational stores must be rebuildable from their declared
inputs.

## Storage ownership

| Data | System of record | Writer | Readers | Mutation rule |
|---|---|---|---|---|
| Raw daily prices | Partitioned Parquet | Price ingestion or migration | DuckDB, quality checks, Qlib exporter | Append only |
| Raw macro observations | Partitioned Parquet | Macro adapters | DuckDB, feature builders | Append only |
| Raw news and SEC events | Partitioned Parquet | News and SEC adapters | Sentiment and event pipelines | Append only |
| Curated features and labels | Partitioned Parquet | Feature pipeline | Backtest, ranker, reports | Rebuild by version |
| Analytical views | DuckDB | Storage bootstrap | Research and reporting | Rebuild; never authoritative |
| Runs, plans, manual orders and fills | SQLite | Operations modules | Reports and journal | Transactional updates |
| Qlib bin | Generated cache | Qlib adapter | Qlib workflows | Delete and rebuild |
| CSV/HTML/Markdown reports | Export artifact | Reporting modules | Human user | Regenerate |

Modules must not write through another module's storage adapter. In particular,
research queries may read DuckDB but must not use it to mutate raw Parquet.

## Daily-price contract

Primary key for the curated view:

```text
symbol, session_date_ny
```

Raw records retain multiple source versions. The DuckDB `daily_prices` view
selects the record with the latest `fetched_at_utc`; raw history remains intact.
Every persisted row includes:

- source and adapter version;
- fetch and point-in-time availability timestamps in UTC;
- ingestion run ID;
- stale status and quality flags;
- an explicit adjusted-price flag.

## Legacy migration

`legacy/data/csv` is a local, read-only migration source. Only adjusted OHLCV
columns are retained. Legacy ATR, ADX, macro joins, sector joins, and fundamental
fields are discarded because their calculation version or true availability
time cannot be reconstructed safely.

Legacy timestamps use conservative inferred semantics:

- `timestamp_utc`: 16:00 America/New_York converted to UTC;
- `available_at_utc`: 16:30 America/New_York converted to UTC;
- `fetched_at_utc`: restored file modification time, never earlier than
  `available_at_utc`.

These inferences are recorded in `quality_flags`.

Files with missing required columns, invalid dates, non-finite values,
impossible OHLC relationships, negative/non-integral volume, or symbol mismatch
are copied to `data/quarantine/legacy_prices/<run_id>`. Files with at most 1%
invalid rows retain their valid history and quarantine only the bad rows; files
above that threshold are quarantined in full. The original file is never
changed. Each quarantined copy has a sidecar reason file.

Quarantined data may return only by correcting the source copy and starting a
new migration run. It must never be edited directly inside quarantine.

## Reconciliation

Every migration emits:

- a per-file schema and quality report;
- a per-symbol before/after row-count and date-range comparison;
- a JSON summary with schema, stale-symbol, quarantine, and output-file counts.

Phase 1 is accepted only when all non-quarantined symbols match on deduplicated
row count and date range.
