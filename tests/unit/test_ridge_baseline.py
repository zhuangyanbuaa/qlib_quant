import pandas as pd
import pytest

from quant_system.models.ridge import RidgeConfig, RidgeRanker, evaluate_predictions
from quant_system.models.walk_forward import WalkForwardConfig, build_purged_walk_forward_splits


def dataset() -> pd.DataFrame:
    sessions = pd.bdate_range("2026-01-01", periods=24).date
    rows = []
    for index, session in enumerate(sessions):
        rows.append(
            {
                "signal_id": f"s{index}",
                "symbol": "AAPL" if index % 2 else "MSFT",
                "signal_session": session,
                "feature": float(index),
                "other_feature": 1.0,
                "relative_return_2": float(index) / 100,
                "label_end_session_2": sessions[min(index + 2, len(sessions) - 1)],
            }
        )
    return pd.DataFrame(rows)


def test_ridge_ranker_learns_monotonic_relationship() -> None:
    frame = dataset().iloc[:12]
    model = RidgeRanker(
        RidgeConfig(feature_columns=("feature",), target_column="relative_return_2")
    )

    model.fit(frame)
    predictions = model.predict(dataset().iloc[12:16])

    assert predictions.is_monotonic_increasing


def test_prediction_metrics_include_rank_ic_and_top_bucket() -> None:
    frame = pd.DataFrame(
        {
            "prediction": [0.1, 0.2, 0.3],
            "target": [0.0, 0.1, 0.2],
        }
    )

    metrics = evaluate_predictions(frame)

    assert metrics["prediction_count"] == 3
    assert metrics["rank_ic"] == pytest.approx(1.0)
    assert metrics["top_bucket_mean_target"] == pytest.approx(0.2)


def test_walk_forward_ridge_skips_insufficient_folds() -> None:
    from quant_system.models.ridge import run_ridge_walk_forward

    frame = dataset()
    folds = build_purged_walk_forward_splits(
        frame,
        WalkForwardConfig(
            train_sessions=12,
            validation_sessions=4,
            step_sessions=4,
            hold_period=2,
            feature_lookback_days=2,
            minimum_train_rows=4,
            minimum_validation_rows=2,
        ),
    )

    result = run_ridge_walk_forward(
        frame,
        folds,
        RidgeConfig(feature_columns=("feature",), target_column="relative_return_2"),
    )

    assert not result.predictions.empty
    assert set(result.predictions.columns) >= {"signal_id", "fold_number", "prediction", "target"}
    assert result.summary["ready_fold_count"] >= 1
    assert result.summary["prediction_count"] == len(result.predictions)
