"""Transparent benchmark regime classification."""

from __future__ import annotations

import pandas as pd

from quant_system.domain.enums import MarketRegime


def classify_market_regime(
    benchmark_features: pd.DataFrame,
    *,
    yellow_maximum_ma50_decline: float,
) -> pd.Series:
    """Classify each benchmark row without reading future observations."""
    regime = pd.Series(
        MarketRegime.RED,
        index=benchmark_features.index,
        dtype="string",
    )
    above_long_trend = benchmark_features["close"] > benchmark_features["ma200"]
    green = above_long_trend & (benchmark_features["ma50_slope20"] >= 0)
    yellow = above_long_trend & (
        benchmark_features["ma50_slope20"] >= -yellow_maximum_ma50_decline
    )
    regime.loc[yellow] = MarketRegime.YELLOW
    regime.loc[green] = MarketRegime.GREEN
    return regime
