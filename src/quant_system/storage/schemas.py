"""Canonical Arrow schemas for persisted datasets."""

from __future__ import annotations

import pyarrow as pa

SCHEMA_VERSION = "1"

INGESTION_FIELDS = [
    pa.field("source", pa.string(), nullable=False),
    pa.field("source_version", pa.string(), nullable=False),
    pa.field("fetched_at_utc", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("available_at_utc", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("ingestion_run_id", pa.string(), nullable=False),
    pa.field("is_stale", pa.bool_(), nullable=False),
    pa.field("quality_flags", pa.list_(pa.string()), nullable=False),
]

DAILY_PRICE_SCHEMA = pa.schema(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("timestamp_utc", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("session_date_ny", pa.date32(), nullable=False),
        pa.field("open", pa.float64(), nullable=False),
        pa.field("high", pa.float64(), nullable=False),
        pa.field("low", pa.float64(), nullable=False),
        pa.field("close", pa.float64(), nullable=False),
        pa.field("volume", pa.int64(), nullable=False),
        pa.field("adjusted", pa.bool_(), nullable=False),
        pa.field("split_factor", pa.float64(), nullable=False),
        pa.field("dividend", pa.float64(), nullable=False),
        *INGESTION_FIELDS,
    ],
    metadata={b"schema_name": b"daily_prices", b"schema_version": SCHEMA_VERSION.encode()},
)

MACRO_OBSERVATION_SCHEMA = pa.schema(
    [
        pa.field("series_id", pa.string(), nullable=False),
        pa.field("observation_date", pa.date32(), nullable=False),
        pa.field("value", pa.float64(), nullable=False),
        pa.field("units", pa.string(), nullable=False),
        pa.field("released_at_utc", pa.timestamp("us", tz="UTC"), nullable=False),
        *INGESTION_FIELDS,
    ],
    metadata={
        b"schema_name": b"macro_observations",
        b"schema_version": SCHEMA_VERSION.encode(),
    },
)

NEWS_ARTICLE_SCHEMA = pa.schema(
    [
        pa.field("article_id", pa.string(), nullable=False),
        pa.field("published_at_utc", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("title", pa.string(), nullable=False),
        pa.field("summary", pa.string(), nullable=False),
        pa.field("url", pa.string(), nullable=False),
        pa.field("source_domain", pa.string(), nullable=False),
        pa.field("canonical_url", pa.string(), nullable=False),
        pa.field("dedupe_key", pa.string(), nullable=False),
        pa.field("semantic_key", pa.string(), nullable=False),
        pa.field("language", pa.string(), nullable=False),
        pa.field("raw_tickers", pa.list_(pa.string()), nullable=False),
        pa.field("raw_topics", pa.list_(pa.string()), nullable=False),
        pa.field("matched_symbols", pa.list_(pa.string()), nullable=False),
        pa.field("event_type", pa.string(), nullable=False),
        pa.field("severity", pa.string(), nullable=False),
        pa.field("sentiment_label", pa.string(), nullable=False),
        pa.field("sentiment_score", pa.float64(), nullable=False),
        *INGESTION_FIELDS,
    ],
    metadata={b"schema_name": b"news_articles", b"schema_version": SCHEMA_VERSION.encode()},
)

COMPANY_EVENT_SCHEMA = pa.schema(
    [
        pa.field("event_id", pa.string(), nullable=False),
        pa.field("cik", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("form_type", pa.string(), nullable=False),
        pa.field("accession_number", pa.string(), nullable=False),
        pa.field("filed_at_utc", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("accepted_at_utc", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("filing_url", pa.string(), nullable=False),
        pa.field("event_type", pa.string(), nullable=False),
        pa.field("severity", pa.string(), nullable=False),
        *INGESTION_FIELDS,
    ],
    metadata={b"schema_name": b"company_events", b"schema_version": SCHEMA_VERSION.encode()},
)
