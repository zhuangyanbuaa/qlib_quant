"""Cash-aware portfolio state and reconstructable trade ledger."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from math import floor
from uuid import UUID

import pandas as pd

from quant_system.domain.enums import ExitReason, MarketRegime
from quant_system.domain.trading import CandidateSignal, Fill, OrderIntent
from quant_system.strategy.config import PortfolioConfig


@dataclass
class Position:
    signal: CandidateSignal
    order: OrderIntent
    entry_fill: Fill
    quantity: int
    entry_price: float
    stop_price: float
    target_price: float
    entry_commission: float
    last_price: float
    bars_held: int = 1


@dataclass(frozen=True)
class TradeRecord:
    signal_id: UUID
    order_id: UUID
    entry_fill_id: UUID
    exit_fill_id: UUID
    symbol: str
    signal_session: object
    entry_session: object
    exit_session: object
    entry_time_utc: datetime
    exit_time_utc: datetime
    quantity: int
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float
    entry_commission: float
    exit_commission: float
    gross_pnl: float
    net_pnl: float
    net_return: float
    bars_held: int
    exit_reason: ExitReason
    market_regime: MarketRegime
    signal_score: float

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        for key in ("signal_id", "order_id", "entry_fill_id", "exit_fill_id"):
            payload[key] = str(payload[key])
        payload["exit_reason"] = self.exit_reason.value
        payload["market_regime"] = self.market_regime.value
        return payload


class Portfolio:
    """Long-only portfolio with explicit reserve, exposure, and risk limits."""

    def __init__(self, config: PortfolioConfig) -> None:
        self.config = config
        self.cash = config.initial_cash
        self.positions: dict[str, Position] = {}
        self.trades: list[TradeRecord] = []

    def equity(self) -> float:
        return self.cash + sum(
            position.quantity * position.last_price for position in self.positions.values()
        )

    def gross_exposure(self) -> float:
        return sum(
            position.quantity * position.last_price for position in self.positions.values()
        )

    def size_position(
        self,
        signal: CandidateSignal,
        *,
        entry_price: float,
        stop_price: float,
        commission_rate: float = 0.0,
    ) -> int:
        if signal.symbol in self.positions:
            return 0
        if len(self.positions) >= self.config.maximum_positions:
            return 0
        equity = self.equity()
        risk_multiplier = (
            self.config.yellow_risk_multiplier
            if signal.market_regime is MarketRegime.YELLOW
            else 1.0
        )
        risk_budget = equity * self.config.risk_per_trade * risk_multiplier
        risk_per_share = entry_price - stop_price
        if risk_per_share <= 0:
            return 0
        risk_quantity = floor(risk_budget / risk_per_share)
        position_cap = equity * self.config.maximum_position_fraction
        position_quantity = floor(position_cap / entry_price)
        gross_capacity = max(
            0.0,
            equity * self.config.maximum_gross_exposure - self.gross_exposure(),
        )
        gross_quantity = floor(gross_capacity / entry_price)
        reserve = equity * self.config.reserve_cash_fraction
        deployable_cash = max(0.0, self.cash - reserve)
        cash_quantity = floor(
            deployable_cash / (entry_price * (1 + commission_rate))
        )
        return max(
            0,
            min(risk_quantity, position_quantity, gross_quantity, cash_quantity),
        )

    def enter(
        self,
        *,
        signal: CandidateSignal,
        order: OrderIntent,
        fill: Fill,
        stop_price: float,
        target_price: float,
    ) -> bool:
        notional = fill.quantity * fill.price
        total_cost = notional + fill.commission
        if total_cost > self.cash:
            return False
        self.cash -= total_cost
        self.positions[signal.symbol] = Position(
            signal=signal,
            order=order,
            entry_fill=fill,
            quantity=fill.quantity,
            entry_price=fill.price,
            stop_price=stop_price,
            target_price=target_price,
            entry_commission=fill.commission,
            last_price=fill.price,
        )
        return True

    def exit(
        self,
        *,
        symbol: str,
        fill: Fill,
        exit_session: object,
        reason: ExitReason,
    ) -> TradeRecord:
        position = self.positions.pop(symbol)
        proceeds = fill.quantity * fill.price
        self.cash += proceeds - fill.commission
        gross_pnl = (fill.price - position.entry_price) * fill.quantity
        net_pnl = gross_pnl - position.entry_commission - fill.commission
        invested = position.entry_price * fill.quantity + position.entry_commission
        trade = TradeRecord(
            signal_id=position.signal.signal_id,
            order_id=position.order.order_id,
            entry_fill_id=position.entry_fill.fill_id,
            exit_fill_id=fill.fill_id,
            symbol=symbol,
            signal_session=position.signal.signal_session,
            entry_session=position.signal.earliest_order_session,
            exit_session=exit_session,
            entry_time_utc=position.entry_fill.fill_time_utc,
            exit_time_utc=fill.fill_time_utc,
            quantity=fill.quantity,
            entry_price=position.entry_price,
            exit_price=fill.price,
            stop_price=position.stop_price,
            target_price=position.target_price,
            entry_commission=position.entry_commission,
            exit_commission=fill.commission,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            net_return=net_pnl / invested,
            bars_held=position.bars_held,
            exit_reason=reason,
            market_regime=position.signal.market_regime,
            signal_score=position.signal.score,
        )
        self.trades.append(trade)
        return trade

    def mark(self, closing_prices: dict[str, float]) -> None:
        for symbol, position in self.positions.items():
            if symbol in closing_prices:
                position.last_price = closing_prices[symbol]

    def snapshot(self, session: object, regime: str) -> dict[str, object]:
        market_value = self.gross_exposure()
        equity = self.cash + market_value
        return {
            "session_date_ny": session,
            "cash": self.cash,
            "market_value": market_value,
            "equity": equity,
            "gross_exposure_fraction": market_value / equity if equity else 0.0,
            "open_positions": len(self.positions),
            "market_regime": regime,
        }

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame(trade.to_dict() for trade in self.trades)
