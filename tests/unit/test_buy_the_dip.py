from datetime import date
from pathlib import Path

import pandas as pd

from quant_system.domain.enums import MarketRegime
from quant_system.strategy.buy_the_dip import BuyTheDipStrategy
from quant_system.strategy.config import load_buy_the_dip_config


def feature_rows() -> pd.DataFrame:
    dates = pd.to_datetime(["2026-06-25", "2026-06-26"])
    common = {
        "open": [100.0, 100.0],
        "high": [101.0, 104.0],
        "low": [94.0, 99.0],
        "volume": [1_000_000, 1_000_000],
        "ma20": [105.0, 103.0],
        "ma50": [100.0, 100.0],
        "ma200": [90.0, 90.0],
        "ma50_slope20": [0.01, 0.01],
        "rolling_high20": [105.0, 105.0],
        "drawdown_from_high20": [0.10, 0.02],
        "avg_dollar_volume20": [100_000_000.0, 100_000_000.0],
        "rsi14": [40.0, 50.0],
        "atr20": [4.0, 4.0],
        "adx14": [25.0, 25.0],
        "return60": [0.15, 0.16],
        "benchmark_return60": [0.05, 0.05],
        "relative_return60": [0.10, 0.11],
        "previous_close": [96.0, 94.5],
        "previous_high": [100.0, 101.0],
        "previous_ma5": [98.0, 96.0],
        "feature_ready": [True, True],
    }
    stock = pd.DataFrame(
        {
            "symbol": "AAPL",
            "session_date_ny": dates,
            "close": [94.5, 103.0],
            "ma5": [96.0, 98.0],
            **common,
        }
    )
    benchmark = stock.copy()
    benchmark["symbol"] = "QQQ"
    benchmark["close"] = [110.0, 112.0]
    benchmark["ma200"] = [100.0, 100.0]
    benchmark["ma50_slope20"] = [0.01, 0.01]
    return pd.concat([stock, benchmark], ignore_index=True)


def test_strategy_generates_next_session_signal_from_yesterday_dip() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    strategy = BuyTheDipStrategy(config.strategy)

    signals = strategy.generate_signals(feature_rows())

    assert len(signals) == 1
    signal = signals[0]
    assert signal.symbol == "AAPL"
    assert signal.signal_session == date(2026, 6, 26)
    assert signal.earliest_order_session == date(2026, 6, 29)
    assert signal.market_regime is MarketRegime.GREEN
    assert signal.data_cutoff_utc < signal.signal_time_utc < signal.earliest_order_time_utc


def test_red_market_blocks_candidate() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    features = feature_rows()
    benchmark = features["symbol"] == "QQQ"
    features.loc[benchmark, "close"] = 80.0

    signals = BuyTheDipStrategy(config.strategy).generate_signals(features)

    assert signals == []
