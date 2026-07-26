"""Provider-neutral contracts for daily price ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol


class ProviderError(RuntimeError):
    """Base error for an external market-data provider."""


class TransientProviderError(ProviderError):
    """A provider failure that may succeed after backoff."""


@dataclass(frozen=True)
class ProviderBar:
    """Provider-normalized daily bar before local lineage is attached."""

    symbol: str
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    adjusted: bool
    split_factor: float = 1.0
    dividend: float = 0.0
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderBatchResult:
    """Partial-success result from one external batch request."""

    bars: tuple[ProviderBar, ...] = ()
    empty_symbols: tuple[str, ...] = ()
    errors: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderNewsArticle:
    """Provider-normalized article before local lineage and classification."""

    article_id: str
    published_at_utc: datetime
    title: str
    summary: str
    url: str
    source_domain: str
    language: str
    raw_tickers: tuple[str, ...] = ()
    raw_topics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderNewsResult:
    """Partial-success result from one news request."""

    articles: tuple[ProviderNewsArticle, ...] = ()
    warnings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderCompanyEvent:
    """Provider-normalized SEC/company event before local lineage."""

    cik: str
    symbol: str
    form_type: str
    accession_number: str
    filed_at_utc: datetime
    accepted_at_utc: datetime
    filing_url: str


@dataclass(frozen=True)
class ProviderCompanyEventResult:
    """Partial-success result from one SEC/company-event request."""

    events: tuple[ProviderCompanyEvent, ...] = ()
    warnings: dict[str, str] = field(default_factory=dict)


class DailyPriceProvider(Protocol):
    """Interface implemented by primary and fallback daily-price sources."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def fetch_daily(
        self,
        symbols: tuple[str, ...],
        *,
        start: date,
        end_exclusive: date,
    ) -> ProviderBatchResult: ...


class NewsProvider(Protocol):
    """Interface implemented by external news providers."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def fetch_news(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> ProviderNewsResult: ...


class CompanyEventProvider(Protocol):
    """Interface implemented by SEC/company-event sources."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def fetch_events(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> ProviderCompanyEventResult: ...
