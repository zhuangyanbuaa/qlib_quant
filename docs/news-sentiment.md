# Phase 4 news and sentiment

Phase 4 builds a point-in-time news risk layer for the rules strategy. It is a
veto and annotation system only; it does not rank candidates and cannot bypass
the Buy-the-Dip hard rules.

## Sources

- Alpha Vantage `NEWS_SENTIMENT` supplies market news by ticker, time range, and
  optional topics.
- SEC `data.sec.gov/submissions/CIK##########.json` supplies recent filing
  events. The adapter requires a real contact `QUANT_SEC_USER_AGENT` and keeps
  requests below the configured fair-access limit.

`configs/sources/news.yaml` owns rate limits, daily budgets, FinBERT settings,
risk lookback, and ticker/CIK/alias mappings.

## Storage

Raw facts are append-only:

- `data/raw/news/year=*/month=*/*.parquet`
- `data/raw/company_events/year=*/month=*/*.parquet`

DuckDB rebuilds deduplicated analytical views:

- `news_articles`, deduped by `dedupe_key`
- `company_events`, deduped by `event_id`

Historical scans query only records with `available_at_utc <= cutoff_utc`.
For historical Alpha Vantage backfills, `available_at_utc` is the article's
provider publication timestamp. SEC events become available at
`accepted_at_utc`.

## Deduplication and risk

Each article stores:

- canonical URL
- URL dedupe key
- semantic fingerprint
- raw provider tickers/topics
- matched canonical symbols
- event type, severity, sentiment label, and sentiment score

High-severity article or SEC event risk vetoes a candidate. Medium risk is
attached to the signal as traceable context but does not block it.

Every veto includes raw article or SEC filing references in `news_references`.

## CLI

```bash
quant data update-news --symbols NVDA,AMD --start 2026-07-01T00:00:00Z --end 2026-07-02T00:00:00Z
quant data news-risk --symbols NVDA,AMD --cutoff 2026-07-02T00:30:00Z
quant strategy scan --symbols NVDA,AMD --date 2026-07-01 --news-risk
```

To enable Alpha Vantage news, set:

```bash
QUANT_ALPHA_VANTAGE_API_KEY=...
```

To enable SEC events, replace the example contact:

```bash
QUANT_SEC_USER_AGENT="qlib_quant/0.1 your-real-email@example.com"
```

## FinBERT

The default scorer is deterministic and local. FinBERT is configured with a
pinned model name and revision, but is lazy-loaded only when
`sentiment.scorer: finbert` is selected and optional model dependencies are
installed.

Before FinBERT is enabled in a time-sensitive daily workflow, add an
article-level inference cache keyed by `article_id` or `dedupe_key` plus model
revision. Premarket and open-gate runs must have a timeout circuit breaker; on
timeout the report should mark `sentiment_status: DEGRADED` and use the
deterministic event/keyword scorer instead of blocking hard-rule scans.
