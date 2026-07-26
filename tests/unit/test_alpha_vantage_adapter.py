from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from quant_system.ingestion.alpha_vantage import AlphaVantageNewsAdapter
from quant_system.ingestion.base import TransientProviderError


def test_alpha_vantage_news_adapter_parses_feed_and_time_bounds() -> None:
    captured = {}

    def transport(url: str, timeout_seconds: float) -> dict[str, object]:
        captured["query"] = parse_qs(urlsplit(url).query)
        captured["timeout"] = timeout_seconds
        return {
            "feed": [
                {
                    "title": "Nvidia faces DOJ investigation",
                    "url": "https://example.com/news?utm_source=x",
                    "time_published": "20260701T120500",
                    "summary": "Probe expands.",
                    "ticker_sentiment": [{"ticker": "NVDA"}],
                    "topics": [{"topic": "technology"}],
                }
            ]
        }

    adapter = AlphaVantageNewsAdapter(
        api_key="demo",
        timeout_seconds=7,
        limit_per_call=50,
        topics=("technology",),
        transport=transport,
    )

    result = adapter.fetch_news(
        ("NVDA",),
        start_utc=datetime(2026, 7, 1, 12, tzinfo=UTC),
        end_utc=datetime(2026, 7, 1, 13, tzinfo=UTC),
    )

    assert captured["query"]["function"] == ["NEWS_SENTIMENT"]
    assert captured["query"]["tickers"] == ["NVDA"]
    assert captured["query"]["time_from"] == ["20260701T1200"]
    assert result.articles[0].raw_tickers == ("NVDA",)
    assert result.articles[0].published_at_utc == datetime(2026, 7, 1, 12, 5, tzinfo=UTC)


def test_alpha_vantage_note_is_transient() -> None:
    adapter = AlphaVantageNewsAdapter(
        api_key="demo",
        timeout_seconds=7,
        limit_per_call=50,
        transport=lambda _url, _timeout: {"Note": "rate limit"},
    )

    with pytest.raises(TransientProviderError):
        adapter.fetch_news(
            ("NVDA",),
            start_utc=datetime(2026, 7, 1, tzinfo=UTC),
            end_utc=datetime(2026, 7, 2, tzinfo=UTC),
        )
