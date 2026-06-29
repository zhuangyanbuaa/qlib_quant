from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from quant_system.domain.models import DailyPrice
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository


def make_price(run_id, *, fetched_offset_days: int, close: float) -> DailyPrice:
    bar_time = datetime(2026, 6, 26, 20, tzinfo=UTC)
    return DailyPrice(
        symbol="AAPL",
        timestamp_utc=bar_time,
        session_date_ny=date(2026, 6, 26),
        open=100.0,
        high=max(102.0, close),
        low=99.0,
        close=close,
        volume=1_000,
        adjusted=True,
        source="fixture",
        source_version="1",
        fetched_at_utc=bar_time + timedelta(days=fetched_offset_days),
        available_at_utc=bar_time + timedelta(minutes=30),
        ingestion_run_id=run_id,
    )


def test_daily_prices_view_keeps_latest_source_version(tmp_path) -> None:
    repository = ParquetRepository(tmp_path)
    first_run = uuid4()
    second_run = uuid4()
    repository.write_daily_prices(
        [make_price(first_run, fetched_offset_days=1, close=101.0)],
        run_id=first_run,
    )
    repository.write_daily_prices(
        [make_price(second_run, fetched_offset_days=2, close=103.0)],
        run_id=second_run,
    )

    with DuckDBAnalytics(
        tmp_path / "db" / "analytics.duckdb",
        repository.daily_prices_root,
    ) as analytics:
        analytics.refresh_views()
        result = analytics.query_daily_prices("aapl")

    assert len(result) == 1
    assert result.iloc[0]["close"] == 103.0


def test_views_can_bootstrap_before_parquet_exists(tmp_path) -> None:
    with DuckDBAnalytics(
        tmp_path / "db" / "analytics.duckdb",
        tmp_path / "raw" / "prices",
    ) as analytics:
        analytics.refresh_views()

        assert analytics.query_daily_prices("AAPL").empty
