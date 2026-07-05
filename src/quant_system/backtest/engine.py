"""Daily portfolio backtest using the same signals as the scanner."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from uuid import NAMESPACE_URL, uuid5

import pandas as pd

from quant_system.backtest.execution import DailyExecutionModel
from quant_system.backtest.portfolio import Portfolio, TradeRecord
from quant_system.domain.clocks import NyseSessionClock
from quant_system.domain.enums import MarketRegime, Side
from quant_system.domain.trading import CandidateSignal, Fill, OrderIntent
from quant_system.strategy.buy_the_dip import BuyTheDipStrategy
from quant_system.strategy.config import BuyTheDipConfig


@dataclass(frozen=True)
class Rejection:
    signal_id: object
    symbol: str
    session: date
    reason: str


@dataclass(frozen=True)
class BacktestResult:
    signals: tuple[CandidateSignal, ...]
    trades: tuple[TradeRecord, ...]
    equity_curve: pd.DataFrame
    rejections: pd.DataFrame
    open_positions: tuple[str, ...]


class BacktestEngine:
    """Event loop with exits before entries and no synthetic terminal exits."""

    def __init__(
        self,
        strategy: BuyTheDipStrategy,
        config: BuyTheDipConfig,
        *,
        clock: NyseSessionClock | None = None,
    ) -> None:
        self.strategy = strategy
        self.config = config
        self.clock = clock or NyseSessionClock()
        self.execution = DailyExecutionModel(config.execution)

    def run(
        self,
        features: pd.DataFrame,
        *,
        start: date,
        end: date,
    ) -> BacktestResult:
        frame = self.strategy.annotate(features)
        frame["session_date_ny"] = pd.to_datetime(frame["session_date_ny"])
        signals = tuple(
            signal
            for signal in self.strategy.generate_signals(frame)
            if start <= signal.signal_session <= end
        )
        entries: dict[date, list[CandidateSignal]] = defaultdict(list)
        for signal in signals:
            entries[signal.earliest_order_session].append(signal)
        for session_signals in entries.values():
            session_signals.sort(key=lambda signal: (-signal.score, signal.symbol))

        evaluation = frame.loc[
            frame["session_date_ny"].dt.date.between(start, end, inclusive="both")
        ].copy()
        bars_by_session = {
            session.date(): group.set_index("symbol")
            for session, group in evaluation.groupby("session_date_ny", sort=True)
        }
        benchmark = self.config.strategy.benchmark_symbol.upper()
        portfolio = Portfolio(self.config.portfolio)
        equity_rows: list[dict[str, object]] = []
        rejections: list[Rejection] = []

        for session in sorted(bars_by_session):
            bars = bars_by_session[session]
            closing_prices = {
                symbol: float(row["close"]) for symbol, row in bars.iterrows()
            }
            portfolio.mark(closing_prices)
            positions_at_open = set(portfolio.positions)
            for signal in entries.get(session, []):
                rejection = self._enter_signal(portfolio, signal, bars, session)
                if rejection:
                    rejections.append(rejection)
            self._process_exits(
                portfolio,
                bars,
                session,
                positions_at_open=positions_at_open,
            )
            portfolio.mark(closing_prices)
            regime = self._regime_for_session(bars, benchmark)
            equity_rows.append(portfolio.snapshot(session, regime))

        return BacktestResult(
            signals=signals,
            trades=tuple(portfolio.trades),
            equity_curve=pd.DataFrame(equity_rows),
            rejections=pd.DataFrame(
                [
                    {
                        "signal_id": str(rejection.signal_id),
                        "symbol": rejection.symbol,
                        "session": rejection.session,
                        "reason": rejection.reason,
                    }
                    for rejection in rejections
                ],
                columns=["signal_id", "symbol", "session", "reason"],
            ),
            open_positions=tuple(sorted(portfolio.positions)),
        )

    def _process_exits(
        self,
        portfolio: Portfolio,
        bars: pd.DataFrame,
        session: date,
        *,
        positions_at_open: set[str],
    ) -> None:
        for symbol in tuple(portfolio.positions):
            if symbol not in bars.index:
                continue
            position = portfolio.positions[symbol]
            if symbol in positions_at_open:
                position.bars_held += 1
            decision = self.execution.exit(
                stop_price=position.stop_price,
                target_price=position.target_price,
                bars_held=position.bars_held,
                bar=bars.loc[symbol],
            )
            if decision:
                fill_time = self.clock.session_close_utc(session)
                fill = Fill(
                    fill_id=uuid5(
                        NAMESPACE_URL,
                        f"{position.order.order_id}:exit:{session}:{decision.reason.value}",
                    ),
                    order_id=position.order.order_id,
                    signal_id=position.signal.signal_id,
                    symbol=symbol,
                    side=Side.SELL,
                    quantity=position.quantity,
                    fill_time_utc=fill_time,
                    earliest_fill_time_utc=position.entry_fill.fill_time_utc,
                    price=decision.fill_price,
                    commission=(
                        position.quantity
                        * decision.fill_price
                        * decision.commission_rate
                    ),
                )
                portfolio.exit(
                    symbol=symbol,
                    fill=fill,
                    exit_session=session,
                    reason=decision.reason,
                )

    def _enter_signal(
        self,
        portfolio: Portfolio,
        signal: CandidateSignal,
        bars: pd.DataFrame,
        session: date,
    ) -> Rejection | None:
        if signal.symbol in portfolio.positions:
            return Rejection(signal.signal_id, signal.symbol, session, "already_held")
        if signal.symbol not in bars.index:
            return Rejection(signal.signal_id, signal.symbol, session, "missing_entry_bar")
        bar = bars.loc[signal.symbol]
        decision = self.execution.entry(signal, bar)
        if decision is None:
            return Rejection(signal.signal_id, signal.symbol, session, "entry_gap_or_stop")
        quantity = portfolio.size_position(
            signal,
            entry_price=decision.fill_price,
            stop_price=decision.stop_price,
            commission_rate=decision.commission_rate,
        )
        if quantity < 1:
            return Rejection(signal.signal_id, signal.symbol, session, "portfolio_constraint")
        order = OrderIntent(
            order_id=uuid5(NAMESPACE_URL, f"{signal.signal_id}:order"),
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=Side.BUY,
            quantity=quantity,
            created_at_utc=signal.signal_time_utc,
            earliest_fill_time_utc=signal.earliest_order_time_utc,
            reference_price=signal.signal_close,
            stop_price=max(
                signal.signal_close * 0.01,
                signal.signal_close
                - self.config.execution.stop_atr_multiple * signal.atr20,
            ),
            target_price=(
                signal.signal_close
                + self.config.execution.target_atr_multiple * signal.atr20
            ),
        )
        fill = Fill(
            fill_id=uuid5(NAMESPACE_URL, f"{order.order_id}:entry"),
            order_id=order.order_id,
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=Side.BUY,
            quantity=quantity,
            fill_time_utc=self.clock.session_open_utc(session),
            earliest_fill_time_utc=signal.earliest_order_time_utc,
            price=decision.fill_price,
            commission=quantity * decision.fill_price * decision.commission_rate,
        )
        entered = portfolio.enter(
            signal=signal,
            order=order,
            fill=fill,
            stop_price=decision.stop_price,
            target_price=decision.target_price,
        )
        if not entered:
            return Rejection(signal.signal_id, signal.symbol, session, "insufficient_cash")
        return None

    @staticmethod
    def _regime_for_session(bars: pd.DataFrame, benchmark: str) -> str:
        if benchmark not in bars.index or "market_regime" not in bars.columns:
            return MarketRegime.RED.value
        return str(bars.loc[benchmark, "market_regime"])
