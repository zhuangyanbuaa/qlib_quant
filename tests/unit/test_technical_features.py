import numpy as np
import pandas as pd

from quant_system.features.technical import (
    TechnicalFeatureConfig,
    build_technical_features,
)


def price_frame(symbol: str, closes: list[float]) -> pd.DataFrame:
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "symbol": symbol,
            "session_date_ny": pd.bdate_range("2024-01-02", periods=len(close)),
            "open": close - 0.2,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1_000_000,
        }
    )


def short_config() -> TechnicalFeatureConfig:
    return TechnicalFeatureConfig(
        fast_ma=2,
        dip_ma=3,
        trend_ma=3,
        long_ma=4,
        trend_slope_lookback=1,
        rsi_period=2,
        atr_period=2,
        adx_period=2,
        relative_return_period=2,
    )


def test_features_do_not_change_when_future_price_changes() -> None:
    prices = pd.concat(
        [
            price_frame("QQQ", [100, 101, 102, 103, 104, 105]),
            price_frame("AAPL", [50, 51, 49, 50, 52, 53]),
        ],
        ignore_index=True,
    )
    original = build_technical_features(prices, config=short_config())
    changed_prices = prices.copy()
    last_aapl = (changed_prices["symbol"] == "AAPL") & (
        changed_prices["session_date_ny"] == changed_prices["session_date_ny"].max()
    )
    changed_prices.loc[last_aapl, ["open", "high", "low", "close"]] = [
        900,
        1_100,
        800,
        1_000,
    ]
    changed = build_technical_features(changed_prices, config=short_config())

    cutoff = original["session_date_ny"] < original["session_date_ny"].max()
    columns = ["ma5", "ma20", "ma50", "ma200", "rsi14", "atr20", "relative_return60"]
    pd.testing.assert_frame_equal(
        original.loc[cutoff, columns].reset_index(drop=True),
        changed.loc[cutoff, columns].reset_index(drop=True),
    )


def test_grouped_features_do_not_bleed_between_symbols() -> None:
    prices = pd.concat(
        [
            price_frame("QQQ", [100, 101, 102, 103, 104]),
            price_frame("FLAT", [10, 10, 10, 10, 10]),
        ],
        ignore_index=True,
    )

    features = build_technical_features(prices, config=short_config())
    flat_latest = features.loc[features["symbol"] == "FLAT"].iloc[-1]

    assert flat_latest["ma5"] == 10
    assert flat_latest["rsi14"] == 50
    assert flat_latest["return60"] == 0


def test_relative_return_uses_same_date_benchmark() -> None:
    prices = pd.concat(
        [
            price_frame("QQQ", [100, 100, 110, 110]),
            price_frame("AAPL", [100, 100, 120, 120]),
        ],
        ignore_index=True,
    )

    features = build_technical_features(prices, config=short_config())
    latest = features.loc[features["symbol"] == "AAPL"].iloc[-1]

    assert np.isclose(latest["return60"], 0.2)
    assert np.isclose(latest["benchmark_return60"], 0.1)
    assert np.isclose(latest["relative_return60"], 0.1)
