import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest
import yaml

from quant_system.backtest.engine import BacktestEngine
from quant_system.backtest.workflow import load_feature_history, scan_workflow
from quant_system.decision.paper import _gate_decisions, _paper_entry_row
from quant_system.decision.premarket import run_premarket_workflow
from quant_system.decision.research import build_research_rows
from quant_system.decision.workbench import run_daily_workbench_workflow
from quant_system.domain.clocks import NyseSessionClock
from quant_system.domain.coverage import NewsCoverageObservation
from quant_system.domain.models import DailyPrice, NewsArticle
from quant_system.domain.securities import SecurityLifecycleEvent
from quant_system.features.point_in_time import build_point_in_time_features
from quant_system.features.technical import TechnicalFeatureConfig, build_technical_features
from quant_system.quality.prices import PriceDataBlocked
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.strategy.buy_the_dip import BuyTheDipStrategy
from quant_system.strategy.config import load_buy_the_dip_config
from quant_system.strategy.leverage_overlay import (
    load_leverage_overlay_config,
    load_leverage_overlay_universe_config,
)


def price(symbol, session, close, *, available=None, stale=False) -> DailyPrice:
    clock = NyseSessionClock()
    available = available or clock.available_at_utc(session)
    return DailyPrice(
        symbol=symbol,
        session_date_ny=session,
        timestamp_utc=clock.session_close_utc(session),
        open=close,
        high=close + 2,
        low=close - 2,
        close=close,
        volume=1_000_000,
        adjusted=True,
        source="fixture",
        source_version="1",
        available_at_utc=available,
        fetched_at_utc=available,
        ingestion_run_id=uuid4(),
        is_stale=stale,
    )


def seed(repository, records) -> None:
    run_id = uuid4()
    repository.write_daily_prices(
        [record.model_copy(update={"ingestion_run_id": run_id}) for record in records],
        run_id=run_id,
    )


def seed_smoke_prices(repository):
    clock = NyseSessionClock()
    sessions = clock.calendar.sessions_in_range("2025-03-03", "2026-06-30")[-300:]
    records = []
    for index, session in enumerate(sessions):
        close = 100 + index * 0.3
        if index == len(sessions) - 2:
            close = 174
        elif index == len(sessions) - 1:
            close = 182
        records.append(price("AAPL", session.date(), close))
        for symbol in ("SPY", "QQQ"):
            records.append(price(symbol, session.date(), 100 + index * 0.05))
    seed(repository, records)
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    rules = config.strategy.model_copy(
        update={
            "minimum_rsi": 0,
            "maximum_rsi": 100,
            "maximum_atr_drawdown": 100,
            "minimum_relative_return_60": -0.05,
        }
    )
    return config.model_copy(update={"strategy": rules})


