from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from quant_system.models.config import RankingBaselineSettings
from quant_system.models.daily_ranker import (
    attach_model_scores,
    rank_daily_candidates_with_lightgbm,
)
from quant_system.strategy.config import load_buy_the_dip_config


class FakeRegressor:
    def fit(self, features: pd.DataFrame, target: pd.Series) -> "FakeRegressor":
        self.feature_importances_ = np.ones(features.shape[1])
        self.weights_ = np.arange(1, features.shape[1] + 1, dtype=float)
        self.target_mean_ = float(target.mean())
        return self

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        return features.to_numpy() @ self.weights_ + self.target_mean_


def test_daily_lightgbm_ranker_scores_current_rows_from_mature_history(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "quant_system.models.daily_ranker.build_candidate_dataset",
        lambda *_args, **_kwargs: training_dataset(),
    )
    rows = [
        {"signal_id": "today-a", "symbol": "AAPL", "recommended_action": "KEEP"},
        {"signal_id": "today-b", "symbol": "MSFT", "recommended_action": "KEEP"},
    ]

    ranking = rank_daily_candidates_with_lightgbm(
        features=scoring_features(),
        candidate_rows=rows,
        strategy_config=load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml")),
        model_settings=ranking_settings(minimum_train_rows=2),
        as_of=date(2026, 1, 10),
        estimator_factory=FakeRegressor,
    )
    attach_model_scores(rows, ranking)

    assert ranking.context["status"] == "SCORED"
    assert ranking.context["training_rows"] == 2
    assert ranking.context["scored_rows"] == 2
    assert {row["model_rank_status"] for row in rows} == {"SCORED"}
    assert sorted(row["model_rank"] for row in rows) == [1, 2]
    assert {row["recommended_action"] for row in rows} == {"KEEP"}


def test_daily_lightgbm_ranker_requires_minimum_mature_training_rows(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "quant_system.models.daily_ranker.build_candidate_dataset",
        lambda *_args, **_kwargs: training_dataset(),
    )

    ranking = rank_daily_candidates_with_lightgbm(
        features=scoring_features(),
        candidate_rows=[{"signal_id": "today-a", "symbol": "AAPL"}],
        strategy_config=load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml")),
        model_settings=ranking_settings(minimum_train_rows=3),
        as_of=date(2026, 1, 10),
        estimator_factory=FakeRegressor,
    )

    assert ranking.context["status"] == "INSUFFICIENT_TRAINING_ROWS"
    assert ranking.context["training_rows"] == 2
    assert ranking.scores_by_signal_id == {}


def training_dataset() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "signal_id": "old-a",
                "symbol": "AAPL",
                "signal_session": date(2026, 1, 2),
                "label_end_session_5": date(2026, 1, 8),
                "relative_return_5": 0.01,
                "feature": 1.0,
            },
            {
                "signal_id": "old-b",
                "symbol": "MSFT",
                "signal_session": date(2026, 1, 3),
                "label_end_session_5": date(2026, 1, 9),
                "relative_return_5": 0.02,
                "feature": 2.0,
            },
            {
                "signal_id": "future-label",
                "symbol": "NVDA",
                "signal_session": date(2026, 1, 9),
                "label_end_session_5": date(2026, 1, 15),
                "relative_return_5": 0.99,
                "feature": 99.0,
            },
        ]
    )


def scoring_features() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "session_date_ny": date(2026, 1, 10),
                "feature": 3.0,
            },
            {
                "symbol": "MSFT",
                "session_date_ny": date(2026, 1, 10),
                "feature": 4.0,
            },
        ]
    )


def ranking_settings(*, minimum_train_rows: int) -> RankingBaselineSettings:
    return RankingBaselineSettings.model_validate(
        {
            "dataset": {"holding_sessions": [5], "feature_columns": ["feature"]},
            "walk_forward": {
                "train_sessions": 10,
                "validation_sessions": 2,
                "step_sessions": 2,
                "hold_period": 5,
                "feature_lookback_days": 1,
                "minimum_train_rows": 1,
                "minimum_validation_rows": 1,
            },
            "ridge": {"alpha": 1.0, "top_fraction": 0.5},
            "lightgbm": {
                "n_estimators": 5,
                "learning_rate": 0.1,
                "num_leaves": 3,
                "max_depth": 2,
                "min_child_samples": 1,
                "subsample": 1.0,
                "colsample_bytree": 1.0,
                "reg_alpha": 0.0,
                "reg_lambda": 1.0,
                "random_state": 42,
                "n_jobs": 1,
                "top_fraction": 0.5,
                "minimum_train_rows": minimum_train_rows,
            },
            "calibration": {"buckets": 2},
        }
    )
