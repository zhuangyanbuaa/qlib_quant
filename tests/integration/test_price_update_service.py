from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from quant_system.domain.clocks import NyseSessionClock
from quant_system.domain.models import DailyPrice
from quant_system.ingestion.base import (
    ProviderBar,
    ProviderBatchResult,
    TransientProviderError,
)
from quant_system.ingestion.prices import PriceUpdateConfig, PriceUpdateService
from quant_system.ingestion.reliability import (
    DailyCallBudget,
    ProviderGuard,
    RetryPolicy,
    SlidingWindowRateLimiter,
)
from quant_system.quality.reports import PipelineStatus
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository

NOW = datetime(2026, 6, 30, 12, tzinfo=UTC)


class FakeProvider:
    name = "fake"
    version = "1"

    def __init__(self, result=None, error=None) -> None:
        self.result = result or ProviderBatchResult()
        self.error = error
        self.calls: list[tuple[tuple[str, ...], date, date]] = []

    def fetch_daily(self, symbols, *, start, end_exclusive):
        self.calls.append((symbols, start, end_exclusive))
        if self.error:
            raise self.error
        return self.result


def guard(maximum_calls: int = 10) -> ProviderGuard:
    return ProviderGuard(
        budget=DailyCallBudget(maximum_calls),
        limiter=SlidingWindowRateLimiter(1_000),
        retry=RetryPolicy(attempts=1),
        sleeper=lambda _seconds: None,
    )


def provider_bar(symbol: str, session: date, close: float) -> ProviderBar:
    return ProviderBar(
        symbol=symbol,
        session_date=session,
        open=100.0,
        high=max(102.0, close),
        low=99.0,
        close=close,
        volume=1_000,
        adjusted=True,
    )


def seed_price(repository: ParquetRepository, symbol: str, session: date) -> None:
    clock = NyseSessionClock()
    run_id = uuid4()
    close = clock.session_close_utc(session)
    repository.write_daily_prices(
        [
            DailyPrice(
                symbol=symbol,
                timestamp_utc=close,
                session_date_ny=session,
                open=100.0,
                high=102.0,
                low=99.0,
                close=101.0,
                volume=1_000,
                adjusted=True,
                source="seed",
                source_version="1",
                fetched_at_utc=close + timedelta(days=1),
                available_at_utc=close + timedelta(minutes=30),
                ingestion_run_id=run_id,
            )
        ],
        run_id=run_id,
        batch_id=f"seed-{symbol}",
    )


def service(
    tmp_path: Path,
    repository: ParquetRepository,
    provider: FakeProvider,
    *,
    core_symbols: tuple[str, ...],
    max_failure_fraction: float = 1,
) -> PriceUpdateService:
    return PriceUpdateService(
        provider=provider,
        guard=guard(),
        repository=repository,
        database_path=tmp_path / "db" / "analytics.duckdb",
        report_directory=tmp_path / "reports",
        config=PriceUpdateConfig(
            bootstrap_start=date(2026, 1, 1),
            core_symbols=core_symbols,
            overlap_sessions=1,
            stale_after_sessions=1,
            max_failure_fraction=max_failure_fraction,
            batch_size=10,
            max_workers=1,
        ),
    )


def test_incremental_update_uses_overlap_and_advances_latest_session(tmp_path) -> None:
    repository = ParquetRepository(tmp_path)
    seed_price(repository, "AAPL", date(2026, 6, 26))
    provider = FakeProvider(
        ProviderBatchResult(
            bars=(
                provider_bar("AAPL", date(2026, 6, 26), 101.5),
                provider_bar("AAPL", date(2026, 6, 29), 103.0),
            )
        )
    )

    report, report_path = service(
        tmp_path,
        repository,
        provider,
        core_symbols=("AAPL",),
    ).run(("AAPL",), now_utc=NOW)

    assert report.status is PipelineStatus.SUCCESS
    assert report.updated_symbols == ("AAPL",)
    assert report.rows_written == 2
    assert report_path.exists()
    assert provider.calls == [(("AAPL",), date(2026, 6, 25), date(2026, 6, 30))]
    with DuckDBAnalytics(
        tmp_path / "db" / "analytics.duckdb",
        repository.daily_prices_root,
    ) as analytics:
        analytics.refresh_views()
        assert analytics.symbol_date_range("AAPL")[1] == date(2026, 6, 29)


def test_core_failure_blocks_without_destroying_existing_facts(tmp_path) -> None:
    repository = ParquetRepository(tmp_path)
    seed_price(repository, "SPY", date(2026, 6, 26))
    provider = FakeProvider(error=TransientProviderError("provider unavailable"))

    report, _ = service(
        tmp_path,
        repository,
        provider,
        core_symbols=("SPY",),
    ).run(("SPY",), now_utc=NOW)

    assert report.status is PipelineStatus.BLOCKED
    assert report.rows_written == 0
    assert report.blocked_reasons == ("core_symbol_failed:SPY",)
    with DuckDBAnalytics(
        tmp_path / "db" / "analytics.duckdb",
        repository.daily_prices_root,
    ) as analytics:
        analytics.refresh_views()
        assert analytics.symbol_date_range("SPY")[1] == date(2026, 6, 26)


def test_noncore_empty_result_is_degraded_not_blocked(tmp_path) -> None:
    repository = ParquetRepository(tmp_path)
    seed_price(repository, "SPY", date(2026, 6, 29))
    provider = FakeProvider(ProviderBatchResult(empty_symbols=("ABC",)))

    report, _ = service(
        tmp_path,
        repository,
        provider,
        core_symbols=("SPY",),
    ).run(("SPY", "ABC"), now_utc=NOW)

    assert report.status is PipelineStatus.DEGRADED
    assert report.empty_symbols == ("ABC",)
    assert report.blocked_reasons == ()
    assert provider.calls[0][0] == ("ABC",)


def test_up_to_date_symbols_do_not_call_provider(tmp_path) -> None:
    repository = ParquetRepository(tmp_path)
    seed_price(repository, "AAPL", date(2026, 6, 29))
    provider = FakeProvider()

    report, _ = service(
        tmp_path,
        repository,
        provider,
        core_symbols=("AAPL",),
    ).run(("AAPL",), now_utc=NOW)

    assert report.status is PipelineStatus.SUCCESS
    assert report.up_to_date_symbols == ("AAPL",)
    assert report.provider_calls == 0
    assert provider.calls == []


def test_excessive_noncore_failures_block_the_run(tmp_path) -> None:
    repository = ParquetRepository(tmp_path)
    seed_price(repository, "SPY", date(2026, 6, 29))
    provider = FakeProvider(ProviderBatchResult(empty_symbols=("ABC", "DEF")))

    report, _ = service(
        tmp_path,
        repository,
        provider,
        core_symbols=("SPY",),
        max_failure_fraction=0.5,
    ).run(("ABC", "DEF"), now_utc=NOW)

    assert report.status is PipelineStatus.BLOCKED
    assert report.blocked_reasons == ("failure_fraction_exceeded:2/3",)


def test_unexpected_provider_bug_is_not_masked_as_data_failure(tmp_path) -> None:
    repository = ParquetRepository(tmp_path)
    provider = FakeProvider(error=ValueError("adapter bug"))

    with pytest.raises(ValueError, match="adapter bug"):
        service(
            tmp_path,
            repository,
            provider,
            core_symbols=("SPY",),
        ).run(("SPY",), now_utc=NOW)
