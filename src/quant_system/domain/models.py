"""Point-in-time domain models for external market data."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

Symbol = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=1, max_length=32),
]
NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class StrictModel(BaseModel):
    """Base model that rejects undeclared fields and accidental mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class IngestionMetadata(StrictModel):
    """Lineage and point-in-time metadata required on every raw record."""

    source: NonEmptyString
    source_version: NonEmptyString
    fetched_at_utc: AwareDatetime
    available_at_utc: AwareDatetime
    ingestion_run_id: UUID
    is_stale: bool = False
    quality_flags: tuple[str, ...] = ()

    @field_validator("fetched_at_utc", "available_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
            raise ValueError("timestamp must use UTC")
        return value

    @model_validator(mode="after")
    def fetched_after_available(self) -> IngestionMetadata:
        if self.fetched_at_utc < self.available_at_utc:
            raise ValueError("fetched_at_utc must not precede available_at_utc")
        return self


class DailyPrice(IngestionMetadata):
    """Adjusted or unadjusted OHLCV values for one New York trading session."""

    symbol: Symbol
    timestamp_utc: AwareDatetime
    session_date_ny: date
    open: PositiveFloat
    high: PositiveFloat
    low: PositiveFloat
    close: PositiveFloat
    volume: Annotated[int, Field(ge=0)]
    adjusted: bool
    split_factor: PositiveFloat = 1.0
    dividend: NonNegativeFloat = 0.0

    @field_validator("timestamp_utc")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
            raise ValueError("timestamp_utc must use UTC")
        return value

    @model_validator(mode="after")
    def validate_bar(self) -> DailyPrice:
        if self.low > min(self.open, self.close):
            raise ValueError("low must not exceed open or close")
        if self.high < max(self.open, self.close):
            raise ValueError("high must not be below open or close")
        if self.low > self.high:
            raise ValueError("low must not exceed high")
        if self.available_at_utc < self.timestamp_utc:
            raise ValueError("price cannot be available before its bar timestamp")
        return self


class MacroObservation(IngestionMetadata):
    """A point-in-time macroeconomic observation."""

    series_id: NonEmptyString
    observation_date: date
    value: Annotated[float, Field(allow_inf_nan=False)]
    units: NonEmptyString
    released_at_utc: AwareDatetime

    @model_validator(mode="after")
    def released_before_available(self) -> MacroObservation:
        if self.available_at_utc < self.released_at_utc:
            raise ValueError("macro value cannot be available before release")
        return self


class NewsArticle(IngestionMetadata):
    """News metadata retained for deterministic sentiment reprocessing."""

    article_id: NonEmptyString
    published_at_utc: AwareDatetime
    title: NonEmptyString
    summary: str = ""
    url: HttpUrl
    language: NonEmptyString = "en"
    raw_tickers: tuple[str, ...] = ()
    raw_topics: tuple[str, ...] = ()

    @model_validator(mode="after")
    def published_before_available(self) -> NewsArticle:
        if self.available_at_utc < self.published_at_utc:
            raise ValueError("article cannot be available before publication")
        return self


class CompanyEvent(IngestionMetadata):
    """A timestamped SEC or company event."""

    cik: NonEmptyString
    symbol: Symbol
    form_type: NonEmptyString
    accession_number: NonEmptyString
    filed_at_utc: AwareDatetime
    accepted_at_utc: AwareDatetime
    filing_url: HttpUrl

    @model_validator(mode="after")
    def validate_event_times(self) -> CompanyEvent:
        if self.accepted_at_utc < self.filed_at_utc:
            raise ValueError("accepted_at_utc must not precede filed_at_utc")
        if self.available_at_utc < self.accepted_at_utc:
            raise ValueError("event cannot be available before SEC acceptance")
        return self
