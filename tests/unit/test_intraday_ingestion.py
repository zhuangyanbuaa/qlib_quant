import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from quant_system.domain.snapshots import IntradaySnapshot
from quant_system.ingestion.intraday import (
    IntradaySourceSettings,
    IntradayUnavailable,
    collect_intraday,
    parse_alpaca_trades,
    select_intraday,
)
from quant_system.settings import Settings
from quant_system.storage.parquet import ParquetRepository

NOW = datetime(2026, 10, 13, 13, 58, tzinfo=UTC)
SESSION = date(2026, 10, 13)


def snapshot(now=NOW, **kwargs):
    return IntradaySnapshot(
        symbol="AAPL",
        last_price=100,
        quote_at_utc=now - timedelta(seconds=10),
        session_date_ny=SESSION,
        source="broker_fixture",
        source_version="1",
        fetched_at_utc=now,
        available_at_utc=now,
        ingestion_run_id=uuid4(),
        news_risk="LOW",
        liquidity_status="OK",
        **kwargs,
    )


def test_forward_import_preserves_origin_and_append_only_pit(tmp_path):
    path = tmp_path / "broker.json"
    origin = snapshot()
    path.write_text(json.dumps({"rows": [origin.model_dump(mode="json")]}))
    settings = Settings(_env_file=None, data_dir=tmp_path)
    repo = ParquetRepository(tmp_path)
    config = IntradaySourceSettings(provider="broker_file", broker_path=path)
    result = collect_intraday(
        settings,
        config,
        repo,
        symbols=["AAPL"],
        session=SESSION,
        now_utc=NOW + timedelta(seconds=1),
    )
    assert result["status"] == "SUCCESS"
    files = {
        p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (tmp_path / "raw").rglob("*.parquet")
    }
    assert not select_intraday(repo, session=SESSION, cutoff=NOW)
    row = select_intraday(repo, session=SESSION, cutoff=NOW + timedelta(seconds=1))[0]
    assert json.loads(row["note"])["origin"]["source_version"] == "1"
    assert row["available_at_utc"] == NOW + timedelta(seconds=1)
    collect_intraday(
        settings,
        config,
        repo,
        symbols=["AAPL"],
        session=SESSION,
        now_utc=NOW + timedelta(seconds=2),
    )
    assert repo.read_intraday_snapshots().num_rows == 2
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == value for p, value in files.items())


@pytest.mark.parametrize("case", ["future", "duplicate", "wrong_session", "malformed"])
def test_invalid_broker_input_writes_no_facts(tmp_path, case):
    fact = snapshot().model_dump(mode="json")
    if case == "future":
        fact["fetched_at_utc"] = "2026-10-13T14:00:00+00:00"
    if case == "wrong_session":
        fact["session_date_ny"] = "2026-10-12"
    rows = [fact, fact] if case == "duplicate" else [fact]
    path = tmp_path / "broker.json"
    path.write_text(json.dumps({"rows": rows}) if case != "malformed" else "{}")
    result = collect_intraday(
        Settings(_env_file=None, data_dir=tmp_path),
        IntradaySourceSettings(provider="broker_file", broker_path=path),
        ParquetRepository(tmp_path),
        symbols=["AAPL"],
        session=SESSION,
        now_utc=NOW,
    )
    assert result["status"] == "UNAVAILABLE" and not list(tmp_path.rglob("*.parquet"))


def test_versioned_alpaca_fixture_is_conservative():
    facts = parse_alpaca_trades(
        {
            "trades": {
                "AAPL": {
                    "t": "2026-10-13T13:57:59Z",
                    "p": 100,
                    "s": 10,
                    "i": 5,
                    "x": "V",
                    "c": ["@"],
                }
            }
        },
        symbols=["AAPL"],
        session=SESSION,
        fetched_at=NOW,
        run_id=uuid4(),
        config=IntradaySourceSettings(),
    )
    assert facts[0].source_version == "latest-trades-v1"
    assert facts[0].news_risk == facts[0].liquidity_status == "UNKNOWN"
    assert "partial_exchange_coverage" in facts[0].quality_flags
    with pytest.raises(IntradayUnavailable):
        parse_alpaca_trades(
            {"trades": {"AAPL": {"t": "2026-10-13T14:00:00Z", "p": 100, "s": 10}}},
            symbols=["AAPL"],
            session=SESSION,
            fetched_at=NOW,
            run_id=uuid4(),
            config=IntradaySourceSettings(),
        )


