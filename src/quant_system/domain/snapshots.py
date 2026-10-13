"""Provenance-bearing broker snapshots for forward-only execution gates."""

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, field_validator, model_validator

from quant_system.domain.models import IngestionMetadata, PositiveFloat, Symbol


class IntradaySnapshot(IngestionMetadata):
    symbol: Symbol
    last_price: PositiveFloat
    quote_at_utc: AwareDatetime
    session_date_ny: date
    news_risk: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"] = "UNKNOWN"
    liquidity_status: Literal["OK", "NORMAL", "HALTED", "UNKNOWN"] = "UNKNOWN"
    note: str = ""

    @field_validator("quote_at_utc")
    @classmethod
    def require_quote_utc(cls, value: datetime):
        return IngestionMetadata.require_utc(value)

    @model_validator(mode="after")
    def validate_quote_timeline(self):
        if self.quote_at_utc > self.available_at_utc:
            raise ValueError("quote time must not follow availability")
        if (
            self.quote_at_utc.astimezone(ZoneInfo("America/New_York")).date()
            != self.session_date_ny
        ):
            raise ValueError("quote date must match the New York session date")
        return self
