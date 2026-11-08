import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_price_decision_cutoff import seed_smoke_prices

from quant_system.operations.pipeline import run_daily_pipeline
from quant_system.operations.registry import PipelineBusy, pipeline_lock
from quant_system.settings import Settings
from quant_system.storage.parquet import ParquetRepository


def context(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    seed_smoke_prices(ParquetRepository(settings.resolved_data_dir))
    payload = yaml.safe_load(Path("configs/universe/ai_watchlist.yaml").read_text())
    payload["selection_policy"].update(minimum_size=1, maximum_size=1)
    payload["symbols"] = [
        payload["symbols"][0]
        | {"symbol": "AAPL", "start_date": "2025-03-03", "benchmark_etf": "QQQ"}
    ]
    path = tmp_path / "universe.yaml"
    path.write_text(yaml.safe_dump(payload))
    return settings, dict(
        signal_session=date(2026, 6, 30),
        universe_paths=(path,),
        now_utc=datetime(2026, 7, 1, 12, tzinfo=UTC),
        context_price_service=Service(),
    )


class Service:
    def __init__(self, status="SUCCESS", crash=False):
        self.calls = []
        self.status = status
        self.crash = crash

    def run(self, *args, **kwargs):
        self.calls.append(kwargs.get("run_id"))
        if self.crash:
            raise SystemExit("simulated process crash")
        return SimpleNamespace(
            to_dict=lambda: {"status": self.status, "blocked_reasons": ["fixture_block"]}
        ), Path("fixture-report.json")


def test_live_pipeline_caches_run_and_records_stages(tmp_path):
    settings, kwargs = context(tmp_path)
    prices, news = Service(), Service("DEGRADED")
    first = run_daily_pipeline(settings, price_service=prices, news_service=news, **kwargs)
    assert first["status"] == "DEGRADED"
    assert set(first["stages"]) == {
        "readiness",
        "prices",
        "context_prices",
        "market_context_acquisition",
        "market_context",
        "news",
        "quality",
        "report",
    }
    assert Path(first["report_path"]).exists()
    assert (
        json.loads(Path(first["report_path"]).read_text())["metadata"]["news_cutoff_utc"]
        == "2026-07-01T12:00:00+00:00"
    )
    second = run_daily_pipeline(settings, price_service=prices, news_service=news, **kwargs)
    assert second["reused"] is True
    assert first["run_id"] == second["run_id"]
    assert len(prices.calls) == len(news.calls) == 1


def test_crash_resume_skips_successful_price_stage_and_uses_new_attempt_id(tmp_path):
    settings, kwargs = context(tmp_path)
    prices, news = Service(), Service(crash=True)
    with pytest.raises(SystemExit):
        run_daily_pipeline(settings, price_service=prices, news_service=news, **kwargs)
    news.crash = False
    recovered = run_daily_pipeline(settings, price_service=prices, news_service=news, **kwargs)
    assert recovered["status"] == "DEGRADED"
    assert len(prices.calls) == 1
    assert len(news.calls) == 2 and news.calls[0] != news.calls[1]
    assert recovered["stages"]["news"]["attempts"] == 2


def test_price_failure_stops_news_and_report(tmp_path):
    settings, kwargs = context(tmp_path)
    prices, news = Service("BLOCKED"), Service()
    result = run_daily_pipeline(settings, price_service=prices, news_service=news, **kwargs)
    assert result["status"] == "BLOCKED"
    assert not news.calls
    assert "report" not in result["stages"]
    root = settings.resolved_data_dir / "reports/daily"
    assert not list(root.glob("*/*/premarket.json"))
    assert "暂停入场建议" in (root / "latest.md").read_text()


def test_replay_skips_providers_and_process_lock_excludes_second_runner(tmp_path):
    settings, kwargs = context(tmp_path)
    prices, news = Service(), Service()
    with pipeline_lock(settings.resolved_data_dir):
        with pytest.raises(PipelineBusy), pipeline_lock(settings.resolved_data_dir):
            pass
        assert run_daily_pipeline(settings, mode="replay", **kwargs)["blocked_reasons"] == [
            "pipeline_already_running"
        ]
    replay = run_daily_pipeline(
        settings, mode="replay", price_service=prices, news_service=news, **kwargs
    )
    assert replay["status"] == "DEGRADED"
    assert not prices.calls and not news.calls
    assert replay["stages"]["prices"]["result"]["status"] == "SKIPPED"


def test_missing_component_artifact_rebuilds_report_without_repeating_acquisition(tmp_path):
    settings, kwargs = context(tmp_path)
    prices, news = Service(), Service("DEGRADED")
    first = run_daily_pipeline(settings, price_service=prices, news_service=news, **kwargs)
    report = json.loads(Path(first["report_path"]).read_text())
    Path(report["components"]["premarket"]["artifacts"]["json"]).unlink()
    recovered = run_daily_pipeline(settings, price_service=prices, news_service=news, **kwargs)
    assert recovered["reused"] is False
    assert recovered["report_path"] != first["report_path"]
    assert len(prices.calls) == len(news.calls) == 1
    assert recovered["stages"]["report"]["attempts"] == 2


def test_live_request_rejects_old_refresh_window_before_provider_calls(tmp_path):
    settings, kwargs = context(tmp_path)
    prices = Service()
    with pytest.raises(ValueError, match="current UTC half-hour"):
        run_daily_pipeline(
            settings,
            price_service=prices,
            refresh_window=kwargs["now_utc"] - timedelta(hours=1),
            **kwargs,
        )
    assert not prices.calls


def test_new_raw_fact_invalidates_report_cache_without_repeating_acquisition(tmp_path):
    from uuid import uuid4

    from quant_system.domain.coverage import NewsCoverageObservation

    settings, kwargs = context(tmp_path)
    prices, news = Service(), Service("DEGRADED")
    first = run_daily_pipeline(settings, price_service=prices, news_service=news, **kwargs)
    now = kwargs["now_utc"]
    fact = NewsCoverageObservation(
        coverage_id=str(uuid4()),
        symbol="AAPL",
        channel="news",
        result="EMPTY",
        rows_returned=0,
        window_start_utc=now - timedelta(hours=72),
        window_end_utc=now,
        source="fixture",
        source_version="1",
        fetched_at_utc=now,
        available_at_utc=now,
        ingestion_run_id=uuid4(),
    )
    ParquetRepository(settings.resolved_data_dir).write_news_coverage(
        (fact,), run_id=fact.ingestion_run_id
    )
    refreshed = run_daily_pipeline(settings, price_service=prices, news_service=news, **kwargs)
    assert refreshed["reused"] is False
    assert refreshed["report_path"] != first["report_path"]
    assert len(prices.calls) == len(news.calls) == 1


def test_daily_model_policy_changes_cache_and_is_visible_in_report(tmp_path):
    from quant_system.models.activation import DailyRankingPolicy

    settings, kwargs = context(tmp_path)
    first = run_daily_pipeline(settings, mode="replay", **kwargs)
    second = run_daily_pipeline(
        settings, mode="replay", ranking_policy=DailyRankingPolicy(mode="validated"), **kwargs
    )
    assert first["run_id"] != second["run_id"] and not second["reused"]
    assert second["model_activation"]["status"] == "AWAITING_VALIDATION_REVIEW"
    report = json.loads(Path(second["report_path"]).read_text())
    assert report["model_activation"] == second["model_activation"]
    assert report["summary"]["execution_eligible_candidate_count"] == 0


def test_digest_config_is_in_cache_identity_and_json_accounts_for_every_symbol(tmp_path):
    settings, kwargs = context(tmp_path)
    payload = yaml.safe_load(Path("configs/reports/daily_digest.yaml").read_text())
    config_path = tmp_path / "digest.yaml"
    config_path.write_text(yaml.safe_dump(payload))
    first = run_daily_pipeline(settings, mode="replay", digest_config_path=config_path, **kwargs)
    assert first["status"] == "DEGRADED"
    report = json.loads(Path(first["report_path"]).read_text())
    assert [row["symbol"] for row in report["digest"]["coverage"]] == ["AAPL"]
    assert report["digest"]["schema_version"] == "2"
    assert Path(report["artifacts"]["digest_markdown"]).exists()
    assert report["digest"]["entry_candidates"] == []
    payload["score_version"] = "technical-repair-v2-fixture"
    config_path.write_text(yaml.safe_dump(payload))
    second = run_daily_pipeline(settings, mode="replay", digest_config_path=config_path, **kwargs)
    assert second["run_id"] != first["run_id"]


def test_context_proxy_failure_does_not_block_canonical_price_scan(tmp_path):
    settings, kwargs = context(tmp_path)
    kwargs["context_price_service"] = Service("BLOCKED")
    result = run_daily_pipeline(
        settings,
        price_service=Service(),
        news_service=Service(),
        **kwargs,
    )
    assert result["status"] == "DEGRADED" and Path(result["report_path"]).exists()
    assert result["stages"]["context_prices"]["result"]["status"] == "DEGRADED"


def test_market_context_fixture_to_pipeline_json_and_replay_is_causal(tmp_path):
    settings, kwargs = context(tmp_path)
    source_path = tmp_path / "market.yaml"
    source_path.write_text("provider: cnn\n")
    fixture = json.loads(Path("tests/fixtures/market_context/cnn_contract.json").read_text())
    calls = []

    def transport(timeout):
        calls.append(timeout)
        return fixture

    live = run_daily_pipeline(
        settings,
        price_service=Service(),
        manual_news=True,
        market_context_config_path=source_path,
        market_context_transport=transport,
        **kwargs,
    )
    assert live["status"] == "DEGRADED"
    assert live["digest"]["market"]["cnn_fear_greed"]["value"] == 42.5
    report = json.loads(Path(live["report_path"]).read_text())
    assert report["digest"] == live["digest"]
    assert (
        report["digest"]["metadata"]["market_context_cutoff_utc"] == kwargs["now_utc"].isoformat()
    )
    replay = run_daily_pipeline(
        settings,
        mode="replay",
        manual_news=True,
        market_context_config_path=source_path,
        market_context_transport=transport,
        **kwargs,
    )
    assert replay["digest"]["market"]["cnn_fear_greed"]["value"] is None
    assert len(calls) == 1
    # Source policy changes cannot reuse the previous request/report identity.
    source_path.write_text("provider: none\n")
    changed = run_daily_pipeline(
        settings,
        mode="replay",
        manual_news=True,
        market_context_config_path=source_path,
        **kwargs,
    )
    assert changed["run_id"] != replay["run_id"]


def test_more_than_twenty_watchlist_members_reach_canonical_scan_and_digest(tmp_path):
    from uuid import uuid4

    import pyarrow as pa
    import pyarrow.compute as pc

    settings, kwargs = context(tmp_path)
    repo = ParquetRepository(settings.resolved_data_dir)
    path = kwargs["universe_paths"][0]
    payload = yaml.safe_load(path.read_text())
    member = payload["symbols"][0]
    symbols = [f"TEST{i:02}" for i in range(25)]
    payload["selection_policy"].update(minimum_size=25, maximum_size=25)
    payload["symbols"] = [member | {"symbol": symbol} for symbol in symbols]
    path.write_text(yaml.safe_dump(payload))
    history = repo.read_daily_prices()
    history = history.filter(pc.equal(history["symbol"], "AAPL"))
    run_id = uuid4()
    for symbol in symbols:
        rows = history.set_column(
            history.schema.get_field_index("symbol"),
            "symbol",
            pa.array([symbol] * history.num_rows),
        )
        rows = rows.set_column(
            rows.schema.get_field_index("ingestion_run_id"),
            "ingestion_run_id",
            pa.array([str(run_id)] * history.num_rows),
        )
        repo.write_daily_price_table(rows, run_id=run_id, batch_id=symbol)
    result = run_daily_pipeline(settings, mode="replay", manual_news=True, **kwargs)
    assert result["status"] == "DEGRADED", result
    assert {row["symbol"] for row in result["digest"]["stocks"]} == set(symbols)
    assert all(row["status"] == "IN_SCOPE" for row in result["digest"]["coverage"])
    report = json.loads(Path(result["report_path"]).read_text())
    premarket = json.loads(Path(report["components"]["premarket"]["artifacts"]["json"]).read_text())
    assert premarket["metadata"]["symbol_count"] >= 25


def test_production_price_batches_share_rate_guard_but_not_failure_denominator(
    tmp_path, monkeypatch
):
    settings, kwargs = context(tmp_path)
    kwargs.pop("context_price_service")
    instances = []
    observed_guards = []

    class GuardedService(Service):
        def __init__(self, config):
            super().__init__()
            self.config = config
            self.guard = object()

        def run(self, symbols, **options):
            observed_guards.append(self.guard)
            self.requested = symbols
            return super().run(symbols, **options)

    def factory(settings, config, repo):
        service = GuardedService(config)
        instances.append(service)
        return service

    monkeypatch.setattr("quant_system.operations.pipeline.build_price_service", factory)
    result = run_daily_pipeline(settings, manual_news=True, **kwargs)
    assert result["status"] == "DEGRADED"
    assert len(instances) == 2
    assert instances[0].config.core_symbols == ("SPY", "QQQ")
    assert instances[1].config.core_symbols == ()
    assert observed_guards[0] is observed_guards[1]
    assert "^VIX" not in instances[0].requested and "^VIX" in instances[1].requested
