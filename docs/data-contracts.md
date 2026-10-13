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
| Reviewed security lifecycle | Partitioned Parquet | Validated local manifest import | Price updates, dated universes, decision quality gates | Append only |
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

### Historical price reads and daily decision cutoffs

`daily_prices` is a latest-version operational view, not a historical replay
input. Historical price queries require an explicit timezone-aware
`cutoff_utc`. They filter `raw_daily_prices` by
`available_at_utc <= cutoff_utc` **before** selecting the latest fetched version
of each symbol/session, retaining source, version, timestamps, run ID and flags.

The canonical feature loader constructs each session's features at that
session's exchange close plus the 30-minute availability delay. Late initial
publications and revisions can change later features, but cannot change
previously emitted decision rows. Scans, backtests and model candidate datasets
use this same implementation; selecting everything visible at a backtest's end
and then computing earlier signals is not sufficient.

Daily report `data_cutoff_utc` and signal logical time use the availability
cutoff, which may be equal. The bar timestamp remains the actual close, and the
earliest order time remains the next exchange session's open. Exchange calendars
handle holidays, early closes and daylight saving time.

SPY, QQQ and the selected market benchmark must have reliable current-session
prices. Missing, not-current or explicitly stale critical prices produce
`BLOCKED`; CLI commands exit with code 2, and the daily runner propagates that
status before creating recommendation reports. Explicit stale/invalid candidate
bars cannot satisfy the dip or confirmation rule. Zero volume alone is not a
suspension detector; securities lifecycle checks remain a separate follow-up.

**Availability limitation:** legacy records and historical Yahoo backfills
include inferred availability timestamps. The replay guarantee is relative to
the recorded metadata. This does not reconstruct missing historical publication
or revision times, nor undo retroactive corporate-action adjustments. Existing
raw records are not relabeled or rewritten; such datasets remain subject to
their stated research and universe-bias limitations.

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

## Security lifecycle and dated membership

Reviewed lifecycle events live in `data/raw/security_lifecycle/year=*/month=*/*.parquet`.
Each event has `event_id`, a local `security_id`, old `symbol`, `event_type`,
`effective_session`, optional successor symbol/identity, source URL and the full
ingestion metadata contract. The `local:` identity namespace is an internal
tracking key, not a verified CUSIP/ISIN or a complete historical security master.
Corrections append versions of the same event; readers filter availability before
selecting the latest visible fetch. DuckDB exposes `raw_security_lifecycle` and
`query_security_lifecycle(cutoff_utc=...)` as rebuildable analytical access.

Both effective date and knowledge cutoff must pass before a lifecycle event applies.
Historical decisions use their exchange-close-plus-delay cutoff; operational price
updates use the current New York date and run start as their knowledge cutoff.
Renames retain identity; share conversions require a separate successor identity.
Merger acquirers are never automatic continuations of the acquired security.
Existing prices are never renamed, rewritten or joined to successor price series.
Inactive noncritical requests are reported separately from provider failures;
inactive or renamed critical sources still block. Derived quality gates reject
known nontradable rows without mutating their raw facts.

Unknown cessation dates may conservatively use the review date, with
`effective_session_unknown_review_date_only`; closing dates are not assertions of
the last trading session. A diagnostic-only or stale lifecycle fact blocks use
instead of granting tradability. The reviewed October 12 import preserves the
diagnostic fetch time as conservative availability, so it does not retrospectively
repair earlier decisions or fill historical price gaps.

Watchlist membership includes both `start_date` and `end_date`. Inventory access
remains available for backfill; decision loaders select only active dated members
and resolve visible lifecycle relationships. These fields do not reconstruct a
historical universe that was never recorded.

Yahoo normalization revision `adapter-3` distinguishes padded rows, action-only
rows and invalid/nonfinite OHLCV. Missing required columns are isolated per symbol.
Action-only rows do not create prices or fabricate company-event facts; the update
report retains their count. Finite zero-volume bars retain `zero_volume_review`;
volume alone does not establish suspension, which requires a lifecycle fact.

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

## Operational budgets, snapshots and news coverage

Provider request reservations live in SQLite `provider_call_budgets`, keyed by
provider and UTC day. Every instrumented attempt, including a retry, reserves
budget atomically before calling the provider. Counts survive process restarts;
the smallest limit configured for that provider/day wins. Reports use each guard's
own attempt counter, independently of the shared total. This is a local policy:
uninstrumented requests and counters from before this upgrade are not imported.
Rate limiting remains bounded per process. Mutating ingestion/import commands and
the daily pipeline share a nonblocking OS lock under the selected data root.
Scheduling remains separate work.