def test_forward_news_has_independent_cutoff_and_preserves_price_signal(tmp_path):
    repository = ParquetRepository(tmp_path / "data")
    config = seed_smoke_prices(repository)
    signal = date(2026, 6, 30)
    generated = datetime(2026, 7, 1, 12, tzinfo=UTC)
    forward = datetime(2026, 7, 1, 10, tzinfo=UTC)
    payload = yaml.safe_load(Path("configs/universe/ai_watchlist.yaml").read_text())
    payload["selection_policy"].update(minimum_size=1, maximum_size=1)
    payload["symbols"] = [
        payload["symbols"][0]
        | {"symbol": "AAPL", "start_date": "2025-03-03", "benchmark_etf": "QQQ"}
    ]
    universe = tmp_path / "universe.yaml"
    universe.write_text(yaml.safe_dump(payload))
    kwargs = dict(
        repository=repository,
        database_path=tmp_path / "analytics.duckdb",
        report_root=tmp_path / "reports",
        universe_paths=(universe,),
        signal_session=signal,
        config=config,
        include_news_risk=True,
        news_lookback_hours=72,
        generated_at_utc=generated,
    )
    facts = [
        NewsCoverageObservation(
            coverage_id=str(uuid4()),
            symbol="AAPL",
            channel=channel,
            window_start_utc=forward - timedelta(hours=72),
            window_end_utc=forward,
            result="EMPTY",
            rows_returned=0,
            source="fixture",
            source_version="1",
            available_at_utc=forward,
            fetched_at_utc=forward,
            ingestion_run_id=uuid4(),
        )
        for channel in ("news", "sec")
    ]
    repository.write_news_coverage(facts, run_id=uuid4())
    historical, _ = run_premarket_workflow(**kwargs)
    refreshed, _ = run_premarket_workflow(**kwargs, news_cutoff_utc=forward)
    assert historical["candidates"][0]["news_risk"] == "UNKNOWN"
    assert refreshed["candidates"][0]["news_risk"] == "LOW"
    assert historical["candidates"][0]["signal_id"] == refreshed["candidates"][0]["signal_id"]
    assert historical["candidates"][0]["decision_id"] != refreshed["candidates"][0]["decision_id"]
    assert historical["metadata"]["data_cutoff_utc"] == refreshed["metadata"]["data_cutoff_utc"]
    assert refreshed["metadata"]["news_mode"] == "FORWARD_REFRESH"
    article = NewsArticle(
        article_id="later-veto",
        published_at_utc=forward - timedelta(minutes=1),
        title="High risk",
        url="https://example.com/veto",
        matched_symbols=("AAPL",),
        severity="HIGH",
        source="fixture",
        source_version="1",
        available_at_utc=forward,
        fetched_at_utc=forward,
        ingestion_run_id=uuid4(),
    )
    repository.write_news_articles((article,), run_id=uuid4())
    assert run_premarket_workflow(**kwargs, news_cutoff_utc=forward)[0]["candidates"] == []
    assert len(run_premarket_workflow(**kwargs)[0]["candidates"]) == 1
    with pytest.raises(ValueError):
        run_premarket_workflow(**kwargs, news_cutoff_utc=generated + timedelta(seconds=1))
    post_order = kwargs | {"generated_at_utc": datetime(2026, 7, 1, 15, tzinfo=UTC)}
    with pytest.raises(ValueError):
        run_premarket_workflow(**post_order, news_cutoff_utc=datetime(2026, 7, 1, 14, tzinfo=UTC))
    gate = tmp_path / "gate.json"
    gate.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "symbol": "AAPL",
                        "signal_id": historical["candidates"][0]["signal_id"],
                        "decision_id": historical["candidates"][0]["decision_id"],
                        "decision": "KEEP",
                    }
                ]
            }
        )
    )
    assert refreshed["candidates"][0]["decision_id"] not in _gate_decisions(gate)


