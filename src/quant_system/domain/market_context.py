"""Traceable observations for market context; never a strategy permission."""

from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, HttpUrl, field_validator, model_validator

from quant_system.domain.models import IngestionMetadata


class MarketObservation(IngestionMetadata):
    series_id: Literal["CNN_FEAR_GREED", "VIX"]
    observed_at_utc: AwareDatetime
    observation_date: date
    value: float = Field(allow_inf_nan=False)
    units: Literal["index_points"] = "index_points"
    label: str = ""
    source_url: str
    input_reference: str = ""

    @field_validator("observed_at_utc")
    @classmethod
    def observation_is_utc(cls, value):
        if value.utcoffset().total_seconds() != 0:
            raise ValueError("observation timestamp must use UTC")
        return value

    @field_validator("value", mode="before")
    @classmethod
    def numeric_value(cls, value):
        if type(value) not in {float, int}:
            raise ValueError("indicator value must be a JSON number")
        return value

    @field_validator("source_url")
    @classmethod
    def valid_url(cls, value):
        return str(HttpUrl(value))

    @model_validator(mode="after")
    def valid_observation(self):
        if self.observed_at_utc > self.available_at_utc:
            raise ValueError("observation cannot be available before publication")
        if (
            self.observation_date
            != self.observed_at_utc.astimezone(ZoneInfo("America/New_York")).date()
        ):
            raise ValueError("observation date must match New York observation time")
        if self.series_id == "CNN_FEAR_GREED" and not 0 <= self.value <= 100:
            raise ValueError("CNN score must be within 0-100")
        if self.series_id == "VIX" and self.value <= 0:
            raise ValueError("VIX must be positive")
        return self
