# Repository rules

These constraints apply to every implementation in this repository.

- When executing a written plan in `docs/plans/`, follow it task-by-task and
  stop at review checkpoints rather than widening scope opportunistically.
- Raw Parquet is append-only. Never update or delete successful raw records.
- Every external fact must retain source, adapter version, fetch time,
  availability time, ingestion run ID, stale status, and quality flags.
- Historical decisions may only read records whose `available_at_utc` is at or
  before their data cutoff.
- Signal time, earliest order time, fill time, and exit time are distinct.
- Every completed Phase must preserve an end-to-end smoke path from local data
  through Parquet, DuckDB, features, strategy scan, and JSON output.
- Scan and backtest code must call the same canonical feature and rule
  implementation.
- DuckDB and Qlib are rebuildable analytical layers, never systems of record.
- SQLite is reserved for operational state and human trading records.
- Models may rank or veto candidates but may not bypass hard strategy rules.
- A missing or stale critical price source must block recommendations.
- Provider adapter behavior or dependency-version changes require updated
  `source_version` metadata and tests against fixed fixtures.
- Walk-forward validation must use purging/embargo tests; training labels or
  feature windows must never overlap validation/test cutoffs.
- Never commit runtime data, model weights, reports, credentials, or local
  virtual environments.
- Every schema, timing, execution, or strategy change requires automated tests.