def test_news_coverage_controls_recommendations_without_changing_canonical_prices(tmp_path):
    repository = ParquetRepository(tmp_path / "data")
    config = seed_smoke_prices(repository)
    session = date(2026, 6, 30)
    cutoff = NyseSessionClock().available_at_utc(session)
    payload = yaml.safe_load(Path("configs/universe/ai_watchlist.yaml").read_text())
    payload["selection_policy"].update(minimum_size=1, maximum_size=1)
    payload["symbols"] = [
        payload["symbols"][0]
        | {"symbol": "AAPL", "start_date": "2025-03-03", "benchmark_etf": "QQQ"}
    ]
    universe = tmp_path / "universe.yaml"
    universe.write_text(yaml.safe_dump(payload))
    kwargs = dict(
        repository=repository,
        database_path=tmp_path / "analytics.duckdb",
        report_root=tmp_path / "reports",
        universe_paths=(universe,),
        signal_session=session,
        config=config,
        include_news_risk=True,
        news_lookback_hours=72,
    )
    initial, artifact = run_premarket_workflow(**kwargs)
    assert initial["data_health"]["news"]["status"] == "DEGRADED"
    assert initial["candidates"][0]["news_risk"] == "UNKNOWN"
    assert not initial["candidates"][0]["execution_eligible"]
    assert initial["candidates"][0]["recommended_action"] == "WATCH_ONLY_NEWS_COVERAGE"
    assert initial["portfolio_posture"]["status"] == "RESEARCH_ONLY_NEWS_UNAVAILABLE"
    assert build_research_rows(initial)[0]["research_bucket"] == "BLOCKED_BUT_INTERESTING"
    bypass = initial["candidates"][0] | {"recommended_action": "PREPARE_MANUAL_CONDITIONAL_ORDER"}
    paper = _paper_entry_row(
        bypass,
        gate_decision="KEEP",
        bar={"open": 182},
        target_session=date(2026, 7, 1),
        config=config,
        existing_signals=set(),
    )
    assert paper["paper_action"] == "SKIP"
    assert paper["reason"] == "unverified_news_coverage"
    assert json.loads(artifact.json_path.read_text())["data_health"] == initial["data_health"]
    facts = [
        NewsCoverageObservation(
            coverage_id=str(uuid4()),
            symbol="AAPL",
            channel=channel,
            window_start_utc=cutoff - timedelta(hours=72),
            window_end_utc=cutoff - timedelta(seconds=1),
            result="EMPTY",
            rows_returned=0,
            source="fixture",
            source_version="1/coverage-1",
            available_at_utc=cutoff - timedelta(seconds=1),
            fetched_at_utc=cutoff - timedelta(seconds=1),
            ingestion_run_id=uuid4(),
        )
        for channel in ("news", "sec")
    ]
    late = [
        fact.model_copy(
            update={
                "available_at_utc": cutoff + timedelta(seconds=1),
                "fetched_at_utc": cutoff + timedelta(seconds=1),
            }
        )
        for fact in facts
    ]
    repository.write_news_coverage(late, run_id=uuid4())
    invisible, _ = run_premarket_workflow(**kwargs)
    assert invisible["candidates"][0]["news_risk"] == "UNKNOWN"
    repository.write_news_coverage(facts, run_id=uuid4())
    current, _ = run_premarket_workflow(**kwargs)
    assert current["data_health"]["news"]["status"] == "SUCCESS"
    assert current["candidates"][0]["news_risk"] == "LOW"
    assert current["candidates"][0]["recommended_action"] == "PREPARE_MANUAL_CONDITIONAL_ORDER"
    assert current["candidates"][0]["signal_close"] == initial["candidates"][0]["signal_close"]
    failed = facts[0].model_copy(
        update={
            "coverage_id": "failed-query",
            "result": "FAILED",
            "available_at_utc": cutoff,
            "fetched_at_utc": cutoff,
        }
    )
    repository.write_news_coverage((failed,), run_id=uuid4())
    article = NewsArticle(
        article_id="high-risk",
        published_at_utc=cutoff - timedelta(hours=1),
        title="High risk",
        url="https://example.com/risk",
        matched_symbols=("AAPL",),
        severity="HIGH",
        source="fixture",
        source_version="1",
        available_at_utc=cutoff - timedelta(hours=1),
        fetched_at_utc=cutoff,
        ingestion_run_id=uuid4(),
    )
    repository.write_news_articles((article,), run_id=uuid4())
    vetoed, _ = run_premarket_workflow(**kwargs)
    assert vetoed["candidates"] == []


def test_known_suspension_blocks_signal_but_late_fact_does_not_rewrite_past(tmp_path):
    repository = ParquetRepository(tmp_path)
    config = seed_smoke_prices(repository)
    kwargs = dict(
        repository=repository,
        database_path=tmp_path / "analytics.duckdb",
        symbols=("AAPL",),
        as_of=date(2026, 6, 30),
        config=config,
    )
    assert [signal["symbol"] for signal in scan_workflow(**kwargs)] == ["AAPL"]
    cutoff = NyseSessionClock().available_at_utc(date(2026, 6, 30))
    fact = SecurityLifecycleEvent(
        event_id="AAPL-suspension",
        security_id="local:AAPL",
        symbol="AAPL",
        event_type="SUSPENDED",
        effective_session=date(2026, 6, 30),
        source_url="https://example.com/notice",
        source="exchange",
        source_version="fixture-1",
        ingestion_run_id=uuid4(),
        fetched_at_utc=cutoff,
        available_at_utc=cutoff,
    )
    late = fact.model_copy(
        update={
            "fetched_at_utc": datetime(2026, 7, 1, tzinfo=UTC),
            "available_at_utc": datetime(2026, 7, 1, tzinfo=UTC),
        }
    )
    repository.write_security_lifecycle((late,), run_id=uuid4())
    assert [signal["symbol"] for signal in scan_workflow(**kwargs)] == ["AAPL"]
    repository.write_security_lifecycle((fact,), run_id=uuid4())
    assert scan_workflow(**kwargs) == []


