from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from quant_system.domain.models import CompanyEvent, DailyPrice, MacroObservation, NewsArticle


def price_values() -> dict[str, object]:
    close_time = datetime(2026, 6, 26, 20, tzinfo=UTC)
    return {
        "symbol": "aapl",
        "timestamp_utc": close_time,
        "session_date_ny": date(2026, 6, 26),
        "open": 275.0,
        "high": 285.95,
        "low": 274.21,
        "close": 283.78,
        "volume": 261_244_321,
        "adjusted": True,
        "split_factor": 1.0,
        "dividend": 0.0,
        "source": "legacy_yfinance",
        "source_version": "legacy-auto-adjust-v1",
        "fetched_at_utc": close_time + timedelta(days=1),
        "available_at_utc": close_time + timedelta(minutes=30),
        "ingestion_run_id": uuid4(),
        "is_stale": False,
        "quality_flags": ("legacy_fetched_at_inferred",),
    }


def test_daily_price_normalizes_symbol_and_preserves_lineage() -> None:
    price = DailyPrice.model_validate(price_values())

    assert price.symbol == "AAPL"
    assert price.quality_flags == ("legacy_fetched_at_inferred",)


def test_daily_price_rejects_impossible_ohlc() -> None:
    values = price_values()
    values["low"] = 280.0

    with pytest.raises(ValidationError, match="low must not exceed"):
        DailyPrice.model_validate(values)


def test_daily_price_rejects_naive_timestamp() -> None:
    values = price_values()
    values["timestamp_utc"] = datetime(2026, 6, 26, 20)

    with pytest.raises(ValidationError):
        DailyPrice.model_validate(values)


def metadata_values() -> dict[str, object]:
    available = datetime(2026, 6, 26, 20, 30, tzinfo=UTC)
    return {
        "source": "fixture",
        "source_version": "1",
        "fetched_at_utc": available + timedelta(minutes=1),
        "available_at_utc": available,
        "ingestion_run_id": uuid4(),
    }


def test_macro_rejects_availability_before_release() -> None:
    metadata = metadata_values()
    with pytest.raises(ValidationError, match="before release"):
        MacroObservation(
            **metadata,
            series_id="VIX",
            observation_date=date(2026, 6, 26),
            value=15.2,
            units="index",
            released_at_utc=datetime(2026, 6, 26, 21, tzinfo=UTC),
        )


def test_news_and_company_event_accept_point_in_time_metadata() -> None:
    metadata = metadata_values()
    article = NewsArticle(
        **metadata,
        article_id="article-1",
        published_at_utc=datetime(2026, 6, 26, 20, tzinfo=UTC),
        title="Example",
        url="https://example.com/article",
    )
    event = CompanyEvent(
        **metadata,
        cik="0000320193",
        symbol="aapl",
        form_type="8-K",
        accession_number="example",
        filed_at_utc=datetime(2026, 6, 26, 19, 59, tzinfo=UTC),
        accepted_at_utc=datetime(2026, 6, 26, 20, tzinfo=UTC),
        filing_url="https://www.sec.gov/example",
    )

    assert str(article.url) == "https://example.com/article"
    assert event.symbol == "AAPL"
