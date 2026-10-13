# Manual News Research Implementation Plan

> Execute task-by-task using executing-plans. The user explicitly authorized all
> four tasks without intermediate pauses; review checkpoint is after final validation.

**Goal:** Complete quant → research package/prompt → reviewed JSON import → combined
report without requiring news-provider APIs or changing canonical scores/hard rules.

**Architecture:** Manual mode skips external news/SEC acquisition but retains local
known vetoes and pending news gates. Freeze complete selected-symbol quant context
and report hashes in a versioned package. Validate external research with strict
schemas and append its provenance to independent raw Parquet; build separate combined
JSON/Markdown reports, retaining original decision reports and execution eligibility.
External text is research evidence, never authority to change trading permissions.

**Tech Stack:** Python/Pydantic, Arrow/Parquet, existing DuckDB workflows, Typer, pytest.

## Design and scope

Chosen approach: human-run browsing prompt plus a deterministic importer. A plain
Markdown-only workflow is already possible but cannot reliably validate identity,
time or scores. An automatic LLM/API workflow adds account integration outside this
request. Keep the chosen manual handoff; no provider calls or browser/account actions.
Preserve the dirty workspace and do not commit automatically.

### Task 1: Manual news mode

Files: `decision/premarket.py`, `decision/research.py`, `decision/workbench.py`,
`operations/pipeline.py`, `cli.py`, `scripts/run_daily_workbench.py`.

1. Add explicit `--manual-news` to update-daily and daily-workbench/script.
2. Skip news/SEC provider construction and requests; distinguish manual from replay.
3. Preserve price gate, quant features/scores/signal IDs and known HIGH vetoes.
   Force unverified manual coverage pending, never DISABLED/LOW or trade eligible.
4. Test mode separation, zero news calls including configured providers, preserved
   scoring, cache identity and pending gates.

### Task 2: Complete research package and prompt

File: new `decision/manual_research.py`; integrate from workbench.

1. Export versioned package with run/session/price cutoff/order time, full selected
   canonical/calibration rows, research context, selected leverage assessments and
   all positions. State selection counts/limits explicitly.
2. Hash quant reports and the package; prompt embeds portable data and identifiers,
   UTC research bounds, source requirements, immutability and untrusted-page rules.
3. Write JSON response schema and an UNKNOWN/no-evidence editable template. Empty
   selection must be valid. Require both narrative and structured results.
4. Test hashes, completeness, deterministic package and output artifacts.

### Task 3: Strict manual result contract and append-only import

Files: new `domain/manual_research.py`, `storage/schemas.py`, `storage/parquet.py`,
`decision/manual_research.py`, `cli.py`.

1. Define evidence (URL/source/title/publication/retrieval/severity) and per-symbol
   reviews with referenced evidence IDs and research-only conclusions. Evidence may
   not turn an unsuccessful/partial search into LOW coverage. No trade/score fields.
2. `decision import-manual-research --package ... --result ... --reviewed-by ...`
   validates all data before writes: version/hash/run/session/symbol set, references,
   timestamps, unchanged source reports and current canonical price gate. Dry-run
   validates without facts/reports. Reviewer attests provenance, not exhaustive truth.
3. Store full structured result as one provenance-bearing independent raw Parquet
   fact with actual local ingestion availability, human/model/template identities,
   stale/quality flags. Exact payload duplicates reuse the fact; changed revisions
   append. Never overwrite prior facts or imply historical availability.
4. Test rejection cases, metadata, idempotence, revisions and raw immutability.

### Task 4: Combined report, docs and full verification

1. Build new immutable combined JSON/Markdown from frozen quant rows and imported
   evidence. Preserve known HIGH vetoes, separate research priority from quant score,
   mark omitted/unverified symbols UNKNOWN, expired order windows and current data
   health. No write to canonical news coverage, gate/paper inputs or original reports.
2. Link evidence and show both cutoffs; provide `decision combine-manual-research`
   for PIT regeneration using facts available at/before requested cutoff.
3. Document complete copy/paste/save/import workflow, current CLI behavior and errors.
4. Run fixed-fixture unit/integration tests, full pytest, Ruff, diff checks and local
   actual-data smoke through Parquet→DuckDB→features→package→import→combined JSON.
   Use an isolated data root and compare actual raw content hashes.

**Final review checkpoint:** All four tasks and verification complete. This batch
does not implement Phase 7, automatic account execution, notifications, orders or
news API adapters. Real user research content remains a daily operational input.

## Execution record — 2026-10-13

All four tasks completed. The user authorized continuous implementation; existing
workspace changes were preserved and no commit was made.

- Manual mode is available in `data update-daily`, `decision daily-workbench` and
  both daily scripts. News/SEC provider construction and acquisition are skipped.
  Canonical scores/signal IDs, critical-price gates and local HIGH vetoes remain;
  pending manual coverage cannot become DISABLED/LOW or execution eligible.
- Versioned packages, portable prompts, JSON response schemas and unrun templates
  include complete selected-symbol quant context, source report/config snapshots,
  hashes, timing, selection limits, leverage observations and all recorded positions.
  Missing package artifacts or modified source reports invalidate cached reuse.
- Strict reviewed-result import validates complete payloads before appending.
  Tests reject wrong IDs/symbols, score/permission fields, inconsistent citations,
  unsafe URLs, future/non-UTC times, modified quant/config inputs and unrun templates.
  Exact payload duplicates reuse facts; revisions append to independent Parquet.
  Availability is actual import time, not the article's publication time.
- Separate combined JSON/Markdown reports preserve quant facts, distinguish research
  priority from news risk, retain visible prior negative evidence for the same package,
  show missing/stale research and expired order windows, and link original reports.
  PIT regeneration excludes later imports. They never authorize gate/paper execution.

Validation: **294 tests passed**; `ruff check src tests scripts` and `git diff --check`
passed. CLI help and wrapper entry points were checked.

The actual-price smoke replay used the 2026-10-09 data in an isolated root with
hard-linked immutable raw records, separate DuckDB and a SQLite backup. It produced
a 23-symbol package, prompt, schema/template and combined report. Repeating the
pipeline reused its report; repeating import wrote zero additional facts. All 2,236
actual raw files matched their pre-run SHA-256 hashes. Real operational state was
not written. Evidence is in
`data/reports/manual-news-smoke/4f60f8c9-a63b-4ef5-a709-63150841395f/smoke-summary.json`.

The imported smoke result explicitly used **unsearched test fixtures**, not live
news research. Expected statuses were DEGRADED / PARTIAL_RESEARCH, with zero
execution-eligible candidates. Runtime artifacts remain ignored by Git.

The daily runbook and data contracts document the complete prepare → browse → save
JSON → review/import → read combined report workflow. Actual daily browsing results
are user-supplied operational input, not missing implementation. Phase 7 and reliable
intraday-provider integration remain later development work.
