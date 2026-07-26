"""Strict configuration for model baselines."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from quant_system.models.datasets import CandidateDatasetConfig
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
