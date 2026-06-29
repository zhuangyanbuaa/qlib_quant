from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from quant_system.domain.models import DailyPrice
from quant_system.storage.parquet import ParquetRepository


def make_price(run_id, *, fetched_offset_days: int = 1, close: float = 101.0) -> DailyPrice:
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


def test_repository_writes_hive_partitions_and_reads_canonical_schema(tmp_path) -> None:
    run_id = uuid4()
    repository = ParquetRepository(tmp_path)

    written = repository.write_daily_prices([make_price(run_id)], run_id=run_id)
    table = repository.read_daily_prices()

    assert len(written) == 1
    assert "year=2026/month=6" in written[0].as_posix()
    assert table.num_rows == 1
    assert table["symbol"].to_pylist() == ["AAPL"]


def test_repository_refuses_to_replace_an_existing_batch(tmp_path) -> None:
    run_id = uuid4()
    repository = ParquetRepository(tmp_path)
    record = make_price(run_id)
    repository.write_daily_prices([record], run_id=run_id)

    with pytest.raises(FileExistsError, match="immutable"):
        repository.write_daily_prices([record], run_id=run_id)
