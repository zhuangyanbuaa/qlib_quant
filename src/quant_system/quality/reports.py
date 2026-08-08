"""Serializable quality reports for daily price updates."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID


class PipelineStatus(StrEnum):
    """Whether downstream decision workflows may safely continue."""

    SUCCESS = "SUCCESS"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class PriceUpdateReport:
    """Complete, machine-readable outcome of one update attempt."""

    run_id: UUID
    started_at_utc: datetime
    completed_at_utc: datetime
    provider: str
    provider_version: str
    expected_session: date
    status: PipelineStatus
    requested_symbols: tuple[str, ...]
    up_to_date_symbols: tuple[str, ...]
    updated_symbols: tuple[str, ...]
    unchanged_symbols: tuple[str, ...]
    empty_symbols: tuple[str, ...]
    stale_symbols: tuple[str, ...]
    missing_sessions: dict[str, int]
    failed_symbols: dict[str, str]
    warnings: dict[str, str]
    rows_written: int
    parquet_files_written: int
    provider_calls: int
    blocked_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["run_id"] = str(self.run_id)
        payload["started_at_utc"] = self.started_at_utc.isoformat()
        payload["completed_at_utc"] = self.completed_at_utc.isoformat()
        payload["expected_session"] = self.expected_session.isoformat()
        payload["status"] = self.status.value
        return payload

    def write(self, report_directory: Path) -> Path:
        """Atomically persist the report as formatted JSON."""
        report_directory.mkdir(parents=True, exist_ok=True)
        destination = report_directory / f"price-update-{self.run_id}.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(destination)
        return destination


@dataclass(frozen=True)
class NewsUpdateReport:
    """Complete, machine-readable outcome of one news/event update attempt."""

    run_id: UUID
    started_at_utc: datetime
    completed_at_utc: datetime
    status: PipelineStatus
    requested_symbols: tuple[str, ...]
    start_utc: datetime
    end_utc: datetime
    article_rows_written: int
    event_rows_written: int
    parquet_files_written: int
    sentiment_status: PipelineStatus
    sentiment_cache_hits: int
    sentiment_cache_misses: int
    sentiment_degraded_count: int
    alpha_vantage_calls: int
    sec_calls: int
    warnings: dict[str, str]
    failed_sources: dict[str, str]
    blocked_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["run_id"] = str(self.run_id)
        payload["started_at_utc"] = self.started_at_utc.isoformat()
        payload["completed_at_utc"] = self.completed_at_utc.isoformat()
        payload["start_utc"] = self.start_utc.isoformat()
        payload["end_utc"] = self.end_utc.isoformat()
        payload["status"] = self.status.value
        payload["sentiment_status"] = self.sentiment_status.value
        return payload

    def write(self, report_directory: Path) -> Path:
        """Atomically persist the report as formatted JSON."""
        report_directory.mkdir(parents=True, exist_ok=True)
        destination = report_directory / f"news-update-{self.run_id}.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(destination)
        return destination