Broker JSON/CSV snapshots must satisfy `IntradaySnapshot`: symbol, positive finite
`last_price`, `quote_at_utc`, `session_date_ny`, source/version, fetched and available
UTC times, ingestion run ID, stale status and quality flags. Include explicit
`news_risk` and `liquidity_status`; their default UNKNOWN cannot KEEP. Quote date
must match the New York session and quote ≤ availability ≤ fetch. CSV quality
flags use a JSON array string. Invalid or duplicate rows stay visible as per-symbol
errors. Existing short snapshots without provenance now defer.

Preopen and T+30/T+60 gates use the next exchange session's actual open. Quotes,
fetches and availability must be at or before the gate cutoff; quotes may be at
most five minutes old. Gate execution must occur between its cutoff and five
minutes afterward, before the session's actual close. Wrong-session, stale,
future, unverified and watch-only plans cannot KEEP. Report rows retain provenance
and reasons; metadata retains the actual cutoff and order session. Intraday gates
remain forward-only and do not infer fills from daily OHLCV.

Query coverage is an external observation stored append-only under
`data/raw/news_coverage/year=*/month=*/*.parquet`. It includes symbol, channel
(`news`/`sec`), requested UTC interval, result (`SUCCESS`, `EMPTY`, `FAILED`,
`UNAVAILABLE`, `PARTIAL`), returned count, reason and full ingestion metadata.
Coverage becomes visible at query completion, including empty and failed attempts.
Provider warnings, capped responses, missing mappings/CIK and absent sources cannot
certify complete coverage. An empty result represents a successful bounded query,
not proof of absence of every possible external event. Dry-run does not write facts.

Both channels require a successful visible observation covering the lookback start,
with query interval end at most one hour before cutoff. Historical replay cannot
use a later query to certify earlier coverage. Known HIGH severity still vetoes;
otherwise unverified low severity is displayed as UNKNOWN, not LOW. Such candidates
are research-only and cannot enter paper positions. Explicit no-news operation
is displayed as DISABLED. Daily index and runner expose data health and DEGRADED
status independently of completed component execution.

Alpha Vantage adapter `NEWS_SENTIMENT-v3` rejects a missing feed, flags capped and
topic-filtered responses, and queries exactly one ticker per coverage request.
Multiple ticker filters represent intersection, so batching cannot certify union
coverage. Premium/invalid-key notices are permanent UNAVAILABLE failures with
redacted messages, not retried transient errors. Unmapped companies and absent SEC
CIK are recorded as UNAVAILABLE without spending request budget on those queries.
Original article/event schemas and raw records remain unchanged.

## Reviewed company identity and daily pipeline

Reviewed identity facts use independent schema version 1 under
`raw/company_mappings/year=*/month=*/*.parquet`. Import JSON has a `mappings` array:

```json
{
  "mappings": [{
    "mapping_id": "review-aapl-20261013",
    "symbol": "AAPL",
    "cik": "0000320193",
    "aliases": ["Apple"],
    "source_url": "https://www.sec.gov/Archives/edgar/data/320193/",
    "source": "manual_sec_review",
    "source_version": "review-1",
    "fetched_at_utc": "2026-10-13T00:00:00Z",
    "available_at_utc": "2026-10-13T00:00:00Z",
    "ingestion_run_id": "00000000-0000-4000-8000-000000000001",
    "is_stale": false,
    "quality_flags": []
  }]
}
```

The example shows format only; actual imports require reviewed provenance and an
actual ingestion run ID. CIK is 1–10 nonzero ASCII digits, normalized to ten places.
Future knowledge/fetch times are rejected. The full manifest validates before any
append, exact duplicates skip, and corrections remain new records. Visible reviewed
facts supersede config entries; stale/invalid revisions remove unsafe mappings.
Only identity-preserving symbol changes inherit old company identifiers. Explicit
current-symbol mappings win; share conversions require their own reviewed identity.

Report metadata distinguishes `price_cutoff_utc` from `news_cutoff_utc`.
The default historical cutoff is unchanged. Explicit forward news cutoff must be
at/after the price cutoff, at/before report generation, and before the earliest
order session's actual open. Later news cannot alter price features or `signal_id`.
Rows carry a separate `decision_id` derived from the parent signal and news cutoff;
gates/paper match that identity and retain parent signal fill deduplication.

