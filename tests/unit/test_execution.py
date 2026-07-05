from datetime import UTC, date, datetime

import pandas as pd

from quant_system.backtest.execution import DailyExecutionModel
from quant_system.domain.enums import ExitReason, MarketRegime
from quant_system.domain.trading import CandidateSignal
from quant_system.strategy.config import ExecutionConfig


def execution() -> DailyExecutionModel:
    return DailyExecutionModel(
        ExecutionConfig(
            holding_sessions=5,
            stop_atr_multiple=2,
            target_atr_multiple=3,
            maximum_gap_up=0.03,
            maximum_gap_down=0.05,
            slippage_bps=10,
            commission_bps=2,
        )
    )


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


def test_large_gap_rejects_entry() -> None:
    bar = pd.Series({"open": 104, "high": 105, "low": 103, "close": 104})

    assert execution().entry(signal(), bar) is None


def test_same_bar_stop_and_target_uses_stop_first() -> None:
    bar = pd.Series({"open": 100, "high": 120, "low": 80, "close": 110})

    decision = execution().exit(
        stop_price=90,
        target_price=115,
        bars_held=2,
        bar=bar,
    )

    assert decision.reason is ExitReason.STOP_LOSS
    assert decision.fill_price < 90


def test_gap_through_stop_fills_at_worse_open_with_slippage() -> None:
    bar = pd.Series({"open": 85, "high": 88, "low": 80, "close": 82})

    decision = execution().exit(
        stop_price=90,
        target_price=115,
        bars_held=2,
        bar=bar,
    )

    assert decision.reason is ExitReason.STOP_LOSS
    assert decision.fill_price == 85 * 0.999
