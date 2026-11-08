import copy
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from quant_system.ingestion.market_context import (
    CNN_SOURCE_VERSION,
    MarketContextSettings,
    MarketContextUnavailable,
    collect_market_context,
    import_manual_cnn,
    parse_cnn,
    select_market_context,
)
from quant_system.settings import Settings
from quant_system.storage.parquet import ParquetRepository

NOW = datetime(2026, 7, 1, 12, tzinfo=UTC)


def payload():
    return json.loads(Path("tests/fixtures/market_context/cnn_contract.json").read_text())


def test_cnn_contract_retains_provenance_and_does_not_backdate_history():
    fact = parse_cnn(payload(), fetched_at=NOW, run_id=uuid4())
    assert fact.value == 42.5 and fact.label == "fear"
    assert fact.series_id == "CNN_FEAR_GREED"
    assert fact.source_version == CNN_SOURCE_VERSION
    assert fact.observation_date == date(2026, 6, 30)
    assert fact.available_at_utc == fact.fetched_at_utc == NOW
    assert fact.observed_at_utc < fact.available_at_utc


@pytest.mark.parametrize(
    "field,value",
    [
        ("score", None),
        ("score", -1),
        ("score", 101),
        ("score", float("nan")),
        ("score", True),
        ("score", "42.5"),
        ("timestamp", "2027-01-01T00:00:00Z"),
        ("timestamp", "2026-06-30T20:00:00"),
        ("timestamp", "not-a-date"),
    ],
)
def test_cnn_rejects_invalid_or_future_facts(field, value):
    data = payload()
    data["fear_and_greed"][field] = value
    with pytest.raises(MarketContextUnavailable):
        parse_cnn(data, fetched_at=NOW, run_id=uuid4())


def test_schema_changes_are_explicit_and_extra_fields_do_not_become_facts():
    data = payload()
    data["unknown"] = {"score": 99}
    assert parse_cnn(data, fetched_at=NOW, run_id=uuid4()).value == 42.5
    data["fear_and_greed"]["value"] = data["fear_and_greed"].pop("score")
    with pytest.raises(MarketContextUnavailable):
        parse_cnn(data, fetched_at=NOW, run_id=uuid4())


@pytest.mark.parametrize("failure", ["cnn_http_403", "cnn_http_418", "cnn_http_429", "cnn_timeout"])
def test_provider_errors_are_bounded_and_do_not_write_raw(tmp_path, failure):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    repository = ParquetRepository(tmp_path)
    calls = []

    def transport(timeout):
        calls.append(timeout)
        raise MarketContextUnavailable(failure)

    config = MarketContextSettings(provider="cnn", daily_call_budget=1, calls_per_minute=2)
    for _ in range(2):
        result = collect_market_context(
            settings, config, repository, now_utc=NOW, transport=transport
        )
        assert result["status"] == "UNAVAILABLE"
    assert len(calls) == 1
    assert not repository.read_market_observations().num_rows


def test_manual_import_requires_cnn_source_and_retains_actual_import_time(tmp_path):
    repository = ParquetRepository(tmp_path)
    data = {
        "value": 42.5,
        "observed_at_utc": "2026-06-30T20:00:00Z",
        "label": "fear",
        "source_url": "https://www.cnn.com/markets/fear-and-greed",
    }
    result = import_manual_cnn(data, repository=repository, now_utc=NOW)
    assert result["rows_written"] == 1
    raw = repository.read_market_observations().to_pylist()[0]
    assert raw["available_at_utc"] == NOW
    assert "manual_transcription" in raw["quality_flags"]
    before = select_market_context(
        repository,
        tmp_path / "analytics.duckdb",
        session=date(2026, 6, 30),
        cutoff=NOW - timedelta(seconds=1),
    )
    assert before["cnn_fear_greed"]["value"] is None
    bad = copy.deepcopy(data)
    bad["source_url"] = "https://example.com/crypto-fear-and-greed"
    with pytest.raises(ValueError):
        import_manual_cnn(bad, repository=repository, now_utc=NOW)
    assert repository.read_market_observations().num_rows == 1


def test_staleness_uses_exchange_sessions_and_no_fake_value_when_missing(tmp_path):
    repository = ParquetRepository(tmp_path)
    record = parse_cnn(payload(), fetched_at=NOW, run_id=uuid4())
    repository.write_market_observations([record], run_id=record.ingestion_run_id)
    current = select_market_context(
        repository, tmp_path / "db.duckdb", session=date(2026, 6, 30), cutoff=NOW
    )
    assert current["cnn_fear_greed"]["status"] == "AVAILABLE"
    assert current["vix"]["status"] == "UNAVAILABLE"
    old = select_market_context(
        repository, tmp_path / "db.duckdb", session=date(2026, 7, 1), cutoff=NOW + timedelta(days=1)
    )
    assert old["cnn_fear_greed"]["status"] == "STALE"
    assert old["cnn_fear_greed"]["value"] == 42.5


