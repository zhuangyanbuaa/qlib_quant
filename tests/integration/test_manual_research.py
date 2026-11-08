import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_daily_pipeline import Service, context

from quant_system.decision.manual_research import (
    combine_manual_research,
    import_manual_research,
    load_manual_package,
)
from quant_system.decision.workbench import run_daily_workbench_workflow
from quant_system.domain.manual_research import content_hash
from quant_system.operations.pipeline import run_daily_pipeline
from quant_system.quality.prices import PriceDataBlocked
from quant_system.storage.parquet import ParquetRepository


def test_manual_pipeline_skips_news_and_remains_pending(tmp_path):
    settings, kwargs = context(tmp_path)
    prices, news = Service(), Service(crash=True)
    report = run_daily_pipeline(
        settings, manual_news=True, price_service=prices, news_service=news, **kwargs
    )
    assert report["status"] == "DEGRADED"
    assert len(prices.calls) == 1 and not news.calls
    assert report["stages"]["news"]["result"]["reason"] == "manual_news_research"
    daily = json.loads(Path(report["report_path"]).read_text())
    assert daily["metadata"]["news_mode"] == "MANUAL_RESEARCH"
    assert daily["summary"]["execution_eligible_candidate_count"] == 0
    assert daily["manual_research"]["status"] == "PENDING_MANUAL_REVIEW"
    artifacts = daily["manual_research"]["artifacts"]
    assert all(Path(path).exists() for path in artifacts.values())
    package = load_manual_package(artifacts["package"])
    prompt = Path(artifacts["prompt"]).read_text()
    assert package["package_sha256"] in prompt and package["run_id"] in prompt
    assert "原始 score" in prompt and "实际联网" in prompt


@pytest.fixture
def research_context(tmp_path):
    settings, kwargs = context(tmp_path)

    def build(**args):
        config = args["strategy_config"]
        args["strategy_config"] = config.model_copy(
            update={
                "strategy": config.strategy.model_copy(
                    update={
                        "minimum_rsi": 0,
                        "maximum_rsi": 100,
                        "maximum_atr_drawdown": 100,
                        "minimum_relative_return_60": -0.05,
                    }
                )
            }
        )
        return run_daily_workbench_workflow(**args)

    result = run_daily_pipeline(
        settings, mode="replay", manual_news=True, report_builder=build, **kwargs
    )
    assert result["status"] == "DEGRADED", result
    daily = json.loads(Path(result["report_path"]).read_text())
    paths = daily["manual_research"]["artifacts"]
    package = load_manual_package(paths["package"])
    assert "AAPL" in package["quant_context"]["symbols"]
    payload = json.loads(Path(paths["template"]).read_text())
    now = kwargs["now_utc"] + timedelta(minutes=10)
    payload.update(
        analyst_system="fixture_browsing",
        model_version="fixed-fixture-1",
        generated_at_utc=now.isoformat(),
        news_cutoff_utc=now.isoformat(),
        overview="Fixture research, not real news.",
    )
    payload["reviews"][0].update(
        coverage="SEARCHED",
        observed_risk="HIGH",
        research_priority="FOCUS",
        evidence_ids=["e1"],
        integrated_view="Fixture HIGH veto.",
    )
    payload["evidence"] = [
        {
            "evidence_id": "e1",
            "symbol": "AAPL",
            "source_name": "fixture",
            "url": "https://example.com/fixture",
            "title": "Fixture regulatory risk",
            "summary": "Fixed test fixture; no actual event asserted.",
            "severity": "HIGH",
            "published_at_utc": (now - timedelta(hours=1)).isoformat(),
            "retrieved_at_utc": now.isoformat(),
        }
    ]
    result_path = tmp_path / "external-result.json"
    result_path.write_text(json.dumps(payload))
    options = {
        "repository": ParquetRepository(settings.resolved_data_dir),
        "database_path": settings.resolved_data_dir / "db/analytics.duckdb",
        "report_root": settings.resolved_data_dir / "reports/daily",
        "package_path": Path(paths["package"]),
        "now_utc": now + timedelta(seconds=1),
    }
    return options, result_path, payload, package


