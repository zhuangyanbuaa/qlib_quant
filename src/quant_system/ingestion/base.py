"""Provider-neutral contracts for daily price ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
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
