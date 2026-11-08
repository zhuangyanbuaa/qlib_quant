"""Explicit review plus compatible point-in-time OOF evidence for daily ranking."""

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict

from quant_system.models.comparison import comparison_table, lightgbm_promotion_status
from quant_system.models.config import load_ranking_baseline_settings
from quant_system.settings import PROJECT_ROOT


class DailyRankingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["off", "validated"] = "off"
    config_path: Path = PROJECT_ROOT / "configs/models/ranking_baseline.yaml"
    approved_run_id: str | None = None


def resolve_daily_ranking(policy, *, database_path, strategy_config, session, cutoff):
    context = {"status": "DISABLED", "mode": policy.mode, "approved_run_id": policy.approved_run_id}
    if policy.mode == "off":
        return None, context
    if not policy.approved_run_id:
        return None, context | {"status": "AWAITING_VALIDATION_REVIEW"}
    try:
        path = policy.config_path
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        settings = load_ranking_baseline_settings(path)
        with sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT summary_json,created_at_utc FROM model_registry WHERE run_id=?",
                (policy.approved_run_id,),
            ).fetchone()
        if row is None:
            raise ValueError("approved_run_not_found")
        summary = json.loads(row[0])
        created = datetime.fromisoformat(row[1])
        if created.utcoffset() is None or created > cutoff or summary["end"] >= session.isoformat():
            raise ValueError("validation_after_decision_cutoff")
        expected = {
            "model": settings.model_dump(mode="json"),
            "strategy": strategy_config.model_dump(mode="json"),
        }
        if (
            summary["config"] != expected
            or summary["config_hash"]
            != hashlib.sha256(
                json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        ):
            raise ValueError("validation_config_mismatch")
        if summary["model"] != "ranking_baseline_v1" or summary["status"] != "COMPLETED":
            raise ValueError("validation_not_completed")
        comparison = pd.read_csv(summary["artifacts"]["comparison"])
        if lightgbm_promotion_status(comparison) != "ELIGIBLE_FOR_REVIEW":
            raise ValueError("validation_baseline_not_beaten")
        dataset = pd.read_csv(summary["artifacts"]["candidate_dataset"])
        label_column = f"label_end_session_{settings.walk_forward.hold_period}"
        label_ends = pd.to_datetime(dataset[label_column], errors="coerce")
        signals = pd.to_datetime(dataset["signal_session"])
        all_predictions = {}
        # Independently re-check fold embargo and label maturity, even for an approved run.
        for model in ("ridge", "lightgbm"):
            folds = pd.read_csv(summary["artifacts"][f"{model}_fold_metrics"])
            ready = folds[folds.status == "READY"]
            if ready.empty:
                raise ValueError("validation_no_ready_folds")
            for fold in ready.to_dict("records"):
                start = pd.Timestamp(fold["validation_start"])
                embargo = pd.Timestamp(fold["embargo_cutoff"])
                if (start - embargo).days < settings.walk_forward.to_config().embargo_days:
                    raise ValueError("validation_embargo_too_short")
                train = dataset.loc[
                    signals.between(
                        pd.Timestamp(fold["train_start"]), pd.Timestamp(fold["train_end"])
                    )
                    & (label_ends <= embargo)
                ]
                if (
                    len(train) != fold["train_rows"]
                    or len(train) < settings.walk_forward.minimum_train_rows
                ):
                    raise ValueError("validation_training_overlap_or_count")
            predictions = pd.read_csv(summary["artifacts"][f"{model}_oof_predictions"])
            if (
                predictions.empty
                or pd.to_datetime(predictions["signal_session"]).dt.date.max() >= session
            ):
                raise ValueError("validation_future_predictions")
            all_predictions[model] = predictions
            for prediction in predictions.to_dict("records"):
                matching = ready.loc[ready.fold_number == prediction["fold_number"]]
                if len(matching) != 1:
                    raise ValueError("validation_unknown_fold")
                fold = matching.iloc[0]
                timestamp = pd.Timestamp(prediction["signal_session"])
                if (
                    not pd.Timestamp(fold.validation_start)
                    <= timestamp
                    <= pd.Timestamp(fold.validation_end)
                ):
                    raise ValueError("validation_prediction_outside_fold")
                target = dataset.loc[
                    dataset.signal_id == prediction["signal_id"], summary["target_column"]
                ]
                if len(target) != 1 or float(target.iloc[0]) != float(prediction["target"]):
                    raise ValueError("validation_target_mismatch")
                actual_session = signals.loc[dataset.signal_id == prediction["signal_id"]].iloc[0]
                if actual_session != timestamp:
                    raise ValueError("validation_signal_time_mismatch")
        keys = ["signal_id", "fold_number"]
        compared = [
            set(frame[keys].itertuples(index=False, name=None))
            for frame in all_predictions.values()
        ]
        if compared[0] != compared[1] or any(
            len(keys_set) != len(frame)
            for keys_set, frame in zip(compared, all_predictions.values(), strict=True)
        ):
            raise ValueError("validation_mismatched_oof_samples")
        oof_dataset = dataset.loc[dataset.signal_id.isin(all_predictions["lightgbm"].signal_id)]
        actual_comparison = comparison_table(
            dataset=oof_dataset,
            target_column=summary["target_column"],
            ridge_predictions=all_predictions["ridge"],
            lightgbm_predictions=all_predictions["lightgbm"],
            top_fraction=settings.lightgbm.top_fraction,
        )
        if lightgbm_promotion_status(actual_comparison) != "ELIGIBLE_FOR_REVIEW":
            raise ValueError("validation_recomputed_metrics_ineligible")
        ends = label_ends.dropna()
        if ends.empty or ends.dt.date.max() >= session:
            raise ValueError("validation_immature_labels")
        artifact_hashes = {
            name: hashlib.sha256(Path(value).read_bytes()).hexdigest()
            for name, value in summary["artifacts"].items()
        }
        return settings, context | {
            "status": "VALIDATED",
            "validation_config_hash": summary["config_hash"],
            "artifact_hashes": artifact_hashes,
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
        # No paths, provider errors or arbitrary registry text enter public output.
        return None, context | {
            "status": "UNAVAILABLE_VALIDATION",
            "reason": "missing_incompatible_or_unqualified_oof_evidence",
        }
