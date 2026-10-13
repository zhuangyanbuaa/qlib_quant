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
    assert set(first["stages"]) == {"readiness", "prices", "news", "quality", "report"}
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
    assert not (settings.resolved_data_dir / "reports/daily").exists()


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