def test_alpaca_uses_bounded_durable_budget(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        alpaca_api_key=SecretStr("secret-fixture"),
        alpaca_secret_key=SecretStr("other-fixture"),
    )
    calls = []

    def transport(*args):
        calls.append(True)
        return {"trades": {"AAPL": {"t": "2026-10-13T13:57:59Z", "p": 100, "s": 10}}}

    kwargs = dict(symbols=["AAPL"], session=SESSION, now_utc=NOW, transport=transport)
    for _ in range(2):
        result = collect_intraday(
            settings,
            IntradaySourceSettings(provider="alpaca", daily_call_budget=1),
            ParquetRepository(tmp_path),
            **kwargs,
        )
    assert len(calls) == 1 and result["status"] == "UNAVAILABLE"
    assert "secret-fixture" not in json.dumps(result)


def test_unconfigured_source_makes_zero_requests(tmp_path):
    calls = []
    result = collect_intraday(
        Settings(_env_file=None, data_dir=tmp_path),
        IntradaySourceSettings(),
        ParquetRepository(tmp_path),
        symbols=["AAPL"],
        session=SESSION,
        now_utc=NOW,
        transport=lambda *args: calls.append(True),
    )
    assert result["reason"] == "intraday_source_not_configured" and not calls


def test_minute_limits_survive_process_style_reconstruction(tmp_path):
    from quant_system.ingestion.budgets import SQLiteMinuteRateLimit
    from quant_system.ingestion.reliability import RequestBudgetExceeded

    instant = [NOW]
    for _ in range(2):
        limiter = SQLiteMinuteRateLimit(
            1, database_path=tmp_path / "ops.sqlite", provider="fixture", clock=lambda: instant[0]
        )
        if _ == 0:
            limiter.acquire()
        else:
            with pytest.raises(RequestBudgetExceeded):
                limiter.acquire()
    instant[0] += timedelta(seconds=60)
    limiter.acquire()


def test_duckdb_rebuild_and_parquet_select_before_revision(tmp_path):
    from quant_system.storage.duckdb import DuckDBAnalytics

    repo = ParquetRepository(tmp_path)
    first = snapshot()
    second = snapshot(NOW + timedelta(minutes=1))
    repo.write_intraday_snapshots([first], run_id=first.ingestion_run_id)
    repo.write_intraday_snapshots([second], run_id=second.ingestion_run_id)
    with DuckDBAnalytics(tmp_path / "db/analytics.duckdb", repo.daily_prices_root) as analytics:
        analytics.refresh_views()
        frame = analytics.query_intraday_snapshots(session=SESSION, cutoff_utc=NOW)
    assert frame.iloc[0]["ingestion_run_id"] == str(first.ingestion_run_id)
    assert select_intraday(repo, session=SESSION, cutoff=NOW)[0]["ingestion_run_id"] == str(
        first.ingestion_run_id
    )


def test_missing_alpaca_quote_timezone_is_invalid():
    with pytest.raises(IntradayUnavailable):
        parse_alpaca_trades(
            {"trades": {"AAPL": {"t": "2026-10-13T13:57:59", "p": 100, "s": 10}}},
            symbols=["AAPL"],
            session=SESSION,
            fetched_at=NOW,
            run_id=uuid4(),
            config=IntradaySourceSettings(),
        )


def test_provider_failure_keeps_successful_first_batch(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        alpaca_api_key="fixture_key",
        alpaca_secret_key="fixture_secret",
    )

    def transport(symbols, *args):
        if symbols == ("MSFT",):
            raise IntradayUnavailable("alpaca_http_429")
        return {"trades": {"AAPL": {"t": "2026-10-13T13:57:59Z", "p": 100, "s": 10}}}

    repo = ParquetRepository(tmp_path)
    result = collect_intraday(
        settings,
        IntradaySourceSettings(provider="alpaca", batch_size=1),
        repo,
        symbols=["AAPL", "MSFT"],
        session=SESSION,
        now_utc=NOW,
        transport=transport,
    )
    assert result["status"] == "DEGRADED" and result["rows_written"] == 1
    assert repo.read_intraday_snapshots().num_rows == 1
