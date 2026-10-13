"""A query attempt is a visible fact even when it returns no articles."""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from quant_system.domain.models import IngestionMetadata, NonEmptyString, Symbol


class NewsCoverageObservation(IngestionMetadata):
    coverage_id: NonEmptyString
    symbol: Symbol
    channel: Literal["news", "sec"]
    window_start_utc: AwareDatetime
    window_end_utc: AwareDatetime
    result: Literal["SUCCESS", "EMPTY", "FAILED", "UNAVAILABLE", "PARTIAL"]
    rows_returned: int = Field(ge=0)
    reason: str = ""

    @field_validator("window_start_utc", "window_end_utc")
    @classmethod
    def require_window_utc(cls, value: datetime):
        return IngestionMetadata.require_utc(value)

    @model_validator(mode="after")
    def validate_window(self):
        if self.window_start_utc > self.window_end_utc:
            raise ValueError("coverage start must not follow end")
        if self.window_end_utc > self.available_at_utc:
            raise ValueError("a future interval cannot certify news coverage")
        return self