def test_import_preserves_quant_raw_and_gate_permissions_and_is_idempotent(research_context):
    options, path, _payload, package = research_context
    repo = options["repository"]
    parent_digest = options["package_path"].parent / "digest.json"
    digest_before = parent_digest.read_bytes()
    before = {p: p.read_bytes() for p in repo.daily_prices_root.rglob("*.parquet")}
    source_files = {
        options["package_path"].parent / row["file"]: (
            options["package_path"].parent / row["file"]
        ).read_bytes()
        for row in package["source_reports"].values()
    }
    dry = import_manual_research(**options, result_path=path, reviewed_by="fixture", dry_run=True)
    assert dry["status"] == "DRY_RUN" and repo.read_manual_research().num_rows == 0
    first = import_manual_research(**options, result_path=path, reviewed_by="fixture")
    report = json.loads(Path(first["report_path"]).read_text())
    row = report["symbols"][0]
    assert row["news_risk"] == "HIGH" and row["research_priority"] == "SKIP"
    assert row["execution_eligible"] is False
    assert row["quant"] == package["quant_context"]["by_symbol"]["AAPL"]
    assert report["summary"]["execution_eligible_candidate_count"] == 0
    assert report["metadata"]["news_cutoff_utc"] != package["price_cutoff_utc"]
    repeated = import_manual_research(**options, result_path=path, reviewed_by="fixture")
    assert repeated["reused_observation"] and repeated["files_written"] == 0
    fact = repo.read_manual_research().to_pylist()[0]
    assert fact["available_at_utc"] == options["now_utc"]
    assert fact["fetched_at_utc"] == options["now_utc"]
    assert fact["source"] == "manual_web_research" and "research_only" in fact["quality_flags"]
    assert all(p.read_bytes() == value for p, value in {**before, **source_files}.items())
    assert repo.read_news_coverage().num_rows == 0
    assert parent_digest.read_bytes() == digest_before
    revisions = [json.loads(p.read_text()) for p in options["report_root"].glob("*/*/digest.json")]
    reviewed = [r for r in revisions if r["metadata"].get("publication_kind") == "research"]
    assert reviewed and all(not r["entry_candidates"] for r in reviewed)
    assert reviewed[0]["stocks"][0]["entry_status"] == "VETOED"
    assert reviewed[0]["research"]["review"]["symbols"][0]["news_risk"] == "HIGH"
    assert not (options["report_root"] / "latest.md").exists()  # Replay stays isolated.


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_run",
        "wrong_symbol",
        "score_field",
        "future",
        "wrong_evidence",
        "wrong_url",
        "duplicate_review",
        "template",
    ],
)
def test_invalid_result_rejected_before_any_append(research_context, fault):
    from uuid import uuid4

    options, path, payload, _ = research_context
    if fault == "wrong_run":
        payload["run_id"] = str(uuid4())
    elif fault == "wrong_symbol":
        payload["reviews"][0]["symbol"] = "MISSING"
    elif fault == "score_field":
        payload["reviews"][0]["score"] = 100
    elif fault == "future":
        payload["generated_at_utc"] = (options["now_utc"] + timedelta(hours=1)).isoformat()
    elif fault == "wrong_evidence":
        payload["reviews"][0]["evidence_ids"] = ["absent"]
    elif fault == "wrong_url":
        payload["evidence"][0]["url"] = "javascript:alert(1)"
    elif fault == "duplicate_review":
        payload["reviews"].append(payload["reviews"][0])
    else:
        payload["analyst_system"] = "UNRUN_TEMPLATE"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        import_manual_research(**options, result_path=path, reviewed_by="fixture")
    assert options["repository"].read_manual_research().num_rows == 0


def test_revisions_append_and_cannot_erase_previous_high_evidence(research_context):
    options, path, payload, _ = research_context
    first = import_manual_research(**options, result_path=path, reviewed_by="fixture")
    repo = options["repository"]
    existing_files = {p: p.read_bytes() for p in repo.manual_research_root.rglob("*.parquet")}
    payload["reviews"][0].update(observed_risk="UNKNOWN", evidence_ids=[])
    payload["evidence"] = []
    payload["overview"] = "Revision without additional risk evidence."
    path.write_text(json.dumps(payload))
    later = options | {"now_utc": options["now_utc"] + timedelta(minutes=1)}
    revised = import_manual_research(**later, result_path=path, reviewed_by="fixture")
    assert revised["review_id"] != first["review_id"]
    assert repo.read_manual_research().num_rows == 2
    assert all(p.read_bytes() == value for p, value in existing_files.items())
    assert json.loads(Path(revised["report_path"]).read_text())["symbols"][0]["news_risk"] == "HIGH"


def test_late_import_visibility_and_missing_reviews(research_context):
    options, path, payload, _ = research_context
    payload["evidence"] = []
    payload["reviews"] = []
    path.write_text(json.dumps(payload))
    imported = import_manual_research(**options, result_path=path, reviewed_by="fixture")
    visible = json.loads(Path(imported["report_path"]).read_text())
    assert visible["status"] == "PARTIAL_RESEARCH"
    assert visible["symbols"][0]["news_risk"] == "UNKNOWN"
    earlier = combine_manual_research(
        **options, cutoff_utc=options["now_utc"] - timedelta(seconds=1)
    )
    assert earlier["combined_status"] == "PENDING_MANUAL_REVIEW"
    old = json.loads(Path(earlier["report_path"]).read_text())
    assert old["observation"] is None and old["symbols"][0]["review_status"] == "NOT_REVIEWED"


