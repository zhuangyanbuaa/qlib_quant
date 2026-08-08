# Technical closure

This branch closes non-runtime technical gaps that were named in the refactor
plan but intentionally left out of the Phase 0–6 daily workbench.

Technical closure must not broaden the system into automatic trading. Each item
is either read-only decision context, a safety/reproducibility improvement, or
an explicit deferral.

## Scope

1. LightGBM daily premarket ranking context.
2. FinBERT production cache, timeout, and `DEGRADED` fallback status.
3. Qlib export and golden tests, or explicit deferral if Qlib is not needed.
4. Chronos remains deferred.

## LightGBM daily ranking context

Status: implemented.

`quant decision premarket` now accepts:

```bash
quant decision premarket --model-ranking
quant decision premarket --no-model-ranking
```

The default CLI behavior attaches a read-only `model_rank_context`. The ranker:

- trains only on historical baseline candidates whose label end session is at
  or before the signal session;
- scores current baseline and calibration rows when enough mature training rows
  exist;
- writes `model_score`, `model_rank`, and `model_rank_status` onto report rows;
- never changes `recommended_action`, `calibration_action`, or
  `manual_review_allowed`;
- returns explicit statuses such as `INSUFFICIENT_TRAINING_ROWS`,
  `UNAVAILABLE_LIGHTGBM_NOT_INSTALLED`, or `SKIPPED_NO_CANDIDATES` instead of
  blocking daily reports.
- displays model rank, model score, model status, training rows, minimum rows,
  scored rows, and target column in Markdown and the local Streamlit dashboard.

Smoke result on the latest local 2026-07-24 report:

```text
model_rank_context.status = INSUFFICIENT_TRAINING_ROWS
training_rows = 29
minimum_train_rows = 30
```

This is the intended behavior: model context is visible, but the system does
not pretend an under-sampled model is actionable.

## FinBERT production cache / timeout / degraded status

Status: implemented.

- cache by `article_id` or `dedupe_key` plus model/tokenizer version;
- avoid repeated inference for old articles;
- enforce a per-run timeout;
- return `sentiment_status: DEGRADED` and deterministic rule/keyword fallback
  when inference is unavailable or too slow.
- write sentiment cache hits, cache misses, degraded count, and
  `sentiment_status` in the news update quality report;
- add `sentiment_status:DEGRADED` to article quality flags when fallback is used.

The cache is file-backed under:

```text
data/cache/sentiment/
```

The raw news schema is unchanged. The persisted article still stores the final
sentiment label/score, while degradation metadata stays in quality reports and
article `quality_flags`.

## Remaining closure items

### Qlib export / golden tests

Deferred unless Qlib workflows are actively needed. If implemented:

- export curated Parquet into rebuildable `data/qlib/`;
- compare DuckDB/canonical feature outputs against Qlib feature outputs on a
  fixed fixture;
- never treat Qlib bin as a system of record.

### Chronos

Explicitly deferred. It remains an optional Phase 8 experiment and does not
block daily workbench quality.
