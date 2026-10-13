import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from quant_system.decision.manual_research import (
    export_manual_research_package,
    load_manual_package,
)
from quant_system.domain.manual_research import ManualResearchResult
from quant_system.strategy.config import load_buy_the_dip_config


def result_payload():
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    return {
        "package_id": str(uuid4()),
        "package_sha256": "a" * 64,
        "run_id": str(uuid4()),
        "signal_session": "2026-06-30",
        "price_cutoff_utc": "2026-06-30T20:30:00Z",
        "analyst_system": "fixture",
        "model_version": "fixture-1",
        "generated_at_utc": now.isoformat(),
        "news_cutoff_utc": now.isoformat(),
        "overview": "fixture",
        "evidence": [
            {
                "evidence_id": "e1",
                "symbol": "AAPL",
                "source_name": "fixture",
                "url": "https://example.com/fixture",
                "title": "fixture",
                "summary": "fixture",
                "severity": "HIGH",
                "published_at_utc": (now - timedelta(hours=1)).isoformat(),
                "retrieved_at_utc": now.isoformat(),
            }
        ],
        "reviews": [
            {
                "symbol": "AAPL",
                "coverage": "SEARCHED",
                "observed_risk": "HIGH",
                "research_priority": "SKIP",
                "quant_interpretation": "fixture",
                "catalyst_assessment": "fixture",
                "risk_assessment": "fixture",
                "integrated_view": "fixture",
                "evidence_ids": ["e1"],
                "limitations": [],
            }
        ],
    }


@pytest.mark.parametrize(
    "fault",
    [
        "non_utc",
        "publication_after_retrieval",
        "future_retrieval",
        "duplicate_evidence",
        "missing_risk_support",
        "unsearched_risk",
        "trade_permission",
    ],
)
def test_manual_result_contract_rejects_unsafe_or_inconsistent_data(fault):
    payload = result_payload()
    if fault == "non_utc":
        payload["news_cutoff_utc"] = "2026-07-01T21:00:00+09:00"
    elif fault == "publication_after_retrieval":
        payload["evidence"][0]["published_at_utc"] = "2026-07-01T13:00:00Z"
    elif fault == "future_retrieval":
        payload["evidence"][0]["retrieved_at_utc"] = "2026-07-01T13:00:00Z"
    elif fault == "duplicate_evidence":
        payload["evidence"].append(payload["evidence"][0])
    elif fault == "missing_risk_support":
        payload["evidence"][0]["severity"] = "LOW"
    elif fault == "unsearched_risk":
        payload["reviews"][0]["coverage"] = "NOT_SEARCHED"
    else:
        payload["execution_eligible"] = True
    with pytest.raises(ValueError):
        ManualResearchResult.model_validate(payload)


def test_empty_research_package_still_exports_portable_prompt_and_strict_template(tmp_path):
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    metadata = {
        "run_id": str(uuid4()),
        "signal_session": "2026-06-30",
        "price_cutoff_utc": "2026-06-30T20:30:00+00:00",
        "generated_at_utc": "2026-07-01T12:00:00+00:00",
        "earliest_order_time_utc": "2026-07-01T13:30:00+00:00",
    }
    reports = {
        "premarket": {
            "metadata": metadata,
            "candidates": [],
            "strategy_config_snapshot": config.model_dump(mode="json"),
        },
        "research": {"metadata": metadata, "research_candidates": []},
        "leverage_overlay": {"metadata": metadata, "assessments": []},
    }
    components = {}
    for name, report in reports.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(report))
        components[name] = {"status": "COMPLETED", "artifacts": {"json": str(path)}}
    components["positions"] = {"status": "SKIPPED"}
    output = export_manual_research_package({"components": components}, tmp_path, config)
    package = load_manual_package(output["artifacts"]["package"])
    assert package["quant_context"]["symbols"] == [] and output["symbol_count"] == 0
    template = ManualResearchResult.model_validate_json(
        Path(output["artifacts"]["template"]).read_text()
    )
    assert template.reviews == () and template.evidence == ()
    assert template.analyst_system == "UNRUN_TEMPLATE"
