"""Pessimistic daily-bar fill rules."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from quant_system.domain.enums import ExitReason
from quant_system.domain.trading import CandidateSignal
from quant_system.strategy.config import ExecutionConfig


@dataclass(frozen=True)
class EntryDecision:
    fill_price: float
    stop_price: float
    target_price: float
    commission_rate: float


@dataclass(frozen=True)
class ExitDecision:
    fill_price: float
    reason: ExitReason
    commission_rate: float


class DailyExecutionModel:
    """Translate daily OHLC into conservative, deterministic fills."""

    def __init__(self, config: ExecutionConfig) -> None:
        self.config = config

    @property
    def slippage_rate(self) -> float:
        return self.config.slippage_bps / 10_000

    @property
    def commission_rate(self) -> float:
        return self.config.commission_bps / 10_000

    def entry(self, signal: CandidateSignal, bar: pd.Series) -> EntryDecision | None:
        gap_return = float(bar["open"]) / signal.signal_close - 1
        if (
            gap_return > self.config.maximum_gap_up
            or gap_return < -self.config.maximum_gap_down
        ):
            return None
        fill_price = float(bar["open"]) * (1 + self.slippage_rate)
        stop_price = fill_price - self.config.stop_atr_multiple * signal.atr20
        target_price = fill_price + self.config.target_atr_multiple * signal.atr20
        if stop_price <= 0:
            return None
        return EntryDecision(
            fill_price=fill_price,
            stop_price=stop_price,
            target_price=target_price,
            commission_rate=self.commission_rate,
        )

    def exit(
        self,
        *,
        stop_price: float,
        target_price: float,
        bars_held: int,
        bar: pd.Series,
    ) -> ExitDecision | None:
        """Apply stop before target when daily OHLC cannot resolve ordering."""
        open_price = float(bar["open"])
        if float(bar["low"]) <= stop_price:
            raw_price = min(open_price, stop_price)
            return self._sell(raw_price, ExitReason.STOP_LOSS)
        if float(bar["high"]) >= target_price:
            return self._sell(target_price, ExitReason.PROFIT_TARGET)
        if bars_held >= self.config.holding_sessions:
            return self._sell(float(bar["close"]), ExitReason.TIME_EXIT)
        return None

    def _sell(self, raw_price: float, reason: ExitReason) -> ExitDecision:
        return ExitDecision(
            fill_price=raw_price * (1 - self.slippage_rate),
            reason=reason,
            commission_rate=self.commission_rate,
        )
