from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from quant_system.strategy.calibration import generate_tiered_candidates, rules_for_tier
from quant_system.strategy.config import load_buy_the_dip_config


def test_rules_for_tier_makes_strict_and_relaxed_thresholds_monotonic() -> None:
    base = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml")).strategy

    strict = rules_for_tier(base, "STRICT")
    relaxed = rules_for_tier(base, "RELAXED")

    assert strict.minimum_relative_return_60 > base.minimum_relative_return_60
    assert strict.maximum_drawdown < base.maximum_drawdown
    assert strict.maximum_rsi < base.maximum_rsi
    assert relaxed.minimum_relative_return_60 < base.minimum_relative_return_60
    assert relaxed.minimum_drawdown < base.minimum_drawdown
    assert relaxed.maximum_rsi > base.maximum_rsi


def test_generate_tiered_candidates_coalesces_to_strictest_passing_tier() -> None:
    base = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml")).strategy
    features = pd.concat(
        [
            _candidate_pair(
                symbol="STRICTY",
                previous_close=96.0,
                confirmation_close=103.0,
                rsi=38.0,
                relative_return=0.08,
            ),
            _candidate_pair(
                symbol="BASEY",
                previous_close=94.5,
                confirmation_close=103.0,
                rsi=40.0,
                relative_return=0.03,
            ),
            _candidate_pair(
                symbol="RELAXY",
                previous_close=101.0,
                confirmation_close=103.0,
                rsi=50.0,
                relative_return=-0.02,
            ),
            _benchmark_pair(),
        ],
        ignore_index=True,
    )

    candidates = generate_tiered_candidates(
        features=features,
        as_of=date(2026, 6, 26),
        base_config=base,
    )

    by_symbol = {candidate.signal.symbol: candidate for candidate in candidates}
    assert by_symbol["STRICTY"].calibration_tier == "STRICT"
    assert by_symbol["STRICTY"].passed_tiers == ("STRICT", "BASELINE", "RELAXED")
    assert by_symbol["BASEY"].calibration_tier == "BASELINE"
    assert by_symbol["BASEY"].passed_tiers == ("BASELINE", "RELAXED")
    assert by_symbol["RELAXY"].calibration_tier == "RELAXED"
    assert by_symbol["RELAXY"].passed_tiers == ("RELAXED",)


def _candidate_pair(
    *,
    symbol: str,
    previous_close: float,
    confirmation_close: float,
    rsi: float,
    relative_return: float,
) -> pd.DataFrame:
    dates = pd.to_datetime(["2026-06-25", "2026-06-26"])
    return pd.DataFrame(
        {
            "symbol": symbol,
            "session_date_ny": dates,
            "open": [100.0, 100.0],
            "high": [101.0, 104.0],
            "low": [94.0, 99.0],
            "close": [previous_close, confirmation_close],
            "volume": [1_000_000, 1_000_000],
            "ma5": [98.0, 98.0],
            "ma20": [105.0, 103.0],
            "ma50": [100.0, 100.0],
            "ma200": [90.0, 90.0],
            "ma50_slope20": [0.01, 0.01],
            "rolling_high20": [105.0, 105.0],
            "drawdown_from_high20": [1 - previous_close / 105.0, 0.02],
            "avg_dollar_volume20": [100_000_000.0, 100_000_000.0],
            "rsi14": [rsi, 50.0],
            "atr20": [4.0, 4.0],
            "adx14": [25.0, 25.0],
            "return60": [relative_return + 0.05, relative_return + 0.05],
            "benchmark_return60": [0.05, 0.05],
            "relative_return60": [relative_return, relative_return],
            "previous_close": [previous_close + 1.0, previous_close],
            "previous_high": [100.0, 101.0],
            "previous_ma5": [98.0, 98.0],
            "feature_ready": [True, True],
        }
    )


def _benchmark_pair() -> pd.DataFrame:
    frame = _candidate_pair(
        symbol="QQQ",
        previous_close=110.0,
        confirmation_close=112.0,
        rsi=50.0,
        relative_return=0.0,
    )
    frame["ma200"] = [100.0, 100.0]
    frame["ma50_slope20"] = [0.01, 0.01]
    return frame
