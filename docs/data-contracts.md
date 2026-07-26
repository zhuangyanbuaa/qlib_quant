# Data contracts

This document defines storage ownership for the quant system. Raw facts are immutable;
all analytical and operational stores must be rebuildable from their declared
inputs.

## Storage ownership

| Data | System of record | Writer | Readers | Mutation rule |
|---|---|---|---|---|
| Raw daily prices | Partitioned Parquet | Price ingestion or migration | DuckDB, quality checks, Qlib exporter | Append only |
| Raw macro observations | Partitioned Parquet | Macro adapters | DuckDB, feature builders | Append only |
| Raw news articles | Partitioned Parquet | News adapters | DuckDB, sentiment/risk, strategy scan | Append only |
| Raw SEC/company events | Partitioned Parquet | SEC/company-event adapters | DuckDB, sentiment/risk, strategy scan | Append only |
| Curated features and labels | Partitioned Parquet | Feature pipeline | Backtest, ranker, reports | Rebuild by version |
| Analytical views | DuckDB | Storage bootstrap | Research and reporting | Rebuild; never authoritative |
| Runs, plans, manual orders and fills | SQLite | Operations modules | Reports and journal | Transactional updates |
| Qlib bin | Generated cache | Qlib adapter | Qlib workflows | Delete and rebuild |
| CSV/HTML/Markdown reports | Export artifact | Reporting modules | Human user | Regenerate |
| Backtest artifacts | Export artifact | Backtest reporting | Human, model comparison | Regenerate by run ID |
| Walk-forward metrics and OOF predictions | Export artifact, then registry summary | Model workflow | Reports, model registry | Rebuild by data/config/code version |

Modules must not write through another module's storage adapter. In particular,
research queries may read DuckDB but must not use it to mutate raw Parquet.
SQLite records operational facts and model registry summaries only; it must not
become a cache for historical market facts or backtest internals.

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

## News and SEC event contract

News and event rows are external facts and follow the same ingestion metadata
contract as prices. Raw Parquet is never rewritten.

Raw news lives under:

```text
data/raw/news/year=*/month=*/*.parquet
```

The DuckDB `news_articles` view deduplicates by `dedupe_key`, keeping the latest
fetch when the same URL/title appears in multiple ingestion runs. Every news row
keeps the canonical URL, URL dedupe key, semantic key, raw provider
tickers/topics, matched canonical symbols, event type, severity, sentiment label,
sentiment score, and raw source URL.

Raw SEC/company events live under:

```text
data/raw/company_events/year=*/month=*/*.parquet
```

The DuckDB `company_events` view deduplicates by `event_id`, derived from CIK,
accession number, and form type. SEC events become visible at
`accepted_at_utc`; Alpha Vantage news becomes visible at provider
`published_at_utc`.

Historical risk queries must use `available_at_utc <= cutoff_utc`. High-severity
events may veto a candidate only when the emitted signal can link back to the
raw article URL or SEC filing URL.

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