def test_successful_fixture_collection_and_durable_budget(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    repo = ParquetRepository(tmp_path)
    config = MarketContextSettings(provider="cnn", daily_call_budget=1, calls_per_minute=2)
    calls = []

    def transport(timeout):
        calls.append(timeout)
        return payload()

    first = collect_market_context(settings, config, repo, now_utc=NOW, transport=transport)
    second = collect_market_context(settings, config, repo, now_utc=NOW, transport=transport)
    assert first["status"] == "SUCCESS" and second["status"] == "UNAVAILABLE"
    assert first["source_version"] == CNN_SOURCE_VERSION
    assert len(calls) == repo.read_market_observations().num_rows == 1


def test_backoff_respects_retry_after_across_calls(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    repo = ParquetRepository(tmp_path)
    config = MarketContextSettings(provider="cnn", failure_backoff_seconds=60)
    calls = []

    def transport(timeout):
        calls.append(timeout)
        raise MarketContextUnavailable("cnn_http_429", retry_after_seconds=3600)

    collect_market_context(settings, config, repo, now_utc=NOW, transport=transport)
    result = collect_market_context(
        settings, config, repo, now_utc=NOW + timedelta(minutes=59), transport=transport
    )
    assert result["reason"] == "cnn_provider_backoff" and len(calls) == 1
    collect_market_context(
        settings, config, repo, now_utc=NOW + timedelta(hours=1), transport=transport
    )
    assert len(calls) == 2


def test_vix_reuses_source_metadata_and_filters_late_revisions_before_dedup(tmp_path):
    from quant_system.domain.models import DailyPrice

    repo = ParquetRepository(tmp_path)
    original = DailyPrice(
        symbol="^VIX",
        timestamp_utc=datetime(2026, 6, 30, 20, tzinfo=UTC),
        session_date_ny=date(2026, 6, 30),
        open=20,
        high=21,
        low=18,
        close=19,
        volume=0,
        adjusted=False,
        source="yahoo_finance",
        source_version="fixture-yahoo-v1",
        fetched_at_utc=NOW,
        available_at_utc=NOW,
        ingestion_run_id=uuid4(),
    )
    repo.write_daily_prices([original], run_id=original.ingestion_run_id)
    revised = original.model_copy(
        update={
            "close": 20,
            "fetched_at_utc": NOW + timedelta(hours=1),
            "available_at_utc": NOW + timedelta(hours=1),
            "ingestion_run_id": uuid4(),
        }
    )
    repo.write_daily_prices([revised], run_id=revised.ingestion_run_id)
    value = select_market_context(
        repo, tmp_path / "db.duckdb", session=date(2026, 6, 30), cutoff=NOW
    )["vix"]
    assert value["value"] == 19 and value["status"] == "AVAILABLE"
    assert value["provenance"]["source"] == "yahoo_finance"
    assert json.loads(value["provenance"]["input_reference"])["ingestion_run_id"] == str(
        original.ingestion_run_id
    )
    invalid = revised.model_copy(
        update={
            "quality_flags": ("quality_blocked",),
            "ingestion_run_id": uuid4(),
            "fetched_at_utc": NOW + timedelta(hours=2),
            "available_at_utc": NOW + timedelta(hours=2),
        }
    )
    repo.write_daily_prices([invalid], run_id=invalid.ingestion_run_id)
    value = select_market_context(
        repo, tmp_path / "db.duckdb", session=date(2026, 6, 30), cutoff=NOW + timedelta(hours=2)
    )["vix"]
    assert value["status"] == "UNAVAILABLE" and value["value"] is None


def test_holiday_weekend_does_not_make_last_session_observation_stale(tmp_path):
    repo = ParquetRepository(tmp_path)
    data = payload()
    data["fear_and_greed"]["timestamp"] = "2026-07-02T20:00:00Z"
    fetched = datetime(2026, 7, 2, 21, tzinfo=UTC)
    record = parse_cnn(data, fetched_at=fetched, run_id=uuid4())
    repo.write_market_observations([record], run_id=record.ingestion_run_id)
    cutoff = datetime(2026, 7, 5, 12, tzinfo=UTC)
    values = select_market_context(
        repo, tmp_path / "db.duckdb", session=date(2026, 7, 2), cutoff=cutoff
    )
    assert values["cnn_fear_greed"]["status"] == "AVAILABLE"


def test_quality_blocked_cnn_revision_never_falls_back_to_old_healthy_fact(tmp_path):
    repo = ParquetRepository(tmp_path)
    original = parse_cnn(payload(), fetched_at=NOW, run_id=uuid4())
    repo.write_market_observations([original], run_id=original.ingestion_run_id)
    blocked = original.model_copy(
        update={
            "quality_flags": ("quality_blocked",),
            "ingestion_run_id": uuid4(),
            "available_at_utc": NOW + timedelta(minutes=1),
            "fetched_at_utc": NOW + timedelta(minutes=1),
        }
    )
    repo.write_market_observations([blocked], run_id=blocked.ingestion_run_id)
    values = select_market_context(
        repo, tmp_path / "db.duckdb", session=date(2026, 6, 30), cutoff=NOW + timedelta(minutes=1)
    )
    assert values["cnn_fear_greed"]["status"] == "UNAVAILABLE"
    assert values["cnn_fear_greed"]["value"] is None


def test_transport_preserves_http_429_retry_after_without_exposing_body(monkeypatch):
    from urllib.error import HTTPError

    from quant_system.ingestion.market_context import cnn_transport

    class Opener:
        def open(self, request, timeout):
            raise HTTPError(
                request.full_url, 429, "private response body", {"Retry-After": "3600"}, None
            )

    monkeypatch.setattr(
        "quant_system.ingestion.market_context.build_opener", lambda *args: Opener()
    )
    with pytest.raises(MarketContextUnavailable, match=r"^cnn_http_429$") as error:
        cnn_transport(10)
    assert error.value.retry_after_seconds == 3600
