import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from quant_system.ingestion.market_context import parse_cnn
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository


def test_append_only_revision_selection_filters_cutoff_before_deduplication(tmp_path):
    repository = ParquetRepository(tmp_path)
    data = json.loads(Path("tests/fixtures/market_context/cnn_contract.json").read_text())
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    first = parse_cnn(data, fetched_at=now, run_id=uuid4())
    paths = repository.write_market_observations([first], run_id=first.ingestion_run_id)
    original_bytes = paths[0].read_bytes()
    data["fear_and_greed"]["score"] = 60
    second = parse_cnn(data, fetched_at=now + timedelta(hours=1), run_id=uuid4())
    repository.write_market_observations([second], run_id=second.ingestion_run_id)
    with pytest.raises(FileExistsError):
        repository.write_market_observations([first], run_id=first.ingestion_run_id)
    assert paths[0].read_bytes() == original_bytes
    assert repository.read_market_observations().num_rows == 2
    with DuckDBAnalytics(tmp_path / "db.duckdb", repository.daily_prices_root) as db:
        db.refresh_views()
        assert db.query_market_observations(cutoff_utc=now)["value"].tolist() == [42.5]
        rows = db.query_market_observations(cutoff_utc=now + timedelta(hours=2))
        assert rows["value"].tolist() == [60]
        assert rows.iloc[0].observation_date.date() == date(2026, 6, 30)
        assert db.query_market_observations(cutoff_utc=now - timedelta(seconds=1)).empty