def test_scan_and_backtest_share_a_nonempty_point_in_time_signal(tmp_path) -> None:
    repository = ParquetRepository(tmp_path)
    config = seed_smoke_prices(repository)
    session = date(2026, 6, 30)
    signals = scan_workflow(
        repository=repository,
        database_path=tmp_path / "analytics.duckdb",
        symbols=("AAPL",),
        as_of=session,
        config=config,
    )
    features = load_feature_history(
        repository=repository,
        database_path=tmp_path / "analytics.duckdb",
        symbols=("AAPL",),
        benchmark_symbol="QQQ",
        start=date(2026, 6, 29),
        end=session,
    )
    backtest = BacktestEngine(BuyTheDipStrategy(config.strategy), config).run(
        features,
        start=date(2026, 6, 29),
        end=session,
    )
    assert len(signals) == 1
    assert signals == [signal.model_dump(mode="json") for signal in backtest.signals]
    assert signals[0]["data_cutoff_utc"] == "2026-06-30T20:30:00Z"


@pytest.mark.parametrize("late_initial", [False, True])
def test_causal_features_match_individual_snapshots_without_future_revisions(
    tmp_path,
    late_initial,
) -> None:
    repository = ParquetRepository(tmp_path)
    sessions = [date(2026, 6, day) for day in (24, 25, 26, 29, 30)]
    records = [
        price(symbol, session, 100 + index)
        for index, session in enumerate(sessions)
        for symbol in ("AAPL", "QQQ", "SPY")
        if not (late_initial and symbol == "AAPL" and session == sessions[1])
    ]
    records.append(
        price("AAPL", sessions[1], 150, available=datetime(2026, 6, 29, 20, 30, tzinfo=UTC))
    )
    seed(repository, records)
    config = TechnicalFeatureConfig(
        fast_ma=1,
        dip_ma=4,
        trend_ma=2,
        long_ma=4,
        trend_slope_lookback=1,
        rsi_period=1,
        atr_period=1,
        adx_period=1,
        relative_return_period=1,
    )
    clock = NyseSessionClock()
    with DuckDBAnalytics(tmp_path / "analytics.duckdb", repository.daily_prices_root) as db:
        db.refresh_views()
        versions = db.query_price_versions(
            ("AAPL", "QQQ", "SPY"),
            start_date=sessions[0],
            end_date=sessions[-1],
            cutoff_utc=clock.available_at_utc(sessions[-1]),
        )
        actual = build_point_in_time_features(versions, config=config)
        for session in sessions:
            snapshot = db.query_price_history(
                ("AAPL", "QQQ", "SPY"),
                start_date=sessions[0],
                end_date=session,
                cutoff_utc=clock.available_at_utc(session),
            )
            expected = build_technical_features(snapshot, config=config)
            expected = expected.loc[expected["session_date_ny"].dt.date == session]
            got = actual.loc[actual["session_date_ny"].dt.date == session]
            columns = ["symbol", "close", "ma20", "ma200", "atr20", "rsi14", "relative_return60"]
            pd.testing.assert_frame_equal(
                got[columns].reset_index(drop=True),
                expected[columns].reset_index(drop=True),
                check_dtype=False,
            )
            assert (got["available_at_utc"] <= pd.Timestamp(clock.available_at_utc(session))).all()
    before = actual.loc[
        (actual["symbol"] == "AAPL") & (actual["session_date_ny"].dt.date == sessions[1])
    ]
    assert before.empty if late_initial else before["close"].tolist() == [101]


