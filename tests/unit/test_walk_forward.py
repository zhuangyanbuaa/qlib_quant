from datetime import date

import pandas as pd

from quant_system.models.walk_forward import (
    WalkForwardConfig,
    build_purged_walk_forward_splits,
)


def dataset() -> pd.DataFrame:
    sessions = pd.bdate_range("2026-01-01", periods=12).date
    return pd.DataFrame(
        {
            "signal_id": [f"s{i}" for i in range(len(sessions))],
            "signal_session": sessions,
            "label_end_session_2": [
                sessions[min(i + 2, len(sessions) - 1)] for i in range(len(sessions))
            ],
            "relative_return_2": [float(i) / 100 for i in range(len(sessions))],
        }
    )


def test_walk_forward_purges_training_labels_inside_embargo_window() -> None:
    folds = build_purged_walk_forward_splits(
        dataset(),
        WalkForwardConfig(
            train_sessions=8,
            validation_sessions=2,
            step_sessions=2,
            hold_period=2,
            feature_lookback_days=3,
            minimum_train_rows=1,
            minimum_validation_rows=1,
        ),
    )

    first = folds[0]
    expected_cutoff = date(2026, 1, 13) - pd.Timedelta(days=5)

    assert first.validation_start == date(2026, 1, 13)
    assert first.embargo_cutoff == expected_cutoff
    assert first.status == "READY"
    assert all(
        dataset().loc[index, "label_end_session_2"] <= expected_cutoff
        for index in first.train_indices
    )
    assert set(first.validation_indices) == {8, 9}


def test_walk_forward_reports_insufficient_without_shortening_embargo() -> None:
    folds = build_purged_walk_forward_splits(
        dataset(),
        WalkForwardConfig(
            train_sessions=6,
            validation_sessions=2,
            step_sessions=2,
            hold_period=2,
            feature_lookback_days=30,
            minimum_train_rows=1,
            minimum_validation_rows=1,
        ),
    )

    assert folds[0].status == "INSUFFICIENT_SAMPLE"
    assert folds[0].train_indices == ()
