"""Daily LightGBM ranking context for the premarket workbench."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from quant_system.models.config import RankingBaselineSettings
from quant_system.models.datasets import build_candidate_dataset
from quant_system.models.lightgbm import LightGBMRanker, SupportsLightGBMRegressor
from quant_system.strategy.config import BuyTheDipConfig


@dataclass(frozen=True)
class DailyModelRanking:
    """Read-only model context for one signal session."""

    context: dict[str, Any]
    scores_by_signal_id: dict[str, dict[str, Any]]


def rank_daily_candidates_with_lightgbm(
    *,
    features: pd.DataFrame,
    candidate_rows: list[dict[str, Any]],
    strategy_config: BuyTheDipConfig,
    model_settings: RankingBaselineSettings,
    as_of: date,
    estimator_factory: Callable[[], SupportsLightGBMRegressor] | None = None,
) -> DailyModelRanking:
    """Train on mature historical candidates and score today's rows.

    This is deliberately a context ranker, not a gate. Training rows must have
    labels whose end session is at or before ``as_of``; current-session rows are
    scored only after passing the existing rules/calibration pipeline.
    """
    target_horizon = model_settings.walk_forward.hold_period
    target_column = f"relative_return_{target_horizon}"
    label_end_column = f"label_end_session_{target_horizon}"
    feature_columns = model_settings.dataset.feature_columns
    base_context: dict[str, Any] = {
        "model": "lightgbm_daily_context_v1",
        "decision_scope": "RANK_CONTEXT_ONLY_DOES_NOT_CHANGE_ACTIONS",
        "target_column": target_column,
        "feature_columns": list(feature_columns),
        "status": "UNAVAILABLE",
        "training_rows": 0,
        "scored_rows": 0,
    }
    if not candidate_rows:
        return DailyModelRanking(
            context={**base_context, "status": "SKIPPED_NO_CANDIDATES"},
            scores_by_signal_id={},
        )

    dataset = build_candidate_dataset(
        features,
        strategy_config,
        dataset_config=model_settings.dataset.to_config(),
    )
    if dataset.empty or label_end_column not in dataset.columns:
        return DailyModelRanking(
            context={**base_context, "status": "INSUFFICIENT_TRAINING_ROWS"},
            scores_by_signal_id={},
        )

    training_frame = _mature_training_frame(
        dataset,
        as_of=as_of,
        label_end_column=label_end_column,
        target_column=target_column,
        feature_columns=feature_columns,
    )
    training_rows = len(training_frame)
    minimum_train_rows = model_settings.lightgbm.minimum_train_rows
    if training_rows < minimum_train_rows:
        return DailyModelRanking(
            context={
                **base_context,
                "status": "INSUFFICIENT_TRAINING_ROWS",
                "training_rows": training_rows,
                "minimum_train_rows": minimum_train_rows,
            },
            scores_by_signal_id={},
        )

    scoring_frame = _current_scoring_frame(
        features,
        candidate_rows=candidate_rows,
        as_of=as_of,
        feature_columns=feature_columns,
    )
    if scoring_frame.empty:
        return DailyModelRanking(
            context={
                **base_context,
                "status": "SKIPPED_NO_SCORABLE_ROWS",
                "training_rows": training_rows,
            },
            scores_by_signal_id={},
        )

    ranker = LightGBMRanker(
        model_settings.lightgbm.to_config(
            feature_columns=feature_columns,
            target_column=target_column,
        ),
        estimator_factory=estimator_factory,
    )
    try:
        ranker.fit(training_frame)
        predictions = ranker.predict(scoring_frame)
    except RuntimeError as exc:
        if "LightGBM is not installed" not in str(exc):
            raise
        return DailyModelRanking(
            context={
                **base_context,
                "status": "UNAVAILABLE_LIGHTGBM_NOT_INSTALLED",
                "training_rows": training_rows,
                "reason": str(exc),
            },
            scores_by_signal_id={},
        )

    scored = scoring_frame.loc[predictions.notna(), ["signal_id", "symbol"]].copy()
    scored["model_score"] = predictions.loc[predictions.notna()].astype(float)
    scored["model_rank"] = (
        scored["model_score"].rank(method="first", ascending=False).astype(int)
    )
    scored = scored.sort_values(["model_rank", "symbol"])
    scores_by_signal_id = {
        str(row.signal_id): {
            "model_score": float(row.model_score),
            "model_rank": int(row.model_rank),
            "model_rank_status": "SCORED",
        }
        for row in scored.itertuples(index=False)
    }
    return DailyModelRanking(
        context={
            **base_context,
            "status": "SCORED",
            "training_rows": training_rows,
            "minimum_train_rows": minimum_train_rows,
            "scored_rows": len(scores_by_signal_id),
        },
        scores_by_signal_id=scores_by_signal_id,
    )


def attach_model_scores(
    rows: list[dict[str, Any]],
    ranking: DailyModelRanking,
) -> None:
    """Attach rank context to rows in-place without changing actions."""
    for row in rows:
        score = ranking.scores_by_signal_id.get(str(row.get("signal_id")))
        if score is None:
            row["model_score"] = None
            row["model_rank"] = None
            row["model_rank_status"] = (
                "NOT_SCORED"
                if ranking.context["status"] == "SCORED"
                else ranking.context["status"]
            )
            continue
        row.update(score)


def _mature_training_frame(
    dataset: pd.DataFrame,
    *,
    as_of: date,
    label_end_column: str,
    target_column: str,
    feature_columns: tuple[str, ...],
) -> pd.DataFrame:
    frame = dataset.copy()
    frame["signal_session"] = pd.to_datetime(frame["signal_session"]).dt.date
    frame[label_end_column] = pd.to_datetime(frame[label_end_column]).dt.date
    return frame.loc[
        (frame["signal_session"] < as_of)
        & (frame[label_end_column] <= as_of)
    ].dropna(subset=[target_column, *feature_columns])


def _current_scoring_frame(
    features: pd.DataFrame,
    *,
    candidate_rows: list[dict[str, Any]],
    as_of: date,
    feature_columns: tuple[str, ...],
) -> pd.DataFrame:
    if not candidate_rows:
        return pd.DataFrame()
    candidates = pd.DataFrame(candidate_rows)
    if candidates.empty:
        return pd.DataFrame()
    candidates["symbol"] = candidates["symbol"].astype("string").str.upper()
    feature_slice = features.copy()
    feature_slice["symbol"] = feature_slice["symbol"].astype("string").str.upper()
    feature_slice["session_date_ny"] = pd.to_datetime(
        feature_slice["session_date_ny"]
    ).dt.date
    feature_slice = feature_slice.loc[
        feature_slice["session_date_ny"] == as_of,
        ["symbol", "session_date_ny", *feature_columns],
    ]
    scoring_frame = candidates[["signal_id", "symbol"]].merge(
        feature_slice,
        how="left",
        on="symbol",
        validate="many_to_one",
    )
    return scoring_frame.dropna(subset=list(feature_columns)).reset_index(drop=True)
