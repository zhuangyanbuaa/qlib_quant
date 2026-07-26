"""Persist reproducible model baseline artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pandas as pd

from quant_system.models.comparison import (
    calibration_table,
    comparison_table,
    feature_stability,
    lightgbm_promotion_status,
)
from quant_system.models.config import RankingBaselineSettings, RidgeBaselineSettings
from quant_system.models.lightgbm import LightGBMWalkForwardResult
from quant_system.models.ridge import RidgeWalkForwardResult
from quant_system.strategy.config import BuyTheDipConfig


@dataclass(frozen=True)
class ModelArtifacts:
    """Files emitted by a model validation run."""

    directory: Path
    summary: Path
    candidate_dataset: Path
    oof_predictions: Path
    fold_metrics: Path


@dataclass(frozen=True)
class RankingModelArtifacts:
    """Files emitted by a Ridge + LightGBM comparison run."""

    directory: Path
    summary: Path
    candidate_dataset: Path
    ridge_oof_predictions: Path
    lightgbm_oof_predictions: Path
    ridge_fold_metrics: Path
    lightgbm_fold_metrics: Path
    lightgbm_feature_importance: Path
    feature_stability: Path
    ridge_calibration: Path
    lightgbm_calibration: Path
    comparison: Path


def write_ridge_baseline_report(
    *,
    result: RidgeWalkForwardResult,
    dataset: pd.DataFrame,
    strategy_config: BuyTheDipConfig,
    model_settings: RidgeBaselineSettings,
    run_id: UUID,
    start: date,
    end: date,
    symbols: tuple[str, ...],
    report_root: Path,
) -> ModelArtifacts:
    """Write OOF predictions, fold metrics, and summary metadata."""
    directory = report_root / str(run_id)
    directory.mkdir(parents=True, exist_ok=False)

    candidate_path = directory / "candidate_dataset.csv"
    predictions_path = directory / "oof_predictions.csv"
    fold_metrics_path = directory / "fold_metrics.csv"
    summary_path = directory / "summary.json"

    dataset.to_csv(candidate_path, index=False)
    result.predictions.to_csv(predictions_path, index=False)
    result.fold_metrics.to_csv(fold_metrics_path, index=False)

    model_payload = model_settings.model_dump(mode="json")
    strategy_payload = strategy_config.model_dump(mode="json")
    config_hash = sha256(
        json.dumps(
            {
                "model": model_payload,
                "strategy": strategy_payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    summary = {
        "run_id": str(run_id),
        "model": "ridge_baseline_v1",
        "status": "COMPLETED"
        if result.summary.get("ready_fold_count", 0) > 0
        else "INSUFFICIENT_SAMPLE",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": list(symbols),
        "symbol_count": len(symbols),
        "candidate_count": len(dataset),
        "fold_count": int(result.summary.get("fold_count", 0)),
        "ready_fold_count": int(result.summary.get("ready_fold_count", 0)),
        "skipped_fold_count": int(result.summary.get("skipped_fold_count", 0)),
        "metrics": _json_safe(result.summary),
        "config": {
            "model": model_payload,
            "strategy": strategy_payload,
        },
        "config_hash": config_hash,
        "artifacts": {
            "candidate_dataset": str(candidate_path),
            "oof_predictions": str(predictions_path),
            "fold_metrics": str(fold_metrics_path),
        },
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return ModelArtifacts(
        directory=directory,
        summary=summary_path,
        candidate_dataset=candidate_path,
        oof_predictions=predictions_path,
        fold_metrics=fold_metrics_path,
    )


def write_ranking_baseline_report(
    *,
    ridge_result: RidgeWalkForwardResult,
    lightgbm_result: LightGBMWalkForwardResult,
    dataset: pd.DataFrame,
    strategy_config: BuyTheDipConfig,
    model_settings: RankingBaselineSettings,
    target_column: str,
    run_id: UUID,
    start: date,
    end: date,
    symbols: tuple[str, ...],
    report_root: Path,
) -> RankingModelArtifacts:
    """Write Ridge, LightGBM, calibration, feature stability, and comparison artifacts."""
    directory = report_root / str(run_id)
    directory.mkdir(parents=True, exist_ok=False)

    candidate_path = directory / "candidate_dataset.csv"
    ridge_predictions_path = directory / "ridge_oof_predictions.csv"
    lightgbm_predictions_path = directory / "lightgbm_oof_predictions.csv"
    ridge_fold_metrics_path = directory / "ridge_fold_metrics.csv"
    lightgbm_fold_metrics_path = directory / "lightgbm_fold_metrics.csv"
    lightgbm_importance_path = directory / "lightgbm_feature_importance.csv"
    feature_stability_path = directory / "feature_stability.csv"
    ridge_calibration_path = directory / "ridge_calibration.csv"
    lightgbm_calibration_path = directory / "lightgbm_calibration.csv"
    comparison_path = directory / "comparison.csv"
    summary_path = directory / "summary.json"

    stability = feature_stability(lightgbm_result.feature_importance)
    ridge_calibration = calibration_table(
        ridge_result.predictions,
        buckets=model_settings.calibration.buckets,
    )
    lightgbm_calibration = calibration_table(
        lightgbm_result.predictions,
        buckets=model_settings.calibration.buckets,
    )
    comparison = comparison_table(
        dataset=dataset,
        target_column=target_column,
        ridge_predictions=ridge_result.predictions,
        lightgbm_predictions=lightgbm_result.predictions,
        top_fraction=model_settings.lightgbm.top_fraction,
    )
    promotion_status = lightgbm_promotion_status(comparison)

    dataset.to_csv(candidate_path, index=False)
    ridge_result.predictions.to_csv(ridge_predictions_path, index=False)
    lightgbm_result.predictions.to_csv(lightgbm_predictions_path, index=False)
    ridge_result.fold_metrics.to_csv(ridge_fold_metrics_path, index=False)
    lightgbm_result.fold_metrics.to_csv(lightgbm_fold_metrics_path, index=False)
    lightgbm_result.feature_importance.to_csv(lightgbm_importance_path, index=False)
    stability.to_csv(feature_stability_path, index=False)
    ridge_calibration.to_csv(ridge_calibration_path, index=False)
    lightgbm_calibration.to_csv(lightgbm_calibration_path, index=False)
    comparison.to_csv(comparison_path, index=False)

    model_payload = model_settings.model_dump(mode="json")
    strategy_payload = strategy_config.model_dump(mode="json")
    config_hash = sha256(
        json.dumps(
            {
                "model": model_payload,
                "strategy": strategy_payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    status = (
        "COMPLETED"
        if ridge_result.summary.get("ready_fold_count", 0) > 0
        or lightgbm_result.summary.get("ready_fold_count", 0) > 0
        else "INSUFFICIENT_SAMPLE"
    )
    summary = {
        "run_id": str(run_id),
        "model": "ranking_baseline_v1",
        "status": status,
        "promotion_status": promotion_status,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": list(symbols),
        "symbol_count": len(symbols),
        "candidate_count": len(dataset),
        "target_column": target_column,
        "ridge": _json_safe(ridge_result.summary),
        "lightgbm": _json_safe(lightgbm_result.summary),
        "config": {
            "model": model_payload,
            "strategy": strategy_payload,
        },
        "config_hash": config_hash,
        "artifacts": {
            "candidate_dataset": str(candidate_path),
            "ridge_oof_predictions": str(ridge_predictions_path),
            "lightgbm_oof_predictions": str(lightgbm_predictions_path),
            "ridge_fold_metrics": str(ridge_fold_metrics_path),
            "lightgbm_fold_metrics": str(lightgbm_fold_metrics_path),
            "lightgbm_feature_importance": str(lightgbm_importance_path),
            "feature_stability": str(feature_stability_path),
            "ridge_calibration": str(ridge_calibration_path),
            "lightgbm_calibration": str(lightgbm_calibration_path),
            "comparison": str(comparison_path),
        },
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return RankingModelArtifacts(
        directory=directory,
        summary=summary_path,
        candidate_dataset=candidate_path,
        ridge_oof_predictions=ridge_predictions_path,
        lightgbm_oof_predictions=lightgbm_predictions_path,
        ridge_fold_metrics=ridge_fold_metrics_path,
        lightgbm_fold_metrics=lightgbm_fold_metrics_path,
        lightgbm_feature_importance=lightgbm_importance_path,
        feature_stability=feature_stability_path,
        ridge_calibration=ridge_calibration_path,
        lightgbm_calibration=lightgbm_calibration_path,
        comparison=comparison_path,
    )


def _json_safe(payload: dict[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, value in payload.items():
        if isinstance(value, float) and (
            pd.isna(value) or value in (float("inf"), -float("inf"))
        ):
            safe[key] = None
        else:
            safe[key] = value
    return safe
