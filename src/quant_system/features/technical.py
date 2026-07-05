"""Point-in-time technical features for the daily strategy."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

REQUIRED_PRICE_COLUMNS = {
    "symbol",
    "session_date_ny",
    "open",
    "high",
    "low",
    "close",
    "volume",
}


@dataclass(frozen=True)
class TechnicalFeatureConfig:
    """Lookback windows for the first rules-only strategy."""

    fast_ma: int = 5
    dip_ma: int = 20
    trend_ma: int = 50
    long_ma: int = 200
    trend_slope_lookback: int = 20
    rsi_period: int = 14
    atr_period: int = 20
    adx_period: int = 14
    relative_return_period: int = 60


def build_technical_features(
    prices: pd.DataFrame,
    *,
    benchmark_symbol: str = "QQQ",
    config: TechnicalFeatureConfig | None = None,
) -> pd.DataFrame:
    """Build causal features using only each row and its historical predecessors."""
    config = config or TechnicalFeatureConfig()
    missing = REQUIRED_PRICE_COLUMNS - set(prices.columns)
    if missing:
        raise ValueError(f"prices are missing required columns: {sorted(missing)}")
    if prices.empty:
        return prices.copy()

    frame = prices.copy()
    frame["symbol"] = frame["symbol"].astype("string").str.upper()
    frame["session_date_ny"] = pd.to_datetime(frame["session_date_ny"])
    frame = frame.sort_values(["symbol", "session_date_ny"]).reset_index(drop=True)
    grouped = frame.groupby("symbol", sort=False, group_keys=False)

    frame["ma5"] = grouped["close"].transform(
        lambda values: values.rolling(config.fast_ma, min_periods=config.fast_ma).mean()
    )
    frame["ma20"] = grouped["close"].transform(
        lambda values: values.rolling(config.dip_ma, min_periods=config.dip_ma).mean()
    )
    frame["ma50"] = grouped["close"].transform(
        lambda values: values.rolling(config.trend_ma, min_periods=config.trend_ma).mean()
    )
    frame["ma200"] = grouped["close"].transform(
        lambda values: values.rolling(config.long_ma, min_periods=config.long_ma).mean()
    )
    frame["ma50_slope20"] = grouped["ma50"].transform(
        lambda values: values / values.shift(config.trend_slope_lookback) - 1
    )
    frame["rolling_high20"] = grouped["close"].transform(
        lambda values: values.rolling(config.dip_ma, min_periods=config.dip_ma).max()
    )
    frame["drawdown_from_high20"] = 1 - frame["close"] / frame["rolling_high20"]
    frame["avg_dollar_volume20"] = (
        (frame["close"] * frame["volume"])
        .groupby(frame["symbol"], sort=False)
        .transform(
            lambda values: values.rolling(config.dip_ma, min_periods=config.dip_ma).mean()
        )
    )
    frame["rsi14"] = grouped["close"].transform(
        lambda values: _wilder_rsi(values, config.rsi_period)
    )
    frame["atr20"] = _average_true_range(frame, config.atr_period)
    frame["adx14"] = _average_directional_index(frame, config.adx_period)
    frame["return60"] = grouped["close"].transform(
        lambda values: values / values.shift(config.relative_return_period) - 1
    )

    benchmark = frame.loc[
        frame["symbol"] == benchmark_symbol.upper(),
        ["session_date_ny", "return60"],
    ].rename(columns={"return60": "benchmark_return60"})
    frame = frame.merge(benchmark, how="left", on="session_date_ny", validate="many_to_one")
    frame["relative_return60"] = frame["return60"] - frame["benchmark_return60"]

    grouped = frame.groupby("symbol", sort=False, group_keys=False)
    frame["previous_close"] = grouped["close"].shift(1)
    frame["previous_high"] = grouped["high"].shift(1)
    frame["previous_ma5"] = grouped["ma5"].shift(1)

    required_features = [
        "ma5",
        "ma20",
        "ma50",
        "ma200",
        "ma50_slope20",
        "rolling_high20",
        "rsi14",
        "atr20",
        "adx14",
        "avg_dollar_volume20",
        "relative_return60",
    ]
    frame["feature_ready"] = frame[required_features].notna().all(axis=1)
    return frame.sort_values(["session_date_ny", "symbol"]).reset_index(drop=True)


def _wilder_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    average_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    average_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    relative_strength = average_gain / average_loss
    rsi = 100 - 100 / (1 + relative_strength)
    rsi = rsi.mask((average_loss == 0) & (average_gain > 0), 100.0)
    return rsi.mask((average_loss == 0) & (average_gain == 0), 50.0)


def _average_true_range(frame: pd.DataFrame, period: int) -> pd.Series:
    previous_close = frame.groupby("symbol", sort=False)["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.groupby(frame["symbol"], sort=False).transform(
        lambda values: values.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    )


def _average_directional_index(frame: pd.DataFrame, period: int) -> pd.Series:
    grouped = frame.groupby("symbol", sort=False)
    upward = grouped["high"].diff()
    downward = -grouped["low"].diff()
    plus_dm = pd.Series(
        np.where((upward > downward) & (upward > 0), upward, 0.0),
        index=frame.index,
    )
    minus_dm = pd.Series(
        np.where((downward > upward) & (downward > 0), downward, 0.0),
        index=frame.index,
    )
    previous_close = grouped["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    def smooth(values: pd.Series) -> pd.Series:
        return values.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    smoothed_tr = true_range.groupby(frame["symbol"], sort=False).transform(smooth)
    smoothed_plus = plus_dm.groupby(frame["symbol"], sort=False).transform(smooth)
    smoothed_minus = minus_dm.groupby(frame["symbol"], sort=False).transform(smooth)
    plus_di = 100 * smoothed_plus / smoothed_tr.replace(0, np.nan)
    minus_di = 100 * smoothed_minus / smoothed_tr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.groupby(frame["symbol"], sort=False).transform(smooth)
