"""High-level model workflows used by the CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pandas as pd

from quant_system.backtest.workflow import load_feature_history
from quant_system.models.config import RankingBaselineSettings, RidgeBaselineSettings
from quant_system.models.datasets import build_candidate_dataset
from quant_system.models.lightgbm import run_lightgbm_walk_forward
from quant_system.models.reports import (
    ModelArtifacts,
    RankingModelArtifacts,
    write_ranking_baseline_report,
    write_ridge_baseline_report,
)
from quant_system.models.ridge import run_ridge_walk_forward
from quant_system.models.walk_forward import build_purged_walk_forward_splits
from quant_system.storage.parquet import ParquetRepository
from quant_system.storage.sqlite import OperationsRegistry
from quant_system.strategy.config import BuyTheDipConfig


@dataclass(frozen=True)
class RidgeBaselineWorkflowResult:
    """Summary and artifact locations from a Ridge baseline workflow."""

    summary: dict[str, object]
    artifacts: ModelArtifacts


@dataclass(frozen=True)
class RankingBaselineWorkflowResult:
    """Summary and artifact locations from a Ridge + LightGBM workflow."""

    summary: dict[str, object]
    artifacts: RankingModelArtifacts


def run_ridge_baseline_workflow(
    *,
    repository: ParquetRepository,
    database_path: Path,
    report_root: Path,
    symbols: tuple[str, ...],
    start: date,
    end: date,
    strategy_config: BuyTheDipConfig,
    model_settings: RidgeBaselineSettings,
    run_id: UUID | None = None,
) -> RidgeBaselineWorkflowResult:
    """Build candidates, run purged Ridge OOF validation, and persist artifacts."""
    if start > end:
        raise ValueError("model start must not follow end")
    run_id = run_id or uuid4()
    requested = tuple(sorted({symbol.upper() for symbol in symbols}))
    benchmark_symbol = strategy_config.strategy.benchmark_symbol.upper()
    features = load_feature_history(
        repository=repository,
        database_path=database_path,
        symbols=requested,
        benchmark_symbol=benchmark_symbol,
        start=start,
        end=end,
    )
    dataset = build_candidate_dataset(
        features,
        strategy_config,
        dataset_config=model_settings.dataset.to_config(),
    )
    dataset = _filter_dataset_period(dataset, start=start, end=end)

    walk_forward_config = model_settings.walk_forward.to_config()
    folds = build_purged_walk_forward_splits(dataset, walk_forward_config)
    target_column = f"relative_return_{walk_forward_config.hold_period}"
    ridge_config = model_settings.ridge.to_config(
        feature_columns=model_settings.dataset.feature_columns,
        target_column=target_column,
    )
    result = run_ridge_walk_forward(dataset, folds, ridge_config)
    artifacts = write_ridge_baseline_report(
        result=result,
        dataset=dataset,
        strategy_config=strategy_config,
        model_settings=model_settings,
        run_id=run_id,
        start=start,
        end=end,
        symbols=requested,
        report_root=report_root,
    )
    summary = json.loads(artifacts.summary.read_text(encoding="utf-8"))
    summary["report_directory"] = str(artifacts.directory)
    return RidgeBaselineWorkflowResult(summary=summary, artifacts=artifacts)


def run_ranking_baseline_workflow(
    *,
    repository: ParquetRepository,
    database_path: Path,
    operations_database_path: Path,
    report_root: Path,
    symbols: tuple[str, ...],
    start: date,
    end: date,
    strategy_config: BuyTheDipConfig,
    model_settings: RankingBaselineSettings,
    run_id: UUID | None = None,
) -> RankingBaselineWorkflowResult:
    """Run rules-approved Ridge + LightGBM OOF validation and register the summary."""
    if start > end:
        raise ValueError("model start must not follow end")
    run_id = run_id or uuid4()
    requested = tuple(sorted({symbol.upper() for symbol in symbols}))
    benchmark_symbol = strategy_config.strategy.benchmark_symbol.upper()
    features = load_feature_history(
        repository=repository,
        database_path=database_path,
        symbols=requested,
        benchmark_symbol=benchmark_symbol,
        start=start,
        end=end,
    )
    dataset = build_candidate_dataset(
        features,
        strategy_config,
        dataset_config=model_settings.dataset.to_config(),
    )
    dataset = _filter_dataset_period(dataset, start=start, end=end)

    walk_forward_config = model_settings.walk_forward.to_config()
    folds = build_purged_walk_forward_splits(dataset, walk_forward_config)
    target_column = f"relative_return_{walk_forward_config.hold_period}"
    ridge_result = run_ridge_walk_forward(
        dataset,
        folds,
        model_settings.ridge.to_config(
            feature_columns=model_settings.dataset.feature_columns,
            target_column=target_column,
        ),
    )
    lightgbm_result = run_lightgbm_walk_forward(
        dataset,
        folds,
        model_settings.lightgbm.to_config(
            feature_columns=model_settings.dataset.feature_columns,
            target_column=target_column,
        ),
    )
    artifacts = write_ranking_baseline_report(
        ridge_result=ridge_result,
        lightgbm_result=lightgbm_result,
        dataset=dataset,
        strategy_config=strategy_config,
        model_settings=model_settings,
        target_column=target_column,
        run_id=run_id,
        start=start,
        end=end,
        symbols=requested,
        report_root=report_root,
    )
    summary = json.loads(artifacts.summary.read_text(encoding="utf-8"))
    summary["report_directory"] = str(artifacts.directory)
    with OperationsRegistry(operations_database_path) as registry:
        registry.record_model_run(summary)
    return RankingBaselineWorkflowResult(summary=summary, artifacts=artifacts)


def _filter_dataset_period(dataset: pd.DataFrame, *, start: date, end: date) -> pd.DataFrame:
    if dataset.empty:
        return dataset.copy()
    frame = dataset.copy()
    frame["signal_session"] = pd.to_datetime(frame["signal_session"]).dt.date
    return frame.loc[
        frame["signal_session"].between(start, end, inclusive="both")
    ].reset_index(drop=True)
