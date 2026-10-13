"""Reviewed security lifecycle facts, independent of provider price series."""

from datetime import date
from typing import Literal

from pydantic import HttpUrl, field_validator, model_validator

from quant_system.domain.models import IngestionMetadata, NonEmptyString, Symbol


class SecurityLifecycleEvent(IngestionMetadata):
    event_id: NonEmptyString
    security_id: NonEmptyString
    symbol: Symbol
    event_type: Literal[
        "SYMBOL_CHANGE", "SHARE_CONVERSION", "MERGER", "DELISTED", "SUSPENDED", "RESUMED"
    ]
    effective_session: date
    successor_symbol: str = ""
    successor_security_id: str = ""
    source_url: str

    @field_validator("successor_symbol")
    @classmethod
    def normalize_successor(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("source_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return str(HttpUrl(value))

    @model_validator(mode="after")
    def validate_relationship(self):
        if self.event_type in {"SYMBOL_CHANGE", "SHARE_CONVERSION"} and (
            not self.successor_symbol or self.successor_symbol == self.symbol
        ):
            raise ValueError("symbol changes/conversions require a distinct successor")
        if self.event_type == "SHARE_CONVERSION" and (
            not self.successor_security_id or self.successor_security_id == self.security_id
        ):
            raise ValueError("share conversion requires a separate successor security identity")
        return self
