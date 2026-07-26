import numpy as np
import pandas as pd

from quant_system.models.lightgbm import LightGBMConfig, LightGBMRanker, run_lightgbm_walk_forward
from quant_system.models.walk_forward import WalkForwardConfig, build_purged_walk_forward_splits


class FakeRegressor:
    def fit(self, features: pd.DataFrame, target: pd.Series) -> "FakeRegressor":
        self.feature_importances_ = np.arange(1, features.shape[1] + 1)
        self.weights_ = np.arange(1, features.shape[1] + 1, dtype=float)
        self.target_mean_ = float(target.mean())
        return self

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        return features.to_numpy() @ self.weights_ + self.target_mean_


def dataset() -> pd.DataFrame:
    sessions = pd.bdate_range("2026-01-01", periods=14).date
    rows = []
    for index, session in enumerate(sessions):
        rows.append(
            {
                "signal_id": f"s{index}",
                "symbol": "AAPL",
                "signal_session": session,
                "feature": float(index),
                "other_feature": float(index % 3),
                "relative_return_2": float(index) / 100,
                "label_end_session_2": sessions[min(index + 2, len(sessions) - 1)],
            }
        )
    return pd.DataFrame(rows)


def test_lightgbm_ranker_predicts_with_injected_estimator() -> None:
    frame = dataset().iloc[:10]
    ranker = LightGBMRanker(
        LightGBMConfig(
            feature_columns=("feature", "other_feature"),
            target_column="relative_return_2",
            minimum_train_rows=2,
        ),
        estimator_factory=FakeRegressor,
    )

    ranker.fit(frame)
    predictions = ranker.predict(dataset().iloc[10:12])

    assert predictions.notna().all()
    assert list(ranker.feature_importances_.index) == ["feature", "other_feature"]


def test_lightgbm_walk_forward_outputs_oof_and_importance() -> None:
    frame = dataset()
    folds = build_purged_walk_forward_splits(
        frame,
        WalkForwardConfig(
            train_sessions=8,
            validation_sessions=2,
            step_sessions=2,
            hold_period=2,
            feature_lookback_days=1,
            minimum_train_rows=1,
            minimum_validation_rows=1,
        ),
    )

    result = run_lightgbm_walk_forward(
        frame,
        folds,
        LightGBMConfig(
            feature_columns=("feature", "other_feature"),
            target_column="relative_return_2",
            minimum_train_rows=2,
        ),
        estimator_factory=FakeRegressor,
    )

    assert result.summary["ready_fold_count"] == 3
    assert len(result.predictions) == 6
    assert set(result.feature_importance["feature"]) == {"feature", "other_feature"}
