from pathlib import Path

import pandas as pd

from quant_system.models.datasets import CandidateDatasetConfig, build_candidate_dataset
from quant_system.strategy.config import load_buy_the_dip_config


def feature_rows() -> pd.DataFrame:
    dates = pd.to_datetime(["2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30"])
    common = {
        "open": [100.0, 100.0, 104.0, 106.0],
        "high": [101.0, 104.0, 108.0, 109.0],
        "low": [94.0, 99.0, 103.0, 105.0],
        "volume": [1_000_000, 1_000_000, 1_000_000, 1_000_000],
        "ma20": [105.0, 103.0, 103.0, 103.0],
        "ma50": [100.0, 100.0, 100.0, 100.0],
        "ma200": [90.0, 90.0, 90.0, 90.0],
        "ma50_slope20": [0.01, 0.01, 0.01, 0.01],
        "rolling_high20": [105.0, 105.0, 108.0, 109.0],
        "drawdown_from_high20": [0.10, 0.02, 0.0, 0.0],
        "avg_dollar_volume20": [100_000_000.0] * 4,
        "rsi14": [40.0, 50.0, 55.0, 55.0],
        "atr20": [4.0, 4.0, 4.0, 4.0],
        "adx14": [25.0, 25.0, 25.0, 25.0],
        "return60": [0.15, 0.16, 0.17, 0.18],
        "benchmark_return60": [0.05, 0.05, 0.06, 0.07],
        "relative_return60": [0.10, 0.11, 0.11, 0.11],
        "previous_close": [96.0, 94.5, 103.0, 106.0],
        "previous_high": [100.0, 101.0, 104.0, 108.0],
        "previous_ma5": [98.0, 96.0, 98.0, 99.0],
        "feature_ready": [True, True, True, True],
    }
    stock = pd.DataFrame(
        {
            "symbol": "AAPL",
            "session_date_ny": dates,
            "close": [94.5, 103.0, 106.0, 108.0],
            "ma5": [96.0, 98.0, 99.0, 100.0],
            **common,
        }
    )
    benchmark = stock.copy()
    benchmark["symbol"] = "QQQ"
    benchmark["close"] = [110.0, 112.0, 113.0, 114.0]
    benchmark["ma200"] = [100.0, 100.0, 100.0, 100.0]
    benchmark["ma50_slope20"] = [0.01, 0.01, 0.01, 0.01]
    return pd.concat([stock, benchmark], ignore_index=True)


def test_candidate_dataset_contains_only_rule_candidates_with_labels() -> None:
    strategy_config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    dataset = build_candidate_dataset(
        feature_rows(),
        strategy_config,
        dataset_config=CandidateDatasetConfig(holding_sessions=(1,), feature_columns=("rsi14",)),
    )

    assert len(dataset) == 1
    row = dataset.iloc[0]
    assert row["symbol"] == "AAPL"
    assert row["signal_session"].isoformat() == "2026-06-26"
    assert row["earliest_order_session"].isoformat() == "2026-06-29"
    assert row["rsi14"] == 50.0
    assert "relative_return_1" in dataset.columns


def test_candidate_dataset_preserves_feature_column_names_when_signal_fields_collide() -> None:
    strategy_config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    dataset = build_candidate_dataset(
        feature_rows(),
        strategy_config,
        dataset_config=CandidateDatasetConfig(
            holding_sessions=(1,),
            feature_columns=("atr20", "rsi14"),
        ),
    )

    assert "atr20" in dataset.columns
    assert "signal_atr20" in dataset.columns
    assert dataset.iloc[0]["atr20"] == 4.0
