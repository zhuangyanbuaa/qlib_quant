"""Persist reproducible model baseline artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pandas as pd

from quant_system.models.config import RidgeBaselineSettings
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
