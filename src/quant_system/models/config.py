"""Strict configuration for model baselines."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from quant_system.models.datasets import CandidateDatasetConfig
from quant_system.models.lightgbm import LightGBMConfig
from quant_system.models.ridge import RidgeConfig
from quant_system.models.walk_forward import WalkForwardConfig


class ModelDatasetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    holding_sessions: tuple[int, ...] = (5,)
    feature_columns: tuple[str, ...]

    def to_config(self) -> CandidateDatasetConfig:
        return CandidateDatasetConfig(
            holding_sessions=self.holding_sessions,
            feature_columns=self.feature_columns,
        )


class WalkForwardSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    train_sessions: int = Field(gt=0)
    validation_sessions: int = Field(gt=0)
    step_sessions: int = Field(gt=0)
    hold_period: int = Field(gt=0)
    feature_lookback_days: int = Field(gt=0)
    minimum_train_rows: int = Field(default=30, gt=0)
    minimum_validation_rows: int = Field(default=5, gt=0)

    def to_config(self) -> WalkForwardConfig:
        return WalkForwardConfig(**self.model_dump())


class RidgeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alpha: float = Field(default=10, ge=0)
    top_fraction: float = Field(default=0.2, gt=0, le=1)

    def to_config(self, *, feature_columns: tuple[str, ...], target_column: str) -> RidgeConfig:
        return RidgeConfig(
            feature_columns=feature_columns,
            target_column=target_column,
            alpha=self.alpha,
            top_fraction=self.top_fraction,
        )


class RidgeBaselineSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: ModelDatasetSettings
    walk_forward: WalkForwardSettings
    ridge: RidgeSettings


class LightGBMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    n_estimators: int = Field(default=50, gt=0)
    learning_rate: float = Field(default=0.03, gt=0, le=1)
    num_leaves: int = Field(default=7, gt=1)
    max_depth: int = Field(default=3, gt=0)
    min_child_samples: int = Field(default=20, gt=0)
    subsample: float = Field(default=0.8, gt=0, le=1)
    colsample_bytree: float = Field(default=0.8, gt=0, le=1)
    reg_alpha: float = Field(default=0.0, ge=0)
    reg_lambda: float = Field(default=10.0, ge=0)
    random_state: int = 42
    n_jobs: int = 1
    top_fraction: float = Field(default=0.2, gt=0, le=1)
    minimum_train_rows: int = Field(default=30, gt=0)

    def to_config(self, *, feature_columns: tuple[str, ...], target_column: str) -> LightGBMConfig:
        return LightGBMConfig(
            feature_columns=feature_columns,
            target_column=target_column,
            **self.model_dump(),
        )


class CalibrationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    buckets: int = Field(default=5, ge=2, le=20)


class RankingBaselineSettings(RidgeBaselineSettings):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lightgbm: LightGBMSettings
    calibration: CalibrationSettings = CalibrationSettings()


def load_ridge_baseline_settings(path: Path) -> RidgeBaselineSettings:
    """Load a strict Ridge baseline config."""
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Ridge baseline config must be a mapping: {path}")
    settings = RidgeBaselineSettings.model_validate(payload)
    target_horizon = settings.walk_forward.hold_period
    if target_horizon not in settings.dataset.holding_sessions:
        raise ValueError("walk_forward.hold_period must be present in dataset.holding_sessions")
    return settings


def load_ranking_baseline_settings(path: Path) -> RankingBaselineSettings:
    """Load strict Ridge + LightGBM ranking baseline config."""
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Ranking baseline config must be a mapping: {path}")
    settings = RankingBaselineSettings.model_validate(payload)
    target_horizon = settings.walk_forward.hold_period
    if target_horizon not in settings.dataset.holding_sessions:
        raise ValueError("walk_forward.hold_period must be present in dataset.holding_sessions")
    return settings
