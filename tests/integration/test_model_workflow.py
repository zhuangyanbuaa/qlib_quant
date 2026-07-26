from datetime import date
from pathlib import Path
from uuid import UUID

import pandas as pd

from quant_system.models.config import RidgeBaselineSettings
from quant_system.models.workflow import (
    run_ranking_baseline_workflow,
    run_ridge_baseline_workflow,
)
from quant_system.storage.parquet import ParquetRepository
from quant_system.storage.sqlite import OperationsRegistry
from quant_system.strategy.config import load_buy_the_dip_config


def candidate_dataset() -> pd.DataFrame:
    sessions = pd.bdate_range("2026-01-01", periods=12)
    rows = []
    for index, session in enumerate(sessions):
        rows.append(
            {
                "signal_id": f"sig-{index}",
                "symbol": "AAPL",
                "signal_session": session.date(),
                "earliest_order_session": session.date(),
                "feature": float(index),
                "relative_return_2": float(index) / 100,
                "label_end_session_2": sessions[min(index + 2, len(sessions) - 1)].date(),
            }
        )
    return pd.DataFrame(rows)


def settings() -> RidgeBaselineSettings:
    return RidgeBaselineSettings.model_validate(
        {
            "dataset": {
                "holding_sessions": [2],
                "feature_columns": ["feature"],
            },
            "walk_forward": {
                "train_sessions": 8,
                "validation_sessions": 2,
                "step_sessions": 2,
                "hold_period": 2,
                "feature_lookback_days": 1,
                "minimum_train_rows": 1,
                "minimum_validation_rows": 1,
            },
            "ridge": {
                "alpha": 1.0,
                "top_fraction": 0.5,
            },
        }
    )


def test_ridge_baseline_workflow_writes_oof_artifacts(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "quant_system.models.workflow.load_feature_history",
        lambda **_kwargs: pd.DataFrame({"placeholder": []}),
    )
    monkeypatch.setattr(
        "quant_system.models.workflow.build_candidate_dataset",
        lambda *_args, **_kwargs: candidate_dataset(),
    )

    result = run_ridge_baseline_workflow(
        repository=ParquetRepository(tmp_path / "data"),
        database_path=tmp_path / "analytics.duckdb",
        report_root=tmp_path / "reports" / "models",
        symbols=("AAPL",),
        start=date(2026, 1, 1),
        end=date(2026, 1, 20),
        strategy_config=load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml")),
        model_settings=settings(),
        run_id=UUID("00000000-0000-0000-0000-000000000005"),
    )

    assert result.summary["status"] == "COMPLETED"
    assert result.summary["ready_fold_count"] == 2
    assert result.artifacts.summary.exists()
    assert result.artifacts.candidate_dataset.exists()
    assert result.artifacts.oof_predictions.exists()
    assert result.artifacts.fold_metrics.exists()
    assert pd.read_csv(result.artifacts.oof_predictions).shape[0] == 4


def ranking_settings():
    payload = settings().model_dump(mode="json")
    payload["lightgbm"] = {
        "n_estimators": 5,
        "learning_rate": 0.1,
        "num_leaves": 3,
        "max_depth": 2,
        "min_child_samples": 2,
        "subsample": 1.0,
        "colsample_bytree": 1.0,
        "reg_alpha": 0.0,
        "reg_lambda": 1.0,
        "random_state": 42,
        "n_jobs": 1,
        "top_fraction": 0.5,
        "minimum_train_rows": 2,
    }
    payload["calibration"] = {"buckets": 2}
    from quant_system.models.config import RankingBaselineSettings

    return RankingBaselineSettings.model_validate(payload)


def test_ranking_baseline_workflow_writes_registry(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "quant_system.models.workflow.load_feature_history",
        lambda **_kwargs: pd.DataFrame({"placeholder": []}),
    )
    monkeypatch.setattr(
        "quant_system.models.workflow.build_candidate_dataset",
        lambda *_args, **_kwargs: candidate_dataset(),
    )

    result = run_ranking_baseline_workflow(
        repository=ParquetRepository(tmp_path / "data"),
        database_path=tmp_path / "analytics.duckdb",
        operations_database_path=tmp_path / "db" / "operations.sqlite",
        report_root=tmp_path / "reports" / "models",
        symbols=("AAPL",),
        start=date(2026, 1, 1),
        end=date(2026, 1, 20),
        strategy_config=load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml")),
        model_settings=ranking_settings(),
        run_id=UUID("00000000-0000-0000-0000-000000000006"),
    )

    assert result.summary["model"] == "ranking_baseline_v1"
    assert result.artifacts.comparison.exists()
    assert result.artifacts.lightgbm_calibration.exists()
    with OperationsRegistry(tmp_path / "db" / "operations.sqlite") as registry:
        runs = registry.latest_model_runs()
    assert runs[0]["run_id"] == "00000000-0000-0000-0000-000000000006"
