from __future__ import annotations

from pathlib import Path

from quant_system.decision.hierarchy import (
    calibrate_strategy_context,
    classify_trend_state,
    load_hierarchy_proxies,
)


def test_load_hierarchy_proxies_assigns_three_layers() -> None:
    proxies = load_hierarchy_proxies(
        universe_paths=(
            Path("configs/universe/ai_watchlist.yaml"),
            Path("configs/universe/hedge_overlay.yaml"),
        ),
        benchmark_path=Path("configs/universe/benchmarks.yaml"),
    )

    by_symbol = {proxy.symbol: proxy for proxy in proxies}
    assert by_symbol["QQQ"].layer == "market"
    assert by_symbol["SMH"].layer == "sector"
    assert by_symbol["NVDA"].layer == "stock"
    assert by_symbol["NVDA"].role == "leader_stock"
    assert by_symbol["NVDA"].proxy_for == "ai_chips"


def test_classify_trend_state_detects_drift_down() -> None:
    state = classify_trend_state(
        {
            "return_5d": -0.02,
            "return_20d": -0.08,
            "return_60d": 0.05,
            "relative_return_20d": -0.04,
            "drawdown_20d": 0.09,
            "drawdown_60d": 0.11,
            "rsi14": 42,
            "ma20_slope_5d": -0.03,
            "ma50_slope_20d": 0.01,
            "down_day_ratio_20d": 0.60,
            "above_ma20": False,
            "above_ma50": True,
        }
    )

    assert state == "DRIFT_DOWN"


def test_classify_trend_state_detects_confirmed_reversal() -> None:
    state = classify_trend_state(
        {
            "return_5d": 0.05,
            "return_20d": 0.09,
            "return_60d": -0.02,
            "relative_return_20d": 0.04,
            "drawdown_20d": 0.02,
            "drawdown_60d": 0.12,
            "rsi14": 58,
            "ma20_slope_5d": 0.02,
            "ma50_slope_20d": 0.00,
            "down_day_ratio_20d": 0.40,
            "above_ma20": True,
            "above_ma50": True,
        }
    )

    assert state == "CONFIRMED_REVERSAL"


def test_calibration_context_turns_defensive_when_ai_leaders_drift_and_defense_leads() -> None:
    context = calibrate_strategy_context(
        state_by_symbol={
            "QQQ": {"trend_state": "WEAKENING", "layer": "market"},
            "SPY": {"trend_state": "UPTREND", "layer": "market"},
            "NVDA": {
                "trend_state": "DRIFT_DOWN",
                "universe_role": "ai_alpha",
                "role": "leader_stock",
                "above_ma20": False,
                "return_20d": -0.10,
            },
            "AVGO": {
                "trend_state": "WEAKENING",
                "universe_role": "ai_alpha",
                "role": "leader_stock",
                "above_ma20": False,
                "return_20d": -0.05,
            },
            "COST": {
                "trend_state": "UPTREND",
                "universe_role": "hedge_overlay",
                "role": "leader_stock",
                "above_ma20": True,
                "return_20d": 0.02,
            },
        },
        rotation_summary={"status": "AI_MOMENTUM_WEAKENING"},
        pair_spreads=[
            {
                "name": "ai_alpha_vs_hedge_overlay",
                "spread_relative_20d": -0.08,
                "spread_relative_60d": 0.03,
            }
        ],
    )

    assert context["candidate_tier_context"] == "DEFENSIVE"
    assert context["risk_multiplier_hint"] == 0.0


def test_calibration_context_allows_relaxed_watchlist_on_leader_reversal() -> None:
    context = calibrate_strategy_context(
        state_by_symbol={
            "QQQ": {"trend_state": "CONFIRMED_REVERSAL", "layer": "market"},
            "SPY": {"trend_state": "UPTREND", "layer": "market"},
            "NVDA": {
                "trend_state": "CONFIRMED_REVERSAL",
                "universe_role": "ai_alpha",
                "role": "leader_stock",
                "above_ma20": True,
                "return_20d": 0.10,
            },
            "AVGO": {
                "trend_state": "REVERSAL_ATTEMPT",
                "universe_role": "ai_alpha",
                "role": "leader_stock",
                "above_ma20": True,
                "return_20d": 0.04,
            },
        },
        rotation_summary={"status": "AI_ROTATION_IMPROVING"},
        pair_spreads=[
            {
                "name": "ai_alpha_vs_hedge_overlay",
                "spread_relative_20d": 0.04,
                "spread_relative_60d": -0.02,
            }
        ],
    )

    assert context["candidate_tier_context"] == "RELAXED_WATCHLIST"
    assert context["ai_leader_breadth"]["reversal_or_confirmed_fraction"] == 1.0
