"""Conservative LightGBM baseline for candidate ranking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import pandas as pd

from quant_system.models.ridge import evaluate_predictions
from quant_system.models.walk_forward import WalkForwardFold


class SupportsLightGBMRegressor(Protocol):
    """Small protocol for the LightGBM estimator API used here."""

    feature_importances_: Any

    def fit(self, features: pd.DataFrame, target: pd.Series) -> SupportsLightGBMRegressor: ...

    def predict(self, features: pd.DataFrame) -> Any: ...


@dataclass(frozen=True)
class LightGBMConfig:
    """Conservative LightGBM settings for OOF ranking only."""

    feature_columns: tuple[str, ...]
    target_column: str
    n_estimators: int = 50
    learning_rate: float = 0.03
    num_leaves: int = 7
    max_depth: int = 3
    min_child_samples: int = 20
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    reg_alpha: float = 0.0
    reg_lambda: float = 10.0
    random_state: int = 42
    n_jobs: int = 1
    top_fraction: float = 0.20
    minimum_train_rows: int = 30

    def __post_init__(self) -> None:
        if not self.feature_columns:
            raise ValueError("at least one feature column is required")
        if min(
            self.n_estimators,
            self.num_leaves,
            self.max_depth,
            self.min_child_samples,
            self.minimum_train_rows,
        ) < 1:
            raise ValueError("LightGBM count settings must be positive")
        if not 0 < self.learning_rate <= 1:
            raise ValueError("learning_rate must be in (0, 1]")
        if not 0 < self.subsample <= 1:
            raise ValueError("subsample must be in (0, 1]")
        if not 0 < self.colsample_bytree <= 1:
            raise ValueError("colsample_bytree must be in (0, 1]")
        if self.reg_alpha < 0 or self.reg_lambda < 0:
            raise ValueError("regularization must be non-negative")
        if not 0 < self.top_fraction <= 1:
            raise ValueError("top_fraction must be in (0, 1]")


class LightGBMRanker:
    """Lazy LightGBM wrapper; imported only when a fold is actually trained."""

    def __init__(
        self,
        config: LightGBMConfig,
        *,
        estimator_factory: Any | None = None,
    ) -> None:
        self.config = config
        self.estimator_factory = estimator_factory
        self.model_: SupportsLightGBMRegressor | None = None
        self.feature_importances_: pd.Series | None = None

    def fit(self, frame: pd.DataFrame) -> LightGBMRanker:
        """Fit a conservative regressor on complete feature and target rows."""
        clean = frame.dropna(subset=[*self.config.feature_columns, self.config.target_column])
        if len(clean) < self.config.minimum_train_rows:
            raise ValueError("LightGBM training frame has insufficient complete rows")
        features = clean.loc[:, self.config.feature_columns].astype(float)
        target = clean[self.config.target_column].astype(float)
        estimator = self._make_estimator()
        estimator.fit(features, target)
        self.model_ = estimator
        self.feature_importances_ = pd.Series(
            estimator.feature_importances_,
            index=self.config.feature_columns,
            dtype=float,
            name="importance",
        )
        return self

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        """Predict target scores; incomplete feature rows return NaN."""
        if self.model_ is None:
            raise RuntimeError("LightGBMRanker must be fit before predict")
        predictions = pd.Series(pd.NA, index=frame.index, dtype="Float64")
        complete = frame.dropna(subset=list(self.config.feature_columns))
        if complete.empty:
            return predictions
        features = complete.loc[:, self.config.feature_columns].astype(float)
        predictions.loc[complete.index] = self.model_.predict(features)
        return predictions.astype(float)

    def _make_estimator(self) -> SupportsLightGBMRegressor:
        if self.estimator_factory is not None:
            return self.estimator_factory()
        try:
            import lightgbm as lgb
        except ImportError as exc:
            raise RuntimeError("LightGBM is not installed") from exc
        return _NativeLightGBMRegressor(lgb, self.config)


class _NativeLightGBMRegressor:
    """Tiny native LightGBM adapter that avoids a scikit-learn dependency."""

    def __init__(self, lightgbm_module: Any, config: LightGBMConfig) -> None:
        self.lightgbm = lightgbm_module
        self.config = config
        self.booster_: Any | None = None
        self.feature_importances_: np.ndarray | None = None

    def fit(self, features: pd.DataFrame, target: pd.Series) -> _NativeLightGBMRegressor:
        dataset = self.lightgbm.Dataset(features, label=target, free_raw_data=True)
        params = {
            "objective": "regression",
            "boosting_type": "gbdt",
            "learning_rate": self.config.learning_rate,
            "num_leaves": self.config.num_leaves,
            "max_depth": self.config.max_depth,
            "min_child_samples": self.config.min_child_samples,
            "bagging_fraction": self.config.subsample,
            "bagging_freq": 1 if self.config.subsample < 1 else 0,
            "feature_fraction": self.config.colsample_bytree,
            "lambda_l1": self.config.reg_alpha,
            "lambda_l2": self.config.reg_lambda,
            "seed": self.config.random_state,
            "feature_fraction_seed": self.config.random_state,
            "bagging_seed": self.config.random_state,
            "num_threads": self.config.n_jobs,
            "verbosity": -1,
            "force_col_wise": True,
        }
        self.booster_ = self.lightgbm.train(
            params,
            dataset,
            num_boost_round=self.config.n_estimators,
        )
        self.feature_importances_ = self.booster_.feature_importance(importance_type="split")
        return self

    def predict(self, features: pd.DataFrame) -> Any:
        if self.booster_ is None:
            raise RuntimeError("native LightGBM estimator must be fit before predict")
        return self.booster_.predict(features)


@dataclass(frozen=True)
class LightGBMWalkForwardResult:
    """OOF predictions, fold metrics, and feature importances."""

    predictions: pd.DataFrame
    fold_metrics: pd.DataFrame
    feature_importance: pd.DataFrame
    summary: dict[str, Any]


def run_lightgbm_walk_forward(
    dataset: pd.DataFrame,
    folds: tuple[WalkForwardFold, ...],
    config: LightGBMConfig,
    *,
    estimator_factory: Any | None = None,
) -> LightGBMWalkForwardResult:
    """Fit LightGBM on READY folds and return OOF artifacts."""
    predictions = []
    fold_metric_rows = []
    importance_rows = []
    ready_count = 0
    skipped_count = 0
    unavailable = False
    for fold in folds:
        if fold.status != "READY":
            skipped_count += 1
            fold_metric_rows.append(_fold_status_row(fold, status=fold.status))
            continue
        train = dataset.loc[list(fold.train_indices)].copy()
        validation = dataset.loc[list(fold.validation_indices)].copy()
        try:
            model = LightGBMRanker(config, estimator_factory=estimator_factory).fit(train)
        except RuntimeError as exc:
            if "LightGBM is not installed" not in str(exc):
                raise
            unavailable = True
            skipped_count += 1
            fold_metric_rows.append(_fold_status_row(fold, status="LIGHTGBM_UNAVAILABLE"))
            continue
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
        if model.feature_importances_ is not None:
            for feature, importance in model.feature_importances_.items():
                importance_rows.append(
                    {
                        "fold_number": fold.fold_number,
                        "feature": feature,
                        "importance": float(importance),
                    }
                )
        fold_metric_rows.append(
            {
                **_fold_status_row(fold, status="READY"),
                **evaluate_predictions(prediction_frame, top_fraction=config.top_fraction),
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
    feature_importance = (
        pd.DataFrame(importance_rows)
        if importance_rows
        else pd.DataFrame(columns=["fold_number", "feature", "importance"])
    )
    summary = {
        "ready_fold_count": ready_count,
        "skipped_fold_count": skipped_count,
        "fold_count": len(folds),
        "lightgbm_available": not unavailable,
        **evaluate_predictions(prediction_output, top_fraction=config.top_fraction),
    }
    return LightGBMWalkForwardResult(
        predictions=prediction_output,
        fold_metrics=fold_metrics,
        feature_importance=feature_importance,
        summary=summary,
    )


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
