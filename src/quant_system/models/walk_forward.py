"""Purged walk-forward splits for candidate-level model validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

import pandas as pd

FoldStatus = Literal["READY", "INSUFFICIENT_SAMPLE"]


@dataclass(frozen=True)
class WalkForwardConfig:
    """Window and embargo settings for model validation."""

    train_sessions: int
    validation_sessions: int
    step_sessions: int
    hold_period: int
    feature_lookback_days: int
    label_end_column: str | None = None
    minimum_train_rows: int = 30
    minimum_validation_rows: int = 5

    def __post_init__(self) -> None:
        if min(
            self.train_sessions,
            self.validation_sessions,
            self.step_sessions,
            self.hold_period,
            self.feature_lookback_days,
            self.minimum_train_rows,
            self.minimum_validation_rows,
        ) < 1:
            raise ValueError("walk-forward counts must be positive")

    @property
    def effective_label_end_column(self) -> str:
        return self.label_end_column or f"label_end_session_{self.hold_period}"

    @property
    def embargo_days(self) -> int:
        return self.hold_period + self.feature_lookback_days


@dataclass(frozen=True)
class WalkForwardFold:
    """One purged train/validation split."""

    fold_number: int
    train_indices: tuple[int, ...]
    validation_indices: tuple[int, ...]
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    embargo_cutoff: date
    status: FoldStatus


def build_purged_walk_forward_splits(
    dataset: pd.DataFrame,
    config: WalkForwardConfig,
) -> tuple[WalkForwardFold, ...]:
    """Create walk-forward folds with label-end embargo checks."""
    required = {"signal_session", config.effective_label_end_column}
    missing = required - set(dataset.columns)
    if missing:
        raise ValueError(f"dataset missing walk-forward columns: {sorted(missing)}")
    if dataset.empty:
        return ()

    frame = dataset.copy()
    frame["signal_session"] = pd.to_datetime(frame["signal_session"]).dt.date
    frame[config.effective_label_end_column] = pd.to_datetime(
        frame[config.effective_label_end_column],
        errors="coerce",
    ).dt.date
    sessions = tuple(sorted(frame["signal_session"].dropna().unique()))
    folds: list[WalkForwardFold] = []
    fold_number = 0
    max_start = len(sessions) - config.train_sessions - config.validation_sessions
    for start_offset in range(0, max_start + 1, config.step_sessions):
        train_sessions = sessions[start_offset : start_offset + config.train_sessions]
        validation_sessions = sessions[
            start_offset
            + config.train_sessions : start_offset
            + config.train_sessions
            + config.validation_sessions
        ]
        if not train_sessions or not validation_sessions:
            continue
        validation_start = validation_sessions[0]
        validation_end = validation_sessions[-1]
        embargo_cutoff = validation_start - pd.Timedelta(days=config.embargo_days)
        train_mask = (
            frame["signal_session"].isin(train_sessions)
            & frame[config.effective_label_end_column].notna()
            & (frame[config.effective_label_end_column] <= embargo_cutoff)
        )
        validation_mask = (
            frame["signal_session"].isin(validation_sessions)
            & frame[config.effective_label_end_column].notna()
        )
        train_indices = tuple(int(index) for index in frame.index[train_mask])
        validation_indices = tuple(int(index) for index in frame.index[validation_mask])
        status: FoldStatus = (
            "READY"
            if len(train_indices) >= config.minimum_train_rows
            and len(validation_indices) >= config.minimum_validation_rows
            else "INSUFFICIENT_SAMPLE"
        )
        folds.append(
            WalkForwardFold(
                fold_number=fold_number,
                train_indices=train_indices,
                validation_indices=validation_indices,
                train_start=train_sessions[0],
                train_end=train_sessions[-1],
                validation_start=validation_start,
                validation_end=validation_end,
                embargo_cutoff=embargo_cutoff,
                status=status,
            )
        )
        fold_number += 1
    return tuple(folds)
