"""Alpha Vantage NEWS_SENTIMENT adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

from quant_system.ingestion.base import (
    ProviderError,
    ProviderNewsArticle,
    ProviderNewsResult,
    TransientProviderError,
)
from quant_system.sentiment.dedupe import stable_hash

Transport = Callable[[str, float], dict[str, object]]


class AlphaVantageNewsAdapter:
    """Fetch and normalize Alpha Vantage market news."""

    name = "alpha_vantage_news"
    version = "NEWS_SENTIMENT-v1"
    endpoint = "https://www.alphavantage.co/query"

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float,
        limit_per_call: int,
        topics: tuple[str, ...] = (),
        transport: Transport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Alpha Vantage API key is required")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.limit_per_call = limit_per_call
        self.topics = topics
        self._transport = transport or _json_get

    def fetch_news(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> ProviderNewsResult:
        """Fetch news visible in a UTC interval."""
        params = {
            "function": "NEWS_SENTIMENT",
            "apikey": self.api_key,
            "sort": "EARLIEST",
            "limit": str(self.limit_per_call),
            "time_from": _format_av_time(start_utc),
            "time_to": _format_av_time(end_utc),
        }
        if symbols:
            params["tickers"] = ",".join(symbol.upper() for symbol in symbols)
        if self.topics:
            params["topics"] = ",".join(self.topics)

        url = f"{self.endpoint}?{urlencode(params)}"
        payload = self._transport(url, self.timeout_seconds)
        _raise_for_provider_message(payload)
        feed = payload.get("feed", [])
        if not isinstance(feed, list):
            raise ProviderError("Alpha Vantage response missing feed list")

        articles: list[ProviderNewsArticle] = []
        warnings: dict[str, str] = {}
        for index, item in enumerate(feed):
            if not isinstance(item, dict):
                warnings[str(index)] = "non_mapping_feed_item"
                continue
            try:
                article = _parse_article(item)
            except (KeyError, TypeError, ValueError) as error:
                warnings[str(index)] = f"{type(error).__name__}: {error}"
                continue
            if start_utc <= article.published_at_utc <= end_utc:
                articles.append(article)
        return ProviderNewsResult(articles=tuple(articles), warnings=warnings)


def _parse_article(item: dict[str, object]) -> ProviderNewsArticle:
    title = str(item["title"]).strip()
    url = str(item["url"]).strip()
    summary = str(item.get("summary", "")).strip()
    published_at = _parse_av_time(str(item["time_published"]))
    source_domain = urlsplit(url).netloc.lower()
    ticker_sentiment = item.get("ticker_sentiment", [])
    raw_tickers = []
    if isinstance(ticker_sentiment, list):
        for entry in ticker_sentiment:
            if isinstance(entry, dict) and entry.get("ticker"):
                raw_tickers.append(str(entry["ticker"]).upper())
    topics = item.get("topics", [])
    raw_topics = []
    if isinstance(topics, list):
        for entry in topics:
            if isinstance(entry, dict) and entry.get("topic"):
                raw_topics.append(str(entry["topic"]))
    article_id = stable_hash(f"{url}|{published_at.isoformat()}|{title}", prefix="av")
    return ProviderNewsArticle(
        article_id=article_id,
        published_at_utc=published_at,
        title=title,
        summary=summary,
        url=url,
        source_domain=source_domain,
        language="en",
        raw_tickers=tuple(raw_tickers),
        raw_topics=tuple(raw_topics),
    )


def _format_av_time(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("Alpha Vantage time bounds must be timezone-aware")
    utc_value = value.astimezone(UTC)
    return utc_value.strftime("%Y%m%dT%H%M")


def _parse_av_time(value: str) -> datetime:
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"invalid Alpha Vantage time: {value}")


def _raise_for_provider_message(payload: dict[str, object]) -> None:
    if "Error Message" in payload:
        raise ProviderError(str(payload["Error Message"]))
    if "Note" in payload or "Information" in payload:
        message = str(payload.get("Note") or payload.get("Information"))
        raise TransientProviderError(message)


def _json_get(url: str, timeout_seconds: float) -> dict[str, object]:
    try:
        with urlopen(url, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as error:
        if 500 <= error.code < 600 or error.code == 429:
            raise TransientProviderError(f"HTTP {error.code}") from error
        raise ProviderError(f"HTTP {error.code}") from error
    except URLError as error:
        raise TransientProviderError(str(error)) from error
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ProviderError("JSON response must be an object")
    return decoded
