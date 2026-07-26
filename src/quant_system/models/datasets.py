"""Candidate-level datasets for conservative model ranking."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from quant_system.models.labels import RelativeReturnLabelConfig, build_relative_return_labels
from quant_system.strategy.buy_the_dip import BuyTheDipStrategy
from quant_system.strategy.config import BuyTheDipConfig

DEFAULT_FEATURE_COLUMNS = (
    "drawdown_from_high20",
    "rsi14",
    "atr20",
    "adx14",
    "avg_dollar_volume20",
    "relative_return60",
    "ma50_slope20",
)


@dataclass(frozen=True)
class CandidateDatasetConfig:
    """Feature and label settings for candidate-level ranking datasets."""

    holding_sessions: tuple[int, ...] = (5, 10)
    feature_columns: tuple[str, ...] = DEFAULT_FEATURE_COLUMNS

    def __post_init__(self) -> None:
        if not self.feature_columns:
            raise ValueError("at least one feature column is required")


def build_candidate_dataset(
    features: pd.DataFrame,
    strategy_config: BuyTheDipConfig,
    *,
    dataset_config: CandidateDatasetConfig | None = None,
) -> pd.DataFrame:
    """Build one row per rules-approved candidate with labels and features."""
    dataset_config = dataset_config or CandidateDatasetConfig()
    missing_features = set(dataset_config.feature_columns) - set(features.columns)
    if missing_features:
        raise ValueError(f"features missing model columns: {sorted(missing_features)}")

    strategy = BuyTheDipStrategy(strategy_config.strategy)
    signals = strategy.generate_signals(features)
    if not signals:
        return _empty_dataset(dataset_config)

    signal_frame = pd.DataFrame([signal.model_dump(mode="python") for signal in signals])
    signal_frame["signal_id"] = signal_frame["signal_id"].astype(str)
    feature_slice = features.copy()
    feature_slice["session_date_ny"] = pd.to_datetime(feature_slice["session_date_ny"]).dt.date
    feature_slice["symbol"] = feature_slice["symbol"].astype("string").str.upper()

    dataset = signal_frame.merge(
        feature_slice[
            [
                "symbol",
                "session_date_ny",
                "close",
                *dataset_config.feature_columns,
            ]
        ],
        how="left",
        left_on=["symbol", "signal_session"],
        right_on=["symbol", "session_date_ny"],
        validate="one_to_one",
    )
    dataset = dataset.rename(columns={"close": "signal_close_feature"})
    labels = build_relative_return_labels(
        dataset[["signal_id", "symbol", "signal_session", "earliest_order_session"]],
        features[["symbol", "session_date_ny", "close"]],
        config=RelativeReturnLabelConfig(
            holding_sessions=dataset_config.holding_sessions,
            benchmark_symbol=strategy_config.strategy.benchmark_symbol,
        ),
    )
    dataset = dataset.merge(
        labels.drop(columns=["symbol", "signal_session"]),
        how="left",
        on="signal_id",
        validate="one_to_one",
    )
    return dataset.sort_values(["signal_session", "symbol"]).reset_index(drop=True)


def _empty_dataset(config: CandidateDatasetConfig) -> pd.DataFrame:
    label_columns = []
    for horizon in config.holding_sessions:
        label_columns.extend(
            [
                f"exit_session_{horizon}",
                f"stock_return_{horizon}",
                f"benchmark_return_{horizon}",
                f"relative_return_{horizon}",
                f"label_end_session_{horizon}",
            ]
        )
    return pd.DataFrame(
        columns=[
            "signal_id",
            "symbol",
            "signal_session",
            "earliest_order_session",
            *config.feature_columns,
            *label_columns,
        ]
    )
