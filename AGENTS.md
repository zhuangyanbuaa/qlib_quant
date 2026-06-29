# Repository rules

These constraints apply to every implementation in this repository.

- Raw Parquet is append-only. Never update or delete successful raw records.
- Every external fact must retain source, adapter version, fetch time,
  availability time, ingestion run ID, stale status, and quality flags.
- Historical decisions may only read records whose `available_at_utc` is at or
  before their data cutoff.
- Signal time, earliest order time, fill time, and exit time are distinct.
- Scan and backtest code must call the same canonical feature and rule
  implementation.
- DuckDB and Qlib are rebuildable analytical layers, never systems of record.
- SQLite is reserved for operational state and human trading records.
- Models may rank or veto candidates but may not bypass hard strategy rules.
- A missing or stale critical price source must block recommendations.
- Never commit runtime data, model weights, reports, credentials, or local
  virtual environments.
- Every schema, timing, execution, or strategy change requires automated tests.