def test_partial_batch_import_does_not_erase_previously_reviewed_symbols(research_context):
    options, path, payload, _ = research_context
    import_manual_research(**options, result_path=path, reviewed_by="fixture")
    payload["reviews"] = []
    payload["evidence"] = []
    payload["overview"] = "Another partial batch."
    path.write_text(json.dumps(payload))
    later = options | {"now_utc": options["now_utc"] + timedelta(minutes=1)}
    result = import_manual_research(**later, result_path=path, reviewed_by="fixture")
    report = json.loads(Path(result["report_path"]).read_text())
    row = next(row for row in report["symbols"] if row["symbol"] == "AAPL")
    assert row["review_status"] == "SEARCHED" and row["news_risk"] == "HIGH"
    assert row["execution_eligible"] is False


def test_legacy_v1_packages_keep_their_actual_source_version(research_context):
    from uuid import UUID, uuid5

    options, path, payload, package = research_context
    package["prompt_version"] = "manual-news-prompt-v1"
    package["package_id"] = str(uuid5(UUID(package["run_id"]), package["prompt_version"]))
    package["package_sha256"] = content_hash(
        {k: v for k, v in package.items() if k != "package_sha256"}
    )
    options["package_path"].write_text(json.dumps(package))
    payload.update(
        {key: package[key] for key in ("prompt_version", "package_id", "package_sha256")}
    )
    path.write_text(json.dumps(payload))
    result = import_manual_research(**options, result_path=path, reviewed_by="fixture")
    fact = options["repository"].read_manual_research().to_pylist()[0]
    assert fact["source_version"].endswith(":manual-news-prompt-v1")
    assert (
        json.loads(Path(result["report_path"]).read_text())["metadata"]["prompt_version"]
        == "manual-news-prompt-v1"
    )


def test_package_and_source_tampering_rejected(research_context):
    options, path, _, package = research_context
    package["quant_context"]["by_symbol"]["AAPL"]["canonical_candidates"][0]["score"] = 999
    package["package_sha256"] = content_hash(
        {k: v for k, v in package.items() if k != "package_sha256"}
    )
    options["package_path"].write_text(json.dumps(package))
    with pytest.raises(ValueError, match="quant context"):
        import_manual_research(**options, result_path=path, reviewed_by="fixture")
    assert options["repository"].read_manual_research().num_rows == 0


def test_changed_source_report_and_config_tampering_are_rejected(research_context):
    options, path, _, package = research_context
    original = options["package_path"].read_bytes()
    package["strategy_config"]["strategy"]["benchmark_symbol"] = "OTHER"
    package["package_sha256"] = content_hash(
        {k: v for k, v in package.items() if k != "package_sha256"}
    )
    options["package_path"].write_text(json.dumps(package))
    with pytest.raises(ValueError, match="strategy config"):
        import_manual_research(**options, result_path=path, reviewed_by="fixture")
    options["package_path"].write_bytes(original)
    source = options["package_path"].parent / package["source_reports"]["premarket"]["file"]
    source.write_text(source.read_text() + " ")
    with pytest.raises(ValueError, match="source quant report changed"):
        import_manual_research(**options, result_path=path, reviewed_by="fixture")
    assert options["repository"].read_manual_research().num_rows == 0


def test_current_critical_price_failure_blocks_import_before_append(research_context):
    from datetime import date

    from test_price_decision_cutoff import price, seed

    options, path, _, _ = research_context
    bad = price("SPY", date(2026, 6, 30), 100, stale=True)
    bad = bad.model_copy(update={"fetched_at_utc": bad.fetched_at_utc + timedelta(minutes=1)})
    seed(options["repository"], [bad])
    with pytest.raises(PriceDataBlocked):
        import_manual_research(**options, result_path=path, reviewed_by="fixture")
    assert options["repository"].read_manual_research().num_rows == 0


