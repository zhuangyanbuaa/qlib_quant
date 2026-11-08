import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import yaml
from test_daily_pipeline import Service, context

from quant_system.domain.snapshots import IntradaySnapshot
from quant_system.operations.pipeline import run_daily_pipeline
from quant_system.operations.scheduling import DailySchedule
from quant_system.operations.session_jobs import run_session_job, session_windows, tick_session_jobs
from quant_system.storage.parquet import ParquetRepository


def test_local_canonical_daily_snapshot_gate_and_weekly_smoke(tmp_path):
    settings, kwargs = context(tmp_path)
    settings = settings.model_copy(
        update={"telegram_bot_token": "123456:" + ("fixture_token_" * 3), "telegram_chat_id": "42"}
    )
    # SecretStr must remain typed when updating a model in this isolated fixture.
    from pydantic import SecretStr

    settings = settings.model_copy(
        update={"telegram_bot_token": SecretStr(settings.telegram_bot_token)}
    )
    before = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in settings.resolved_data_dir.rglob("*.parquet")
    }
    broker = tmp_path / "broker.json"
    source = tmp_path / "intraday.yaml"
    source.write_text(yaml.safe_dump({"provider": "broker_file", "broker_path": str(broker)}))
    config = DailySchedule(
        session_jobs=("premarket", "preopen", "open30", "open60", "weekly"),
        intraday_config_path=source,
    )
    calls = []

    class Notifier:
        def send(self, chat, text):
            calls.append(text)
            return len(calls)

    prices = Service()
    news = Service(crash=True)

    def pipeline(settings, **options):
        return run_daily_pipeline(
            settings,
            universe_paths=kwargs["universe_paths"],
            price_service=prices,
            context_price_service=kwargs["context_price_service"],
            news_service=news,
            **options,
        )

    def executor(settings, config, window, **options):
        return run_session_job(settings, config, window, pipeline=pipeline, **options)

    now = datetime(2026, 7, 1, 12, 30, tzinfo=UTC)
    tick_session_jobs(settings, config, now_utc=now, executor=executor, client=Notifier())
    assert len(prices.calls) == 1 and not news.calls
    # A separate explicit plan fixture exercises pending manual-news gates.
    # The canonical daily report and scores generated above remain untouched.
    plan_dir = settings.resolved_data_dir / "reports/daily/2026-06-30" / str(uuid4())
    plan_dir.mkdir(parents=True)
    (plan_dir / "premarket.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "signal_session": "2026-06-30",
                    "earliest_order_session": "2026-07-01",
                    "generated_at_utc": "2026-07-01T12:40:00+00:00",
                    "news_cutoff_utc": "2026-07-01T12:40:00+00:00",
                },
                "candidates": [
                    {
                        "symbol": "AAPL",
                        "signal_id": "manual-pending-fixture",
                        "recommended_action": "PREPARE_MANUAL_CONDITIONAL_ORDER",
                        "news_risk": "UNKNOWN",
                        "limit_price": 100,
                        "max_gap_up_price": 103,
                        "gap_down_cancel_below": 95,
                    }
                ],
            }
        )
    )
    for _kind, due in [
        ("preopen", datetime(2026, 7, 1, 13, 20, tzinfo=UTC)),
        ("open30", datetime(2026, 7, 1, 14, tzinfo=UTC)),
        ("open60", datetime(2026, 7, 1, 14, 30, tzinfo=UTC)),
    ]:
        snapshot_at = due - timedelta(minutes=2)
        fact = IntradaySnapshot(
            symbol="AAPL",
            last_price=101,
            quote_at_utc=snapshot_at - timedelta(seconds=1),
            session_date_ny=date(2026, 7, 1),
            source="broker_fixture",
            source_version="1",
            fetched_at_utc=snapshot_at,
            available_at_utc=snapshot_at,
            ingestion_run_id=uuid4(),
            news_risk="LOW",
            liquidity_status="OK",
        )
        broker.write_text(json.dumps({"rows": [fact.model_dump(mode="json")]}))
        tick_session_jobs(
            settings, config, now_utc=snapshot_at, executor=executor, client=Notifier()
        )
        tick_session_jobs(settings, config, now_utc=due, executor=executor, client=Notifier())
        tick_session_jobs(settings, config, now_utc=due, executor=executor, client=Notifier())
        # Discover by gate metadata because the canonical preopen stem is versioned independently.
        gates = []
        for path in (settings.resolved_data_dir / "reports/daily/2026-06-30").glob("*/*.json"):
            payload = json.loads(path.read_text())
            if payload.get("metadata", {}).get("gate_name"):
                gates.append(payload)
        assert gates and all(report["counts"]["keep_count"] == 0 for report in gates)
        assert any(report["counts"]["defer_count"] == 1 for report in gates)
    assert ParquetRepository(settings.resolved_data_dir).read_intraday_snapshots().num_rows == 3
    weekly = next(
        w
        for w in session_windows(config, datetime(2026, 7, 2, 21, tzinfo=UTC))
        if w["kind"] == "weekly" and w["signal_session"] == date(2026, 7, 2)
    )
    output = run_session_job(settings, config, weekly, now_utc=datetime(2026, 7, 2, 21, tzinfo=UTC))
    assert output["summary"]["daily_report_count"] == 1
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == value for p, value in before.items())
    assert len(calls) == 4  # one premarket and one per gate; snapshot collection stays local
