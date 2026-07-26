"""OOF comparison, calibration, and stability helpers."""

from __future__ import annotations

from typing import Any

import pandas as pd

from quant_system.models.ridge import evaluate_predictions


def rule_candidate_metrics(dataset: pd.DataFrame, *, target_column: str) -> dict[str, Any]:
    """Evaluate the unranked rules-only candidate set."""
    if dataset.empty or target_column not in dataset.columns:
        return {
            "prediction_count": 0,
            "mean_target": None,
            "positive_rate": None,
        }
    target = pd.to_numeric(dataset[target_column], errors="coerce").dropna()
    if target.empty:
        return {
            "prediction_count": 0,
            "mean_target": None,
            "positive_rate": None,
        }
    return {
        "prediction_count": len(target),
        "mean_target": float(target.mean()),
        "positive_rate": float((target > 0).mean()),
    }


def calibration_table(
    predictions: pd.DataFrame,
    *,
    buckets: int,
) -> pd.DataFrame:
    """Bucket OOF scores and report realized target statistics."""
    if predictions.empty:
        return pd.DataFrame(
            columns=[
                "bucket",
                "count",
                "mean_prediction",
                "mean_target",
                "positive_rate",
                "min_prediction",
                "max_prediction",
            ]
        )
    clean = predictions.dropna(subset=["prediction", "target"]).copy()
    if clean.empty:
        return calibration_table(pd.DataFrame(), buckets=buckets)
    bucket_count = min(buckets, len(clean))
    clean["bucket"] = pd.qcut(
        clean["prediction"].rank(method="first"),
        q=bucket_count,
        labels=False,
        duplicates="drop",
    )
    table = (
        clean.groupby("bucket", observed=True)
        .agg(
            count=("target", "size"),
            mean_prediction=("prediction", "mean"),
            mean_target=("target", "mean"),
            positive_rate=("target", lambda values: float((values > 0).mean())),
            min_prediction=("prediction", "min"),
            max_prediction=("prediction", "max"),
        )
        .reset_index()
        .sort_values("bucket", ascending=False)
    )
    return table.reset_index(drop=True)


def feature_stability(feature_importance: pd.DataFrame) -> pd.DataFrame:
    """Summarize LightGBM feature importance stability across folds."""
    if feature_importance.empty:
        return pd.DataFrame(
            columns=[
                "feature",
                "mean_importance",
                "std_importance",
                "nonzero_fold_count",
                "fold_count",
                "stability_ratio",
            ]
        )
    grouped = (
        feature_importance.groupby("feature")
        .agg(
            mean_importance=("importance", "mean"),
            std_importance=("importance", "std"),
            nonzero_fold_count=("importance", lambda values: int((values > 0).sum())),
            fold_count=("importance", "size"),
        )
        .reset_index()
    )
    grouped["std_importance"] = grouped["std_importance"].fillna(0.0)
    grouped["stability_ratio"] = grouped.apply(
        lambda row: None
        if row["mean_importance"] == 0
        else float(row["std_importance"] / row["mean_importance"]),
        axis=1,
    )
    return grouped.sort_values(
        ["mean_importance", "nonzero_fold_count"],
        ascending=[False, False],
    ).reset_index(drop=True)


def comparison_table(
    *,
    dataset: pd.DataFrame,
    target_column: str,
    ridge_predictions: pd.DataFrame,
    lightgbm_predictions: pd.DataFrame,
    top_fraction: float,
) -> pd.DataFrame:
    """Compare rules, Ridge, and LightGBM without promoting scores to probabilities."""
    rows = [
        {
            "model": "rules_only",
            **rule_candidate_metrics(dataset, target_column=target_column),
            "ic": None,
            "rank_ic": None,
            "top_bucket_mean_target": None,
            "top_bucket_count": 0,
        },
        {
            "model": "ridge",
            **evaluate_predictions(ridge_predictions, top_fraction=top_fraction),
        },
        {
            "model": "lightgbm",
            **evaluate_predictions(lightgbm_predictions, top_fraction=top_fraction),
        },
    ]
    return pd.DataFrame(rows)


def lightgbm_promotion_status(comparison: pd.DataFrame) -> str:
    """Return a conservative gate status for daily-report eligibility."""
    indexed = comparison.set_index("model")
    lightgbm = indexed.loc["lightgbm"]
    ridge = indexed.loc["ridge"]
    rules = indexed.loc["rules_only"]
    if lightgbm["prediction_count"] == 0:
        return "NOT_ELIGIBLE_NO_OOF"
    if pd.isna(lightgbm["rank_ic"]) or pd.isna(ridge["rank_ic"]):
        return "NOT_ELIGIBLE_INSUFFICIENT_METRICS"
    beats_ridge = float(lightgbm["rank_ic"]) > float(ridge["rank_ic"])
    beats_rules = (
        not pd.isna(rules["mean_target"])
        and not pd.isna(lightgbm["top_bucket_mean_target"])
        and float(lightgbm["top_bucket_mean_target"]) > float(rules["mean_target"])
    )
    if beats_ridge and beats_rules:
        return "ELIGIBLE_FOR_REVIEW"
    return "NOT_ELIGIBLE_BASELINE_NOT_BEATEN"