SQLite `pipeline_runs` and `pipeline_steps` track operational progress only. Request
keys include session, live/replay mode, UTC half-hour window, selection/config/code
fingerprints and public source readiness. Successful acquisitions survive restart;
interrupted/failed acquisition attempts use new run IDs. Retry never changes raw
records or resets provider budgets. Cached reports require intact component artifacts,
unchanged raw inventory and a passing canonical critical-price check. Changed raw
inventory rebuilds reports while retaining successful acquisition stages. `--retry`
explicitly repeats acquisition. Live windows must be the current UTC half-hour
boundary; replay skips both providers and respects historical knowledge time.
Pipeline manifests expose stage results separately from execution status: a stage
can execute successfully while its data result is DEGRADED. Price blocking prevents
report generation; unavailable news permits only a degraded research report.

## Manual web research contract

Manual mode (`--manual-news`) skips external news/SEC acquisition, uses the canonical
quant implementation and preserves known local HIGH vetoes. Even if no-news was
also requested, manual mode forces pending coverage rather than DISABLED/LOW. Manual
mode is included in pipeline request identity. Pending source health remains
DEGRADED; no candidate becomes execution-eligible through manual research.

`manual_research_package.json` version `manual-research-package-v1` freezes selected
quant rows, strategy configuration, all positions, context, selection limits and
source report SHA-256 hashes. The package has its own checksum, a run-derived package
ID, signal session, price cutoff, generation time, earliest order time and prompt
version `manual-news-prompt-v1`. Canonical reports include a strategy config snapshot.
Package loading rederives quant context and compares configuration against the frozen
reports; hashes detect file changes and are not a cryptographic signature or proof
that an external claim is true. All source reports must share the package directory.

The generated prompt embeds portable data, JSON schema and an editable unrun template.
`manual-research-result-v1` requires package/run/hash/timing identities, actual analyst
system/model version, overview, evidence and symbol reviews. Evidence includes source
name, HTTP(S) URL, title, summary, symbol, severity, publication time (nullable), actual
UTC retrieval time and explicit background status. A review references evidence IDs
and contains coverage (`SEARCHED`, `PARTIAL`, `UNAVAILABLE`, `NOT_SEARCHED`), observed
risk (`HIGH`, `MEDIUM`, `UNKNOWN`) and research priority (`FOCUS`, `WATCH`, `SKIP`).
Scores, execution permissions and order fields are forbidden in the result schema.
HIGH/MEDIUM review claims require matching cited evidence for the same symbol.

Validate the entire result before appending: schema/version/identity, source hashes,
symbol membership, unique evidence/review IDs, citations and time order. Retrieval
must follow package creation and not exceed the result news cutoff, which must be
at/before result generation and import time. Publication cannot follow retrieval;
older-than-requested evidence must be background_only. Result creation more than
24 hours after package creation requires a fresh package. Unrun templates reject.
Import and combination both recheck canonical critical prices. Source/provenance
attestation requires an explicit reviewer identity; it does not certify exhaustive
news coverage. Partial symbol results are accepted with omitted symbols left UNKNOWN.

Reviewed results are separate schema version 1 facts under `raw/manual_research/`.
They retain the full normalized result JSON and digest, review/package/parent run IDs,
reviewer, signal session and full ingestion metadata. `source=manual_web_research`;
source_version includes result and prompt versions. `fetched_at_utc` and
`available_at_utc` are actual local import time; individual web retrieval timestamps
remain in the payload. Import never backdates availability to article publication.
Unknown publication and research-only/uncertified coverage flags remain visible.
Exact normalized payload duplicates reuse observations; changed revisions append.
Successful raw records are never overwritten. A validated raw append survives an
interrupted report write; rerunning reconstructs the report without a duplicate fact.

New combined reports preserve source quant rows and are separate from original
reports, gate inputs, provider news facts, coverage observations and paper state.
They show price cutoff, knowledge cutoff, actual external research cutoff, lineage,
missing/stale reviews, order-window expiration and zero execution-eligible candidates.
No LOW is inferred from an empty web search. Local HIGH vetoes and visible prior
negative evidence for the same package cannot be silently erased by later revisions.
Research is stale after one hour or if imported stale. Knowledge cutoff must be at
or after package generation and at/before actual run time; only imported observations
available at/before that cutoff may appear. Markdown narratives remain human-reviewed
model interpretation, distinct from the frozen quant facts.

## Reconciliation

Every migration emits:

- a per-file schema and quality report;
- a per-symbol before/after row-count and date-range comparison;
- a JSON summary with schema, stale-symbol, quarantine, and output-file counts.

Phase 1 is accepted only when all non-quarantined symbols match on deduplicated
row count and date range.
