# Session Automation and Validated Ranking Implementation Plan

> Execute task-by-task with executing-plans. The user authorized items 2–5
> continuously; the review checkpoint follows all tasks and validation.

**Goal:** Add premarket refresh/notification, forward intraday collection and
T+30/T+60 jobs, a weekly review, and configurable validated daily model context.

**Architecture:** Keep the installed exchange-calendar polling LaunchAgent and
durable SQLite attempts/outbox. Additional jobs use separate stable identities and
reuse canonical workflows. Append provenance-bearing snapshots to Parquet before
gate cutoffs. Weekly reports summarize existing operational records without
inventing performance. Model activation requires compatible OOF evidence; failures
fall back to rules. Preserve manual-news pending gates and existing dirty checkout.

**Tech Stack:** Python/Pydantic, exchange-calendars, Arrow/Parquet, SQLite, stdlib
HTTPS, existing LightGBM/OOF workflows, Typer, pytest and launchd.

## Task 1: Configurable validated model context (item 5)

Modify `operations/pipeline.py`, `operations/scheduling.py` and daily config; add
`models/activation.py`. Validate the configured model/strategy, completed comparison
and PIT-compatible registry/evaluation artifacts before forwarding model settings.
Include model policy/evidence in cache identity and report provenance. Keep optional
failure nonfatal and actions unchanged. Test eligible/ineligible, future evidence,
config mismatch and changed cache policies using fixed fixtures.

## Task 2: Forward intraday facts (item 3 foundation)

Add `ingestion/intraday.py`, source config and SecretStr Alpaca credentials. Support
validated broker JSON import and optional Alpaca latest trades with bounded durable
budgets, rate limits, no redirect/secret logging and versioned fixture parsing.
Append snapshots via `storage/parquet.py`/`schemas.py`; local availability must not
be backdated to a provider quote. Select only facts available before gate cutoff.
Alpaca trade-only data cannot assert news/liquidity clearance; IEX coverage is
partial. Missing providers continue conservative DEFER. Test immutable storage,
time/session/duplicate/quality rejection, provider failures and PIT selection.

## Task 3: Session jobs (items 2–3)

Add `operations/session_jobs.py`; extend strict daily scheduling configuration and
existing CLI tick/status. Schedule premarket pipeline at next-open minus 60 minutes,
snapshots just before preopen/T+30/T+60, canonical preopen at -10 and gates at +30/+60.
Jobs expire after their bounded exchange windows; report selection must match signal
and order session and precede gate cutoff. Use the existing scheduler/pipeline locks,
bounded attempts, crash recovery and destination-scoped outbox. Do not place orders
or fabricate paper fills. Test DST/early close/holiday, independent identities,
deduplication, late wake, missing prices/plans, snapshot cutoff and notifications.

## Task 4: Weekly review (item 4)

Add `operations/weekly.py`. Trigger close+60 on the last NYSE session of each ISO week,
including Friday holidays. Write immutable JSON/Markdown for actual week/session
bounds: report/candidate counts, manual versus paper fills, matched signal reference
slippage (or explicit unavailable), data exceptions and outstanding manual research.
No win rate or profit claims without reconstructable closed trades. Test boundaries,
missing/malformed artifacts, PIT journal records and holiday deduplication.

## Task 5: Integration, installation and documentation

Enable the selected session jobs in `configs/operations/daily.yaml`; document provider
setup, task times, weekly interpretation and model readiness. Keep credentials and
runtime artifacts ignored. Verify the installed agent registration/configuration,
fixture end-to-end local prices→Parquet→DuckDB→features→daily→snapshots→gates→weekly
JSON, full suite, Ruff and diff checks. Real intraday collection requires a configured
source; fixture success must be distinguished from live provider validation. No
automatic commits/push. Tests run with injected transports and isolated data roots.

Commands: `.venv/bin/python -m pytest tests/unit/test_model_activation.py
tests/unit/test_intraday_ingestion.py tests/unit/test_session_jobs.py
tests/unit/test_weekly_review.py tests/integration/test_session_automation.py -q`,
then `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check src tests scripts`, `git diff --check`.
Expected: all pass; optional unavailable sources/models remain visible and conservative.

## Execution record — 2026-10-13

Tasks 1–5 completed. Final full suite: **350 tests passed in 30.27 seconds**;
Ruff and diff checks passed. Existing uncommitted scheduling/Telegram work was
preserved. No automatic commit/push or credential/runtime staging was performed.

Automatic pipeline supports off/validated model policy. Explicit review run IDs,
compatible configs, PIT registry timestamps, mature labels, fold embargo/training
counts, matching OOF sample sets, target/session checks and recomputed OOS comparison
guard activation. Evidence/config hashes invalidate incompatible cache reuse.
Missing evidence or inference failure leaves rule actions and manual news gates intact.

Added append-only broker/Alpaca intraday facts and a rebuildable DuckDB raw view.
Tests verify historical selectors filter visibility before revision selection,
import availability cannot be backdated, and later failed batches preserve earlier
successful records. Alpaca calls are single-attempt, rate/day bounded across process
restarts, with no secret-bearing redirects/errors. Trade observations retain UNKNOWN
news/liquidity and IEX's partial coverage. The user explicitly chose no current
intraday provider, so configured provider remains none.

Session tasks include premarket (-60), snapshots two minutes before preopen/T+30/T+60,
canonical preopen (-10), gates (+30/+60), and last-session-of-ISO-week close+60 review.
Dedicated operational keys/locks/outbox preserve bounded retry and crash recovery;
expired gates do not backfill. Pending outbox rows cannot be starved by recent SENT
rows. Tests cover DST, Friday holiday, plan/cutoff mismatch, duplicates, failure
budgets, late wake and interrupted jobs after their execution window.

The isolated integration smoke ran local price Parquet through DuckDB, features,
canonical daily/manual package generation, imported three broker fixtures, generated
three pending-news gate reports and weekly JSON. Repeated gate ticks sent once each.
Existing raw files retained their SHA-256 hashes. These were explicit broker fixtures
and fake Telegram transport, not live intraday-provider validation or real news research.

Weekly reports select latest visible daily indices, separate manual/paper fills,
compare linked pre-fill BUY plan limits, report missing data/operational exceptions
and outstanding research imports. They do not infer closed-trade P&L or win rates.

During implementation the existing LaunchAgent was temporarily unloaded to prevent
partially implemented code from executing. After final checks it was reinstalled;
live status confirms registered=true, Telegram configured and all five session jobs
enabled. The actual source check returns UNAVAILABLE / intraday_source_not_configured
with zero rows, and model-status returns AWAITING_VALIDATION_REVIEW. The earlier
live Telegram connection/daily notification remain confirmed; new gates/weekly jobs
await their actual calendar windows rather than fabricated live runs.

At verification (2026-10-13 14:10 UTC), next forward snapshot is 14:28 UTC and
T+60 is 14:30 UTC (Tokyo 23:28/23:30); next after-close trigger is 21:00 UTC
(Tokyo 2026-10-14 06:00), next premarket is 2026-10-14 12:30 UTC (Tokyo 21:30).
No automatic LLM/account actions or orders were added. Real intraday source setup,
human news review and review of a qualifying model comparison remain operational inputs.
