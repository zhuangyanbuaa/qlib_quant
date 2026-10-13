from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pandas as pd
import pytest

from quant_system.domain.coverage import NewsCoverageObservation
from quant_system.sentiment.coverage import assess_coverage
from quant_system.sentiment.risk import assess_news_risk
from quant_system.storage.parquet import ParquetRepository

CUTOFF = datetime(2026, 10, 12, 20, 30, tzinfo=UTC)


def observation(channel="news", **changes):
    return NewsCoverageObservation(
        **(
            dict(
                coverage_id=str(uuid4()),
                symbol="NVDA",
                channel=channel,
                window_start_utc=CUTOFF - timedelta(hours=72),
                window_end_utc=CUTOFF,
                result="EMPTY",
                rows_returned=0,
                source=f"fixture_{channel}",
                source_version="1/coverage-1",
                fetched_at_utc=CUTOFF,
                available_at_utc=CUTOFF,
                ingestion_run_id=uuid4(),
            )
            | changes
        )
    )


def test_successful_empty_coverage_is_distinct_from_never_queried():
    empty = assess_coverage((), symbol="NVDA", cutoff_utc=CUTOFF, lookback_hours=72)
    assert empty["status"] == "NOT_QUERIED"
    observations = (observation(), observation("sec"))
    covered = assess_coverage(observations, symbol="NVDA", cutoff_utc=CUTOFF, lookback_hours=72)
    assert covered["status"] == "CURRENT"
    risk = assess_news_risk(
        symbols=("NVDA",),
        articles=pd.DataFrame(),
        events=pd.DataFrame(),
        cutoff_utc=CUTOFF,
        lookback_hours=72,
        coverage=observations,
    )["NVDA"]
    assert risk.to_dict()["severity"] == "LOW"
    missing = assess_news_risk(
        symbols=("NVDA",),
        articles=pd.DataFrame(),
        events=pd.DataFrame(),
        cutoff_utc=CUTOFF,
        lookback_hours=72,
    )["NVDA"]
    assert missing.to_dict()["severity"] == "UNKNOWN"
    assert not missing.coverage_current


@pytest.mark.parametrize("result", ["FAILED", "UNAVAILABLE", "PARTIAL"])
def test_failed_unavailable_partial_are_not_current(result):
    coverage = assess_coverage(
        (observation(result=result), observation("sec")),
        symbol="NVDA",
        cutoff_utc=CUTOFF,
        lookback_hours=72,
    )
    assert coverage["status"] == result


def test_late_and_old_observations_cannot_certify_current_coverage():
    late = observation(
        fetched_at_utc=CUTOFF + timedelta(seconds=1), available_at_utc=CUTOFF + timedelta(seconds=1)
    )
    assert (
        assess_coverage((late,), symbol="NVDA", cutoff_utc=CUTOFF, lookback_hours=72)["channels"][
            "news"
        ]["status"]
        == "NOT_QUERIED"
    )
    old = observation(
        window_end_utc=CUTOFF - timedelta(hours=2),
        available_at_utc=CUTOFF - timedelta(hours=2),
        fetched_at_utc=CUTOFF - timedelta(hours=2),
    )
    assert (
        assess_coverage(
            (old, observation("sec")), symbol="NVDA", cutoff_utc=CUTOFF, lookback_hours=72
        )["status"]
        == "STALE"
    )
    short = observation(window_start_utc=CUTOFF - timedelta(hours=1))
    assert (
        assess_coverage(
            (short, observation("sec")), symbol="NVDA", cutoff_utc=CUTOFF, lookback_hours=72
        )["status"]
        == "PARTIAL"
    )


def test_coverage_parquet_is_immutable_and_keeps_lineage(tmp_path):
    repository = ParquetRepository(tmp_path)
    fact = observation()
    files = repository.write_news_coverage((fact,), run_id=fact.ingestion_run_id)
    assert repository.read_news_coverage().to_pylist()[0]["source_version"] == "1/coverage-1"
    before = files[0].read_bytes()
    with pytest.raises(FileExistsError):
        repository.write_news_coverage((fact,), run_id=fact.ingestion_run_id)
    assert files[0].read_bytes() == before
