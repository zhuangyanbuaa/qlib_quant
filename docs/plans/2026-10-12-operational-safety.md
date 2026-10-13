# Operational Safety Implementation Plan

> Execute sequentially using the executing-plans workflow; review after these three tasks.

**Goal:** Preserve provider budgets across processes, reject unreliable intraday snapshots, and make missing news coverage visible without treating it as low risk.

**Architecture:** SQLite owns atomic operational budget counters; external snapshots and query-coverage observations retain provenance and cutoff semantics. Append coverage observations to raw Parquet, including successful empty queries, and derive news availability independently from observed event severity. Keep canonical price/strategy calculations and existing raw datasets intact.

**Tech Stack:** Python, SQLite, Pydantic, Arrow/Parquet, exchange-calendars, pytest.

**Scope:** The user authorized this batch in the current workspace. Preserve previous uncommitted work. No live provider fetching, scheduling installation, outbound notifications, model changes, strategy threshold changes, automatic orders or commits. Complete all three tasks before the review checkpoint.

### Task 1: Durable provider budgets

**Files:** Create `src/quant_system/ingestion/budgets.py`, `tests/integration/test_durable_budgets.py`; modify `ingestion/reliability.py`, `ingestion/prices.py`, `ingestion/news.py`, `cli.py`.

1. Write tests for restart persistence, separate providers, UTC rollover, conflicting limits, concurrent processes and retry counting. Run `.venv/bin/python -m pytest tests/integration/test_durable_budgets.py -q` and confirm initial failure.
2. Use a SQLite transaction to reserve each attempt before network access. Key counters by provider and UTC day; the smallest configured limit seen that day wins. Old day counters remain auditable. Keep the in-memory budget for isolated tests.
3. Record per-guard attempts separately from the shared total so reports cannot count another process's calls. Wire all production CLI provider guards to the same operational database.
4. Run the durable-budget, reliability and ingestion-service tests. Existing per-process rate limits and bounded backoff remain unchanged; global scheduling/coordination is outside this batch.

### Task 2: Validated intraday snapshots and gate timing

**Files:** Create `src/quant_system/domain/snapshots.py`; modify `decision/gates.py`; extend `tests/unit/test_phase6_gates.py` and create `tests/integration/test_snapshot_gates.py`.

1. Write fixtures for valid snapshots, missing provenance, NaN/inf/nonpositive price, duplicates, stale/future observations, wrong session, holidays, early closes and DST. Assert all unreliable inputs defer and cannot KEEP.
2. Require source/version, fetch/availability times, run ID, stale/quality flags, symbol, quote timestamp and exchange session. Retain invalid rows as explicit per-symbol errors rather than dropping them silently.
3. Anchor preopen and T+30/T+60 cutoffs on the next exchange session's actual open. Reject future availability, quotes older than five minutes, gates run too early or after the order session closes, and non-executable watch-only plans. Missing reliable snapshots remain DEFER; never synthesize intraday facts from daily bars.
4. Preserve snapshot provenance and gate cutoffs in JSON reports. Run gate/CLI tests with fixtures; no broker connection is required.

### Task 3: News query coverage, freshness and daily health

**Files:** Create `domain/coverage.py`, `sentiment/coverage.py`; modify `storage/schemas.py`, `storage/parquet.py`, `ingestion/news.py`, `ingestion/alpha_vantage.py`, `quality/reports.py`, `sentiment/risk.py`, `strategy/buy_the_dip.py`, `decision/premarket.py`, `decision/workbench.py`, `decision/research.py`, `decision/paper.py`, `backtest/workflow.py`, `cli.py`, `scripts/run_daily_workbench.py`; extend coverage/news/decision integration tests and repository docs.

1. Test successful empty queries versus not queried, failed, unavailable, partial and stale coverage; test late coverage invisibility and high-risk veto despite incomplete coverage.
2. Append per-symbol news/SEC query observations with requested interval, completion availability, result and provenance. Disabled/unconfigured sources and missing CIK are unavailable. Warnings or capped responses cannot certify complete coverage. Dry-run does not write coverage facts.
3. Require a recent successful observation for each required channel, covering the requested lookback start and ending within one hour of the cutoff. Apply completion availability before selection. Historic replay must not read observations made after its cutoff.
4. Expose UNKNOWN when low severity cannot be verified by current coverage; keep known HIGH vetoes. Unknown coverage candidates are manual research only, and open gates cannot KEEP such plans. Explicit no-news mode is DISABLED rather than LOW. Surface news data health in the daily index and runner output.
   Missing Alpha Vantage feed is a provider error; response limits produce partial coverage. Bump adapter version with fixed-fixture tests. Gate runs more than five minutes late defer, and first snapshot fetch must also precede its cutoff.
5. Run `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check src tests scripts`, `git diff --check`, and fixed-fixture/local-data Parquet → DuckDB → features/scan → JSON smoke. Verify existing raw files unchanged and document manifest formats and conservative timing policies.

**Review checkpoint:** Report completed behavior, test results, limitations and remaining daily orchestration/Phase 7 work. Stop here.

## Execution record — 2026-10-12

- Tasks 1–3 completed. Production CLI guards now share atomic SQLite budgets;
  process-restart, four-process contention, UTC rollover, conflicting limits and
  retry/report accounting tests pass. Existing rate limits remain per process.
- Snapshot contracts require provenance, valid finite prices, explicit news and
  liquidity state, matching exchange session and ordered UTC timestamps. Gate
  windows use actual exchange opens/closes. Fixtures cover holiday, DST, early
  close, JSON/CSV, stale/future/missing fields and duplicate-symbol cases.
- News/SEC query coverage appends immutable Parquet facts. Empty success is
  distinct from unconfigured, failed, partial, stale and not-queried states.
  Cutoff queries cannot use later observations. Missing feed/response cap
  handling is fixture-tested under Alpha Vantage version `NEWS_SENTIMENT-v2`.
- Canonical strategy output labels disabled news explicitly; unknown coverage
  produces research-only actions, blocks gate KEEP and paper entry, and cannot
  erase an observed HIGH veto. Daily index/runner expose degraded health.
- Full suite: 242 passed. Ruff and `git diff --check` passed. Fixed-fixture smoke
  covers Parquet → DuckDB → canonical prices/features/signals → decision JSON,
  successful empty coverage, late invisibility, unknown restriction and HIGH veto.
- Local October 9 workbench: all four components completed, status DEGRADED,
  one candidate for research, six research rows and zero order-draft candidates.
  None of the 193 stock members had current news/SEC coverage. All 2,235 original
  raw Parquet files retained identical sizes and modification times. Used a
  copied operational database; no live provider calls.
- Review checkpoint reached. Real news/SEC source configuration and acquisition,
  unified daily orchestration, shared rate/run coordination and Phase 7 remain
  separate next steps. Existing inferred historical price availability limitations
  remain; strict snapshot metadata cannot authenticate an unverified source.
