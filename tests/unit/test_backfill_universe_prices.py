
import pytest
from scripts.backfill_universe_prices import (
    CoverageSnapshot,
    build_chunks,
    estimate_runtime_seconds,
)


def snapshot() -> CoverageSnapshot:
    return CoverageSnapshot(
        expected_session="2026-07-24",
        requested_symbols=("A", "B", "C", "D", "E"),
        missing_symbols=("D", "B"),
        stale_symbols=("A", "C", "B"),
        up_to_date_symbols=("E",),
        latest_sessions={
            "A": "2026-06-26",
            "B": None,
            "C": "2026-07-02",
            "D": None,
            "E": "2026-07-24",
        },
    )


def test_build_chunks_prioritizes_missing_and_deduplicates() -> None:
    chunks = build_chunks(snapshot(), chunk_size=2)

    assert chunks == (("D", "B"), ("A", "C"))


def test_build_chunks_rejects_invalid_chunk_size() -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        build_chunks(snapshot(), chunk_size=0)


def test_estimate_runtime_includes_inter_chunk_sleep() -> None:
    estimate = estimate_runtime_seconds(
        chunk_count=3,
        sleep_seconds=45,
        seconds_per_chunk=30,
    )

    assert estimate == 180


def test_estimate_runtime_zero_without_chunks() -> None:
    assert estimate_runtime_seconds(chunk_count=0, sleep_seconds=45) == 0
