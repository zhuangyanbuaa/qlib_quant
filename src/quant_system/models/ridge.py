"""Conservative Ridge baseline for candidate ranking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from quant_system.models.walk_forward import WalkForwardFold


@dataclass(frozen=True)
class RidgeConfig:
    """Ridge baseline settings."""

    feature_columns: tuple[str, ...]
    target_column: str
    alpha: float = 10.0
    top_fraction: float = 0.20

    def __post_init__(self) -> None:
        if not self.feature_columns:
            raise ValueError("at least one feature column is required")
        if self.alpha < 0:
            raise ValueError("alpha must be non-negative")
        if not 0 < self.top_fraction <= 1:
            raise ValueError("top_fraction must be in (0, 1]")


class RidgeRanker:
    """Small deterministic Ridge implementation for OOF sanity checks."""

    def __init__(self, config: RidgeConfig) -> None:
        self.config = config
        self.feature_mean_: pd.Series | None = None
        self.feature_std_: pd.Series | None = None
        self.coefficients_: np.ndarray | None = None

    def fit(self, frame: pd.DataFrame) -> RidgeRanker:
        """Fit on rows with complete feature and target values."""
        clean = frame.dropna(subset=[*self.config.feature_columns, self.config.target_column])
        if clean.empty:
            raise ValueError("Ridge training frame has no complete rows")
        features = clean.loc[:, self.config.feature_columns].astype(float)
        target = clean[self.config.target_column].astype(float).to_numpy()
        self.feature_mean_ = features.mean()
        std = features.std(ddof=0).replace(0, 1.0)
        self.feature_std_ = std
        standardized = (features - self.feature_mean_) / self.feature_std_
        design = np.column_stack([np.ones(len(standardized)), standardized.to_numpy()])
        penalty = np.eye(design.shape[1]) * self.config.alpha
        penalty[0, 0] = 0.0
        lhs = design.T @ design + penalty
        rhs = design.T @ target
        try:
            self.coefficients_ = np.linalg.solve(lhs, rhs)
        except np.linalg.LinAlgError:
            self.coefficients_ = np.linalg.pinv(lhs) @ rhs
        return self

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        """Predict target scores; incomplete feature rows return NaN."""
        if self.feature_mean_ is None or self.feature_std_ is None or self.coefficients_ is None:
            raise RuntimeError("RidgeRanker must be fit before predict")
        predictions = pd.Series(np.nan, index=frame.index, dtype=float)
        complete = frame.dropna(subset=list(self.config.feature_columns))
        if complete.empty:
            return predictions
        features = complete.loc[:, self.config.feature_columns].astype(float)
        standardized = (features - self.feature_mean_) / self.feature_std_
        design = np.column_stack([np.ones(len(standardized)), standardized.to_numpy()])
        predictions.loc[complete.index] = design @ self.coefficients_
        return predictions


@dataclass(frozen=True)
class RidgeWalkForwardResult:
    """OOF predictions and metrics from a Ridge walk-forward run."""

    predictions: pd.DataFrame
    fold_metrics: pd.DataFrame
    summary: dict[str, Any]


def run_ridge_walk_forward(
    dataset: pd.DataFrame,
    folds: tuple[WalkForwardFold, ...],
    config: RidgeConfig,
) -> RidgeWalkForwardResult:
    """Fit Ridge on each READY fold and return OOF predictions."""
    predictions = []
    fold_metric_rows = []
    ready_count = 0
    skipped_count = 0
    for fold in folds:
        if fold.status != "READY":
            skipped_count += 1
            fold_metric_rows.append(_fold_status_row(fold, status=fold.status))
            continue
        train = dataset.loc[list(fold.train_indices)].copy()
        validation = dataset.loc[list(fold.validation_indices)].copy()
        try:
            model = RidgeRanker(config).fit(train)
        except ValueError:
            skipped_count += 1
            fold_metric_rows.append(_fold_status_row(fold, status="INSUFFICIENT_SAMPLE"))
            continue
        validation_predictions = model.predict(validation)
        prediction_frame = pd.DataFrame(
            {
                "signal_id": validation["signal_id"].astype(str).to_numpy(),
                "symbol": validation["symbol"].astype(str).to_numpy(),
                "signal_session": validation["signal_session"].to_numpy(),
                "fold_number": fold.fold_number,
                "prediction": validation_predictions.to_numpy(),
                "target": validation[config.target_column].astype(float).to_numpy(),
            },
            index=validation.index,
        ).dropna(subset=["prediction", "target"])
        predictions.append(prediction_frame)
        metrics = evaluate_predictions(prediction_frame, top_fraction=config.top_fraction)
        fold_metric_rows.append(
            {
                **_fold_status_row(fold, status="READY"),
                **metrics,
            }
        )
        ready_count += 1

    prediction_output = (
        pd.concat(predictions, ignore_index=True)
        if predictions
        else pd.DataFrame(
            columns=["signal_id", "symbol", "signal_session", "fold_number", "prediction", "target"]
        )
    )
    fold_metrics = pd.DataFrame(fold_metric_rows)
    summary = {
        "ready_fold_count": ready_count,
        "skipped_fold_count": skipped_count,
        "fold_count": len(folds),
        **evaluate_predictions(prediction_output, top_fraction=config.top_fraction),
    }
    return RidgeWalkForwardResult(
        predictions=prediction_output,
        fold_metrics=fold_metrics,
        summary=summary,
    )


def evaluate_predictions(
    frame: pd.DataFrame,
    *,
    top_fraction: float = 0.20,
) -> dict[str, Any]:
    """Evaluate ranking quality without presenting scores as probabilities."""
    if frame.empty:
        return {
            "prediction_count": 0,
            "ic": None,
            "rank_ic": None,
            "mean_target": None,
            "top_bucket_mean_target": None,
            "top_bucket_count": 0,
        }
    clean = frame.dropna(subset=["prediction", "target"]).copy()
    if clean.empty:
        return {
            "prediction_count": 0,
            "ic": None,
            "rank_ic": None,
            "mean_target": None,
            "top_bucket_mean_target": None,
            "top_bucket_count": 0,
        }
    top_count = max(1, int(np.ceil(len(clean) * top_fraction)))
    top = clean.sort_values("prediction", ascending=False).head(top_count)
    return {
        "prediction_count": len(clean),
        "ic": _safe_corr(clean["prediction"], clean["target"]),
        "rank_ic": _safe_corr(clean["prediction"].rank(), clean["target"].rank()),
        "mean_target": float(clean["target"].mean()),
        "top_bucket_mean_target": float(top["target"].mean()),
        "top_bucket_count": len(top),
    }


def _safe_corr(left: pd.Series, right: pd.Series) -> float | None:
    if len(left) < 2 or left.nunique(dropna=True) < 2 or right.nunique(dropna=True) < 2:
        return None
    value = left.corr(right)
    return None if pd.isna(value) else float(value)


def _fold_status_row(fold: WalkForwardFold, *, status: str) -> dict[str, Any]:
    return {
        "fold_number": fold.fold_number,
        "status": status,
        "train_rows": len(fold.train_indices),
        "validation_rows": len(fold.validation_indices),
        "train_start": fold.train_start.isoformat(),
        "train_end": fold.train_end.isoformat(),
        "validation_start": fold.validation_start.isoformat(),
        "validation_end": fold.validation_end.isoformat(),
        "embargo_cutoff": fold.embargo_cutoff.isoformat(),
    }