@pytest.mark.parametrize("issue", ["missing", "stale"])
def test_canonical_loader_blocks_missing_or_stale_critical_price(tmp_path, issue) -> None:
    repository = ParquetRepository(tmp_path)
    session = date(2026, 6, 26)
    records = [price("AAPL", session, 100), price("QQQ", session, 110)]
    if issue == "stale":
        records.append(price("SPY", session, 120, stale=True))
    seed(repository, records)
    with pytest.raises(PriceDataBlocked) as caught:
        load_feature_history(
            repository=repository,
            database_path=tmp_path / "analytics.duckdb",
            symbols=("AAPL",),
            benchmark_symbol="QQQ",
            start=session,
            end=session,
        )
    assert caught.value.report["status"] == "BLOCKED"
    assert any("SPY" in reason for reason in caught.value.report["blocked_reasons"])


def test_critical_price_available_after_cutoff_does_not_unlock_session(tmp_path) -> None:
    repository = ParquetRepository(tmp_path)
    session = date(2026, 6, 26)
    seed(
        repository,
        [
            price("AAPL", session, 100),
            price("QQQ", session, 110),
            price("SPY", session, 120, available=datetime(2026, 6, 29, 20, 30, tzinfo=UTC)),
        ],
    )
    with pytest.raises(PriceDataBlocked):
        load_feature_history(
            repository=repository,
            database_path=tmp_path / "analytics.duckdb",
            symbols=("AAPL",),
            benchmark_symbol="QQQ",
            start=session,
            end=session,
        )


@pytest.mark.parametrize("missing_core", [False, True])
def test_full_daily_workbench_preserves_smoke_and_blocks_before_reports(
    tmp_path, missing_core
) -> None:
    repository = ParquetRepository(tmp_path / "data")
    config = (
        seed_smoke_prices(repository)
        if not missing_core
        else load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    )
    universe = yaml.safe_load(Path("configs/universe/ai_watchlist.yaml").read_text())
    universe["selection_policy"].update(minimum_size=1, maximum_size=1)
    universe["symbols"] = [
        {
            **universe["symbols"][0],
            "symbol": "AAPL",
            "company_name": "Apple",
            "liquidity_tier": "mega",
            "benchmark_etf": "QQQ",
            "start_date": date(2025, 3, 3),
        }
    ]
    universe_path = tmp_path / "universe.yaml"
    universe_path.write_text(yaml.safe_dump(universe))
    kwargs = {
        "repository": repository,
        "database_path": tmp_path / "analytics.duckdb",
        "operations_database_path": tmp_path / "operations.sqlite",
        "report_root": tmp_path / "reports",
        "signal_session": date(2026, 6, 30),
        "universe_paths": (universe_path,),
        "strategy_config": config,
        "include_news_risk": False,
        "news_lookback_hours": 72,
        "benchmark_path": Path("configs/universe/benchmarks.yaml"),
        "include_model_ranking": False,
        "model_settings": None,
        "leverage_config": load_leverage_overlay_config(
            Path("configs/strategy/leverage_overlay.yaml")
        ),
        "leverage_universe": load_leverage_overlay_universe_config(
            Path("configs/universe/leverage_overlay_universe.yaml")
        ),
    }
    if missing_core:
        with pytest.raises(PriceDataBlocked):
            run_daily_workbench_workflow(**kwargs)
        assert not (tmp_path / "reports").exists()
        return
    report, artifacts = run_daily_workbench_workflow(**kwargs)
    persisted = json.loads(artifacts.json_path.read_text())
    assert persisted["metadata"]["data_cutoff_utc"] == "2026-06-30T20:30:00+00:00"
    assert persisted["summary"]["candidate_count"] == 1
    assert all(c["status"] == "COMPLETED" for c in report["components"].values())
    for component in report["components"].values():
        assert component["metadata"]["run_id"] == report["metadata"]["run_id"]


@pytest.mark.parametrize("entrypoint", ["cli", "runner"])
def test_subprocess_price_gate_propagates_blocked_status(tmp_path, entrypoint) -> None:
    command = [sys.executable]
    if entrypoint == "cli":
        command += ["-m", "quant_system", "decision", "daily-workbench"]
    else:
        command += ["scripts/run_daily_workbench.py"]
    command += ["--date", "2026-06-26", "--no-news-risk"]
    environment = {
        **os.environ,
        "QUANT_DATA_DIR": str(tmp_path),
        "PYTHONPATH": str(Path("src").resolve()),
    }
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "BLOCKED"
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "reports" / "daily").exists()
