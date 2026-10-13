# News Sources and Daily Pipeline Implementation Plan

> Use the executing-plans workflow; the user authorized all four tasks in this batch. Preserve the current workspace and stop after the final review.

**Goal:** Check usable news sources, support reviewed company mappings and bounded live verification, separate news knowledge time from price signal time, and provide one auditable daily entry point.

**Architecture:** Reuse canonical ingestion and decision services. Reviewed company mappings are append-only external facts; SQLite records pipeline progress and idempotency, while an OS file lock prevents concurrent mutation. Price features remain fixed at their historical cutoff, and explicitly selected forward news refreshes have a separate cutoff without altering historical signals.

**Tech Stack:** Python, Pydantic, SQLite, Parquet, exchange-calendars, Typer, pytest.

### Task 1: Readiness and reviewed company mappings

- Add `ingestion/source_health.py`, `domain/companies.py`, mapping Parquet schema/access and `data check-sources` / `data import-company-mappings` commands.
- Never expose keys or contact strings. Report enabled/configured/entitlement-unverified separately, and identify missing mapping/CIK and resolved codes. Do not invent identities for the remaining universe.
- Merge reviewed visible mapping revisions with existing configuration, retaining sources and availability. Tests cover secret redaction, invalid CIK, late revisions, immutable imports and idempotence.

### Task 2: Bounded news acquisition validation

- Factor shared factories from the existing CLI into `ingestion/factory.py`; add `data verify-news` with a maximum three-symbol sample and existing durable budgets/rate limits.
- Official Alpha Vantage docs checked October 13 label NEWS_SENTIMENT Premium and multi-ticker filters as simultaneous intersection. Fix union coverage assumptions: request one symbol per call, mark topic-filtered results partial, classify premium/invalid-key responses as permanent failures, bump adapter version and fixed fixtures. Do not buy a subscription or use fabricated credentials.
- Capture capability and coverage results even when credentials are unavailable. If real credentials become available, run AAPL/MSFT/NVDA once; otherwise preserve an explicit unavailable validation result and complete fixture verification.

### Task 3: Independent news cutoff

- Add explicit optional news cutoff to premarket, research and daily-workbench workflows/CLI. Default remains historical price cutoff. Forward refresh must be at/before generated time and before earliest order time; do not allow changing historical scan/backtest cutoffs.
- Reports record both price and news cutoffs, with a separate forward news decision identifier. Candidate price signal identity/time remains immutable. Gate and paper use the forward decision identity when present.
- Tests cover later-known vetoes, coverage becoming visible only at news cutoff, future/post-order rejection and unchanged price features/signal IDs under a refresh.

### Task 4: Unified daily runner and durable progress

- Create `operations/pipeline.py` / `operations/registry.py`, `data update-daily` and a thin `scripts/run_daily_pipeline.py` wrapper. Keep existing report-only runner usable.
- Stages: readiness → price update → news/SEC update → quality validation → report. Persist run ID, stage attempts/status, artifact paths and structured errors. Use exchange calendar and explicit mode: live forward refresh vs historical replay with acquisition skipped.
- Process lock auto-releases on crash. Idempotency keys include session, symbol/config selection and refresh window. Successful completed runs return their artifacts; interrupted/failed acquisition stages retry under fresh attempt IDs, never overwrite raw files. Revalidate price/news at decision stage; price failures stop reports, news failures yield research-only degraded reports.
- Test concurrent lock exclusion, crash recovery, failure/no misleading report, repeat-run provider-call avoidance and complete Parquet → DuckDB → features → report JSON path. Run full pytest, Ruff, diff checks and local replay smoke without rewriting raw data.

**Review checkpoint:** Complete code/tests/docs for Tasks 1–4; report any credential-blocked live validation honestly. No Phase 7 jobs, notifications, orders, model changes or automatic commits.

## Execution record — 2026-10-13

Tasks 1–4 are implemented and stopped at this review checkpoint. Existing dirty
workspace changes were retained; no commit was made.

- Task 1: Redacted source readiness and append-only reviewed mapping imports,
  manifest validation before append, idempotent imports, PIT revisions and
  conservative identity handling. Explicit renamed-company identities win over
  inherited aliases; stale revisions cannot silently fall back to old mappings.
- Task 2: Shared factories and three-symbol verification; Alpha Vantage v3 fixtures
  cover one query per mapped ticker, topic-filtered partial coverage, redacted
  entitlement errors and no retries for permanent failures. Missing mappings/CIK
  avoid request budget consumption. The AAPL/MSFT/NVDA validation report is
  `data/reports/quality/news-verification-20261013.json`: verification UNAVAILABLE,
  0 external calls, 0 articles/events, 6 UNAVAILABLE coverage facts in one new raw
  file. This explicitly records missing credentials, not successful connectivity.
- Task 3: Independent optional forward news cutoff in all four relevant CLI/report
  entry points; immutable price signal identity and separate news decision identity.
  Fixtures cover PIT visibility, later HIGH veto, future/post-order rejection,
  gate identity matching and ISO timestamps with Z or explicit timezone offsets.
- Task 4: Canonical live/replay runner, durable SQLite stages and attempt counts,
  shared process lock, bounded acquisition, crash resume, repeat-run caching and
  report reconstruction. Stale custom refresh windows are rejected. Missing report
  components or changed raw inventory invalidate report reuse without repeating
  successful acquisition. Critical-price validation is canonical even on reuse.

Validation: `266 passed`; `ruff check src tests scripts` and `git diff --check`
passed. A local replay on the actual 2026-10-09 Parquet data used an isolated DuckDB
and a SQLite backup, preserving real operational state. All four components
(premarket/research/leverage/positions) completed, repeated requests reused the
report, and SHA-256 comparisons confirmed all 2,236 raw files unchanged during
replay. The result remained DEGRADED / RESEARCH_ONLY_NEWS_UNAVAILABLE with zero
execution-eligible candidates. Replay artifacts and compact evidence are under
`data/reports/pipeline-smoke/`; runtime artifacts remain ignored by Git.

Runbook and data contracts document the commands, source permissions, mapping
format, separate cutoffs, cache/retry behavior and statuses.

**Still pending external validation:** Configure an authorized usable news source
and a real SEC contact locally, then repeat the bounded probe. A free Alpha Vantage
key does not establish NEWS_SENTIMENT entitlement; the checked official documentation
labels the endpoint Premium. No subscription purchase was attempted. Mapping/CIK
gaps across the larger universe need reviewed facts, not fabricated identities.
Phase 7 scheduling and notifications remain outside this batch.