def test_manual_mode_preserves_scores_and_signal_ids_without_disabling_news_gate(
    research_context, monkeypatch
):
    from datetime import date

    from quant_system.decision.premarket import run_premarket_workflow
    from quant_system.domain.enums import EventSeverity
    from quant_system.sentiment.risk import NewsRiskAssessment
    from quant_system.strategy.config import BuyTheDipConfig

    options, _, _, package = research_context
    repository = options["repository"]
    kwargs = dict(
        repository=repository,
        database_path=options["database_path"],
        report_root=options["report_root"],
        universe_paths=(repository.data_root.parent / "universe.yaml",),
        signal_session=date.fromisoformat(package["signal_session"]),
        config=BuyTheDipConfig.model_validate(package["strategy_config"]),
        include_news_risk=False,
        news_lookback_hours=72,
        generated_at_utc=options["now_utc"],
    )
    baseline, _ = run_premarket_workflow(**kwargs)
    manual, _ = run_premarket_workflow(**kwargs, manual_news=True)
    assert baseline["candidates"] and manual["candidates"]
    assert [(r["signal_id"], r["score"]) for r in baseline["candidates"]] == [
        (r["signal_id"], r["score"]) for r in manual["candidates"]
    ]
    assert manual["metadata"]["news_risk_enabled"] is True
    assert all(
        r["news_risk"] == "UNKNOWN" and not r["execution_eligible"] for r in manual["candidates"]
    )
    monkeypatch.setattr(
        "quant_system.decision.premarket._load_news_risk",
        lambda **_: {"AAPL": NewsRiskAssessment(symbol="AAPL", severity=EventSeverity.HIGH)},
    )
    vetoed, _ = run_premarket_workflow(**kwargs, manual_news=True)
    assert not vetoed["candidates"]


def test_mode_is_part_of_cache_key_and_missing_prompt_is_rebuilt(tmp_path):
    settings, kwargs = context(tmp_path)
    prices, news = Service(), Service("DEGRADED")
    api = run_daily_pipeline(settings, price_service=prices, news_service=news, **kwargs)
    manual = run_daily_pipeline(
        settings, manual_news=True, price_service=prices, news_service=news, **kwargs
    )
    assert api["run_id"] != manual["run_id"] and len(news.calls) == 1
    daily = json.loads(Path(manual["report_path"]).read_text())
    Path(daily["manual_research"]["artifacts"]["prompt"]).unlink()
    rebuilt = run_daily_pipeline(
        settings, manual_news=True, price_service=prices, news_service=news, **kwargs
    )
    assert rebuilt["reused"] is False
    assert len(prices.calls) == 2 and len(news.calls) == 1


def test_stale_and_expired_research_cannot_be_execution_eligible(research_context):
    options, path, _, package = research_context
    imported = import_manual_research(**options, result_path=path, reviewed_by="fixture")
    old_bytes = Path(imported["report_path"]).read_bytes()
    later = options["now_utc"] + timedelta(hours=2)
    combined = combine_manual_research(**(options | {"now_utc": later}), cutoff_utc=later)
    report = json.loads(Path(combined["report_path"]).read_text())
    assert combined["combined_status"] == "STALE_RESEARCH"
    assert report["metadata"]["order_window_expired"] is True
    assert report["summary"]["execution_eligible_candidate_count"] == 0
    assert Path(imported["report_path"]).read_bytes() == old_bytes
    with pytest.raises(ValueError, match="package availability"):
        combine_manual_research(
            **options,
            cutoff_utc=datetime.fromisoformat(package["price_cutoff_utc"]),
        )


def test_cli_import_and_combine_manual_research_and_error_status(research_context, monkeypatch):
    from typer.testing import CliRunner

    from quant_system.cli import app
    from quant_system.settings import get_settings

    options, path, _, _ = research_context
    monkeypatch.setenv("QUANT_DATA_DIR", str(options["repository"].data_root))
    get_settings.cache_clear()
    runner = CliRunner()
    args = [
        "decision",
        "import-manual-research",
        "--package",
        str(options["package_path"]),
        "--result",
        str(path),
        "--reviewed-by",
        "fixture",
    ]
    try:
        dry = runner.invoke(app, [*args, "--dry-run"])
        assert dry.exit_code == 0 and json.loads(dry.stdout)["status"] == "DRY_RUN"
        imported = runner.invoke(app, args)
        assert imported.exit_code == 0, imported.output
        assert json.loads(imported.stdout)["execution_eligible_candidate_count"] == 0
        combined = runner.invoke(
            app, ["decision", "combine-manual-research", "--package", str(options["package_path"])]
        )
        assert combined.exit_code == 0 and json.loads(combined.stdout)["status"] == "RESEARCH_ONLY"
        path.write_text('{"unexpected": "schema"}')
        rejected = runner.invoke(app, args)
        assert rejected.exit_code == 2 and json.loads(rejected.stdout)["status"] == "REJECTED"
    finally:
        get_settings.cache_clear()
