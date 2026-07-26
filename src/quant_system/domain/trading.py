"""Time-safe strategy signal, order, and fill contracts."""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from quant_system.domain.enums import MarketRegime, Side
from quant_system.domain.models import PositiveFloat, StrictModel, Symbol


class CandidateSignal(StrictModel):
    """A close-confirmed candidate that cannot trade before the next session."""

    signal_id: UUID = Field(default_factory=uuid4)
    symbol: Symbol
    signal_session: date
    data_cutoff_utc: AwareDatetime
    signal_time_utc: AwareDatetime
    earliest_order_session: date
    earliest_order_time_utc: AwareDatetime
    score: float
    signal_close: PositiveFloat
    atr20: PositiveFloat
    market_regime: MarketRegime
    reasons: tuple[str, ...]
    news_risk: str = "LOW"
    news_references: tuple[dict[str, object], ...] = ()

    @model_validator(mode="after")
    def validate_timeline(self) -> CandidateSignal:
        if self.data_cutoff_utc > self.signal_time_utc:
            raise ValueError("data cutoff must not follow signal creation")
        if self.signal_time_utc >= self.earliest_order_time_utc:
            raise ValueError("earliest order must be strictly after signal creation")
        if self.earliest_order_session <= self.signal_session:
            raise ValueError("earliest order session must follow signal session")
        return self


class OrderIntent(StrictModel):
    """An order derived from one immutable candidate signal."""

    order_id: UUID = Field(default_factory=uuid4)
    signal_id: UUID
    symbol: Symbol
    side: Side
    quantity: int = Field(gt=0)
    created_at_utc: AwareDatetime
    earliest_fill_time_utc: AwareDatetime
    reference_price: PositiveFloat
    stop_price: PositiveFloat
    target_price: PositiveFloat

    @model_validator(mode="after")
    def validate_order_timeline(self) -> OrderIntent:
        if self.created_at_utc > self.earliest_fill_time_utc:
            raise ValueError("order cannot be created after its earliest fill")
        if not self.stop_price < self.reference_price < self.target_price:
            raise ValueError("order prices must satisfy stop < reference < target")
        return self


class Fill(StrictModel):
    """A simulated or manually recorded execution."""

    fill_id: UUID = Field(default_factory=uuid4)
    order_id: UUID
    signal_id: UUID
    symbol: Symbol
    side: Side
    quantity: int = Field(gt=0)
    fill_time_utc: AwareDatetime
    earliest_fill_time_utc: AwareDatetime
    price: PositiveFloat
    commission: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_fill_timeline(self) -> Fill:
        if self.fill_time_utc < self.earliest_fill_time_utc:
            raise ValueError("fill cannot precede earliest fill time")
        return self
