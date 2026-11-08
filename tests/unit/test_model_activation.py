import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest
import yaml

from quant_system.models.activation import DailyRankingPolicy, resolve_daily_ranking
from quant_system.models.comparison import comparison_table
from quant_system.models.config import load_ranking_baseline_settings
from quant_system.storage.sqlite import OperationsRegistry
from quant_system.strategy.config import load_buy_the_dip_config


def evidence(tmp_path):
    config = yaml.safe_load(Path("configs/models/ranking_baseline.yaml").read_text())
    config["walk_forward"].update(feature_lookback_days=1, hold_period=1, minimum_train_rows=2)
    config["dataset"]["holding_sessions"] = [1]
    path = tmp_path / "ranking.yaml"
    path.write_text(yaml.safe_dump(config))
    settings = load_ranking_baseline_settings(path)
    strategy = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    dataset = pd.DataFrame(
        {
            "signal_id": ["a", "b", "c", "d"],
            "signal_session": ["2026-01-02", "2026-01-03", "2026-01-10", "2026-01-11"],
            "label_end_session_1": ["2026-01-03", "2026-01-04", "2026-01-11", "2026-01-12"],
            "relative_return_1": [0, 0, -0.1, 0.1],
        }
    )
    ridge = pd.DataFrame(
        {
            "signal_id": ["c", "d"],
            "signal_session": ["2026-01-10", "2026-01-11"],
            "fold_number": [0, 0],
            "prediction": [0.1, -0.1],
            "target": [-0.1, 0.1],
        }
    )
    light = ridge.assign(prediction=[-0.1, 0.1])
    folds = pd.DataFrame(
        [
            dict(
                fold_number=0,
                status="READY",
                train_rows=2,
                train_start="2026-01-02",
                train_end="2026-01-09",
                validation_start="2026-01-10",
                validation_end="2026-01-11",
                embargo_cutoff="2026-01-08",
            )
        ]
    )
    tables = {
        "candidate_dataset": dataset,
        "ridge_oof_predictions": ridge,
        "lightgbm_oof_predictions": light,
        "ridge_fold_metrics": folds,
        "lightgbm_fold_metrics": folds,
        "comparison": comparison_table(
            dataset=dataset,
            target_column="relative_return_1",
            ridge_predictions=ridge,
            lightgbm_predictions=light,
            top_fraction=0.2,
        ),
    }
    artifacts = {}
    for name, frame in tables.items():
        artifact = tmp_path / f"{name}.csv"
        frame.to_csv(artifact, index=False)
        artifacts[name] = str(artifact)
    payload = {
        "model": settings.model_dump(mode="json"),
        "strategy": strategy.model_dump(mode="json"),
    }
    summary = dict(
        run_id="reviewed",
        model="ranking_baseline_v1",
        status="COMPLETED",
        end="2026-01-12",
        target_column="relative_return_1",
        config=payload,
        config_hash=hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        artifacts=artifacts,
        report_directory=str(tmp_path),
    )
    db = tmp_path / "operations.sqlite"
    with OperationsRegistry(db) as registry:
        registry.record_model_run(summary)
        registry.connection.execute(
            "UPDATE model_registry SET created_at_utc='2026-01-13T00:00:00+00:00'"
        )
        registry.connection.commit()
    return DailyRankingPolicy(mode="validated", config_path=path, approved_run_id="reviewed"), dict(
        database_path=db,
        strategy_config=strategy,
        session=date(2026, 1, 20),
        cutoff=datetime(2026, 1, 20, 22, tzinfo=UTC),
    )


def test_reviewed_compatible_oof_evidence_activates(tmp_path):
    policy, kwargs = evidence(tmp_path)
    settings, context = resolve_daily_ranking(policy, **kwargs)
    assert settings is not None and context["status"] == "VALIDATED"
    assert len(context["artifact_hashes"]) == 6


@pytest.mark.parametrize(
    "case", ["future", "embargo", "missing", "metrics", "config", "target", "samples"]
)
def test_unqualified_evidence_falls_back(tmp_path, case):
    policy, kwargs = evidence(tmp_path)
    if case == "future":
        kwargs["cutoff"] = datetime(2026, 1, 1, tzinfo=UTC)
    elif case == "samples":
        path = tmp_path / "ridge_oof_predictions.csv"
        frame = pd.read_csv(path).iloc[:1]
        frame.to_csv(path, index=False)
    elif case == "missing":
        (tmp_path / "ridge_oof_predictions.csv").unlink()
    elif case == "config":
        payload = yaml.safe_load(policy.config_path.read_text())
        payload["lightgbm"]["random_state"] = 9
        policy.config_path.write_text(yaml.safe_dump(payload))
    else:
        path = tmp_path / (
            "lightgbm_fold_metrics.csv" if case == "embargo" else "lightgbm_oof_predictions.csv"
        )
        frame = pd.read_csv(path)
        if case == "embargo":
            frame["embargo_cutoff"] = "2026-01-09"
        elif case == "target":
            frame["target"] = [10, 20]
        else:
            frame["prediction"] = [0.1, -0.1]
        frame.to_csv(path, index=False)
    settings, context = resolve_daily_ranking(policy, **kwargs)
    assert settings is None and context["status"] == "UNAVAILABLE_VALIDATION"


def test_policy_requires_explicit_review(tmp_path):
    _policy, kwargs = evidence(tmp_path)
    assert (
        resolve_daily_ranking(DailyRankingPolicy(mode="validated"), **kwargs)[1]["status"]
        == "AWAITING_VALIDATION_REVIEW"
    )
    assert resolve_daily_ranking(DailyRankingPolicy(), **kwargs)[1]["status"] == "DISABLED"
