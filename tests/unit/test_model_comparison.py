import pandas as pd

from quant_system.models.comparison import (
    calibration_table,
    comparison_table,
    feature_stability,
    lightgbm_promotion_status,
)


def predictions(multiplier: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "prediction": [1.0, 2.0, 3.0, 4.0],
            "target": [0.01, 0.02, 0.03, 0.04 * multiplier],
        }
    )


def test_calibration_table_orders_highest_bucket_first() -> None:
    table = calibration_table(predictions(1), buckets=2)

    assert list(table["count"]) == [2, 2]
    assert table.iloc[0]["mean_prediction"] > table.iloc[1]["mean_prediction"]


def test_feature_stability_summarizes_importance_across_folds() -> None:
    stability = feature_stability(
        pd.DataFrame(
            {
                "fold_number": [0, 1, 0, 1],
                "feature": ["a", "a", "b", "b"],
                "importance": [10.0, 14.0, 0.0, 1.0],
            }
        )
    )

    assert stability.iloc[0]["feature"] == "a"
    assert stability.iloc[0]["nonzero_fold_count"] == 2


def test_lightgbm_promotion_requires_beating_rules_and_ridge() -> None:
    comparison = comparison_table(
        dataset=pd.DataFrame({"relative_return_2": [0.01, 0.02, 0.03, 0.04]}),
        target_column="relative_return_2",
        ridge_predictions=predictions(1),
        lightgbm_predictions=predictions(2),
        top_fraction=0.5,
    )

    assert lightgbm_promotion_status(comparison) == "NOT_ELIGIBLE_BASELINE_NOT_BEATEN"
