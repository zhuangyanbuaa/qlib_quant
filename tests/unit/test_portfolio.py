from datetime import UTC, date, datetime

from quant_system.backtest.portfolio import Portfolio
from quant_system.domain.enums import MarketRegime
from quant_system.domain.trading import CandidateSignal
from quant_system.strategy.config import PortfolioConfig


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


def test_position_sizing_respects_cash_reserve_and_maximum_position() -> None:
    portfolio = Portfolio(
        PortfolioConfig(
            initial_cash=100_000,
            risk_per_trade=0.005,
            reserve_cash_fraction=0.40,
            maximum_position_fraction=0.15,
            maximum_gross_exposure=0.60,
            maximum_positions=5,
            yellow_risk_multiplier=0.5,
        )
    )

    quantity = portfolio.size_position(signal(), entry_price=100, stop_price=95)

    assert quantity == 100
    assert quantity * 100 <= 15_000
    assert portfolio.cash - quantity * 100 >= 40_000


def test_yellow_regime_halves_risk_budget() -> None:
    config = PortfolioConfig(
        initial_cash=100_000,
        risk_per_trade=0.005,
        reserve_cash_fraction=0.40,
        maximum_position_fraction=1,
        maximum_gross_exposure=1,
        maximum_positions=5,
        yellow_risk_multiplier=0.5,
    )
    green_quantity = Portfolio(config).size_position(
        signal(),
        entry_price=100,
        stop_price=95,
    )
    yellow_signal = signal().model_copy(update={"market_regime": MarketRegime.YELLOW})
    yellow_quantity = Portfolio(config).size_position(
        yellow_signal,
        entry_price=100,
        stop_price=95,
    )

    assert yellow_quantity == green_quantity // 2
