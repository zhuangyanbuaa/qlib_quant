from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from quant_system.domain.companies import ReviewedCompanyMapping
from quant_system.ingestion.config import load_news_source_settings
from quant_system.ingestion.source_health import check_sources, effective_companies
from quant_system.settings import Settings
from quant_system.storage.parquet import ParquetRepository


def mapping(**changes):
    now = datetime(2026, 10, 13, tzinfo=UTC)
    return ReviewedCompanyMapping(
        **(
            dict(
                mapping_id="AAPL",
                symbol="AAPL",
                cik="0000320193",
                aliases=("Apple",),
                source="review",
                source_version="fixture-1",
                source_url="https://example.com/identity",
                fetched_at_utc=now,
                available_at_utc=now,
                ingestion_run_id=uuid4(),
            )
            | changes
        )
    )


def test_readiness_is_redacted_and_reports_missing_mappings(tmp_path):
    config = load_news_source_settings(Path("configs/sources/news.yaml"))
    settings = Settings(
        _env_file=None,
        alpha_vantage_api_key="secret-fixture-key",
        sec_user_agent="app real@fixture.org",
    )
    result = check_sources(
        settings,
        config,
        ParquetRepository(tmp_path),
        ("AAPL", "MISSING"),
        cutoff_utc=datetime(2026, 10, 13, tzinfo=UTC),
    )
    assert "secret-fixture-key" not in str(result)
    assert "real@fixture.org" not in str(result)
    assert result["sources"]["news"]["entitlement"] == "UNVERIFIED"
    assert result["unmapped_symbols"] == ["MISSING"]
    missing = check_sources(
        Settings(_env_file=None),
        config,
        ParquetRepository(tmp_path),
        ("AAPL",),
        cutoff_utc=datetime(2026, 10, 13, tzinfo=UTC),
    )
    assert missing["sources"]["news"]["configured"] is False
    assert missing["sources"]["sec"]["configured"] is False


def test_mapping_corrections_preserve_visibility_and_raw_files(tmp_path):
    repository = ParquetRepository(tmp_path)
    config = load_news_source_settings(Path("configs/sources/news.yaml"))
    fact = mapping()
    files = repository.write_company_mappings((fact,), run_id=fact.ingestion_run_id)
    before = files[0].read_bytes()
    later = mapping(
        cik="1234567890",
        available_at_utc=fact.available_at_utc + timedelta(days=1),
        fetched_at_utc=fact.fetched_at_utc + timedelta(days=1),
    )
    repository.write_company_mappings((later,), run_id=later.ingestion_run_id)
    initial = effective_companies(config, repository, cutoff_utc=fact.available_at_utc)
    current = effective_companies(config, repository, cutoff_utc=later.available_at_utc)
    assert next(row for row in initial if row.symbol == "AAPL").cik == "0000320193"
    assert next(row for row in current if row.symbol == "AAPL").cik == "1234567890"
    assert files[0].read_bytes() == before
    with pytest.raises(ValueError):
        mapping(cik="not-a-cik")


def test_rename_prefers_explicit_mapping_and_stale_revision_blocks_old_alias_fallback(tmp_path):
    from quant_system.domain.securities import SecurityLifecycleEvent
    from quant_system.sentiment.mapping import CompanyMapping

    repository = ParquetRepository(tmp_path)
    now = datetime(2026, 10, 13, tzinfo=UTC)
    config = load_news_source_settings(Path("configs/sources/news.yaml")).model_copy(
        update={"companies": (CompanyMapping(symbol="OLD", cik="123"),)}
    )
    event = SecurityLifecycleEvent(
        event_id="rename",
        security_id="local:OLD",
        symbol="OLD",
        event_type="SYMBOL_CHANGE",
        successor_symbol="NEW",
        effective_session="2026-10-12",
        source="exchange",
        source_version="fixture-1",
        source_url="https://example.com/notice",
        fetched_at_utc=now,
        available_at_utc=now,
        ingestion_run_id=uuid4(),
    )
    repository.write_security_lifecycle((event,), run_id=event.ingestion_run_id)
    renamed = effective_companies(config, repository, cutoff_utc=now)
    assert [(row.symbol, row.cik) for row in renamed] == [("NEW", "123")]
    reviewed = mapping(symbol="NEW", mapping_id="new", cik="456")
    repository.write_company_mappings((reviewed,), run_id=reviewed.ingestion_run_id)
    assert effective_companies(config, repository, cutoff_utc=now)[0].cik == "0000000456"
    stale = mapping(
        symbol="NEW",
        mapping_id="new-stale",
        is_stale=True,
        cik="456",
        fetched_at_utc=now + timedelta(seconds=1),
        available_at_utc=now + timedelta(seconds=1),
    )
    repository.write_company_mappings((stale,), run_id=stale.ingestion_run_id)
    assert not effective_companies(config, repository, cutoff_utc=stale.available_at_utc)
