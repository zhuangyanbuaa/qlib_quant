from datetime import date
from pathlib import Path
from uuid import UUID

import pandas as pd

from quant_system.models.config import RidgeBaselineSettings
from quant_system.models.workflow import run_ridge_baseline_workflow
from quant_system.storage.parquet import ParquetRepository
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
