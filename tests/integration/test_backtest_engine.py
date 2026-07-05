from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from quant_system.backtest.engine import BacktestEngine
from quant_system.backtest.metrics import calculate_metrics, reconstruct_realized_equity
from quant_system.domain.clocks import NyseSessionClock
from quant_system.domain.enums import MarketRegime
from quant_system.domain.trading import CandidateSignal
from quant_system.strategy.config import load_buy_the_dip_config


class StaticStrategy:
    def __init__(self, signals) -> None:
        self.signals = signals

    def generate_signals(self, _features):
        return self.signals

    @staticmethod
    def annotate(features):
        return features.copy()


def signal() -> CandidateSignal:
    return CandidateSignal(
        symbol="AAPL",
        signal_session=date(2026, 6, 26),
        data_cutoff_utc=datetime(2026, 6, 26, 20, tzinfo=UTC),
        signal_time_utc=datetime(2026, 6, 26, 20, 30, tzinfo=UTC),
        earliest_order_session=date(2026, 6, 29),
        earliest_order_time_utc=datetime(2026, 6, 29, 13, 30, tzinfo=UTC),
        score=0.1,
        signal_close=100,
        atr20=5,
        market_regime=MarketRegime.GREEN,
        reasons=("test",),
    )


def feature_frame(sessions: list[str]) -> pd.DataFrame:
    rows = []
    for session in pd.to_datetime(sessions):
        rows.extend(
            [
                {
                    "symbol": "MSFT",
                    "session_date_ny": session,
                    "open": 200.0,
                    "high": 205.0,
                    "low": 199.0,
                    "close": 204.0,
                    "market_regime": MarketRegime.GREEN,
                },
                {
                    "symbol": "AAPL",
                    "session_date_ny": session,
                    "open": 100.0,
                    "high": 103.0,
                    "low": 99.0,
                    "close": 102.0,
                    "market_regime": MarketRegime.GREEN,
                },
                {
                    "symbol": "QQQ",
                    "session_date_ny": session,
                    "open": 500.0,
                    "high": 505.0,
                    "low": 499.0,
                    "close": 504.0,
                    "market_regime": MarketRegime.GREEN,
                },
            ]
        )
    return pd.DataFrame(rows)


def test_trade_ledger_reconstructs_final_realized_equity() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    engine = BacktestEngine(StaticStrategy([signal()]), config)
    sessions = ["2026-06-26", "2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02", "2026-07-06"]

    result = engine.run(
        feature_frame(sessions),
        start=date(2026, 6, 26),
        end=date(2026, 7, 6),
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_session == date(2026, 6, 29)
    assert trade.exit_session == date(2026, 7, 6)
    assert trade.bars_held == 5
    assert trade.entry_time_utc > signal().signal_time_utc
    assert result.open_positions == ()
    final_equity = result.equity_curve.iloc[-1]["equity"]
    assert abs(final_equity - (config.portfolio.initial_cash + trade.net_pnl)) < 1e-8
    assert reconstruct_realized_equity(config.portfolio.initial_cash, result.trades) == (
        config.portfolio.initial_cash + trade.net_pnl
    )
    benchmark = feature_frame(sessions).loc[
        lambda frame: frame["symbol"] == "QQQ",
        ["session_date_ny", "close"],
    ]
    metrics = calculate_metrics(
        result,
        benchmark_prices=benchmark,
        initial_cash=config.portfolio.initial_cash,
    )
    assert metrics["sample_size_status"] == "INSUFFICIENT"

    repeated = engine.run(
        feature_frame(sessions),
        start=date(2026, 6, 26),
        end=date(2026, 7, 6),
    )
    assert repeated.trades[0].to_dict() == trade.to_dict()


def test_unmatured_position_remains_open_without_synthetic_latest_price_exit() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    engine = BacktestEngine(StaticStrategy([signal()]), config)

    result = engine.run(
        feature_frame(["2026-06-26", "2026-06-29", "2026-06-30"]),
        start=date(2026, 6, 26),
        end=date(2026, 6, 30),
    )

    assert result.trades == ()
    assert result.open_positions == ("AAPL",)


def test_closing_proceeds_cannot_fund_an_order_at_same_session_open() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    constrained_portfolio = config.portfolio.model_copy(update={"maximum_positions": 1})
    config = config.model_copy(update={"portfolio": constrained_portfolio})
    clock = NyseSessionClock()
    second = CandidateSignal(
        symbol="MSFT",
        signal_session=date(2026, 7, 2),
        data_cutoff_utc=clock.session_close_utc(date(2026, 7, 2)),
        signal_time_utc=clock.available_at_utc(date(2026, 7, 2)),
        earliest_order_session=date(2026, 7, 6),
        earliest_order_time_utc=clock.session_open_utc(date(2026, 7, 6)),
        score=0.2,
        signal_close=200,
        atr20=5,
        market_regime=MarketRegime.GREEN,
        reasons=("test",),
    )
    engine = BacktestEngine(StaticStrategy([signal(), second]), config)
    sessions = [
        "2026-06-26",
        "2026-06-29",
        "2026-06-30",
        "2026-07-01",
        "2026-07-02",
        "2026-07-06",
    ]

    result = engine.run(
        feature_frame(sessions),
        start=date(2026, 6, 26),
        end=date(2026, 7, 6),
    )

    rejection = result.rejections.loc[result.rejections["symbol"] == "MSFT"].iloc[0]
    assert rejection["reason"] == "portfolio_constraint"
    assert result.open_positions == ()
