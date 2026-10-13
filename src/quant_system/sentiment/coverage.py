"""Source availability is independent of the severity of observed news."""

from datetime import datetime, timedelta

from quant_system.domain.coverage import NewsCoverageObservation
from quant_system.storage.parquet import ParquetRepository


def load_coverage(repository: ParquetRepository) -> tuple[NewsCoverageObservation, ...]:
    return tuple(
        NewsCoverageObservation.model_validate(row)
        for row in repository.read_news_coverage().to_pylist()
    )


def news_health(assessments) -> dict:
    if assessments is None:
        return {"status": "DISABLED", "symbol_count": 0, "current_symbol_count": 0}
    current = sum(assessment.coverage_current for assessment in assessments.values())
    return {
        "status": "SUCCESS" if current == len(assessments) else "DEGRADED",
        "symbol_count": len(assessments),
        "current_symbol_count": current,
        "symbols": {
            symbol: {
                "coverage_status": value.coverage_status,
                "news_risk": value.risk_level,
                "source_coverage": value.source_coverage,
            }
            for symbol, value in assessments.items()
        },
    }


def assess_coverage(
    observations: tuple[NewsCoverageObservation, ...],
    *,
    symbol: str,
    cutoff_utc: datetime,
    lookback_hours: int,
    required_channels: tuple[str, ...] = ("news", "sec"),
    max_age: timedelta = timedelta(hours=1),
) -> dict:
    if cutoff_utc.tzinfo is None or cutoff_utc.utcoffset() is None or lookback_hours <= 0:
        raise ValueError("coverage needs an aware cutoff and positive lookback")
    start = cutoff_utc - timedelta(hours=lookback_hours)
    channels = {}
    for channel in required_channels:
        visible = [
            fact
            for fact in observations
            if fact.symbol == symbol.upper()
            and fact.channel == channel
            and fact.available_at_utc <= cutoff_utc
        ]
        latest = max(
            visible,
            key=lambda fact: (fact.available_at_utc, fact.fetched_at_utc, fact.coverage_id),
            default=None,
        )
        status = "NOT_QUERIED"
        if latest:
            if latest.is_stale or cutoff_utc - latest.window_end_utc > max_age:
                status = "STALE"
            elif latest.result not in {"SUCCESS", "EMPTY"}:
                status = latest.result
            elif latest.window_start_utc > start or any(
                flag == "quality_blocked" or flag.startswith("invalid_")
                for flag in latest.quality_flags
            ):
                status = "PARTIAL"
            else:
                status = "CURRENT"
        channels[channel] = {
            "status": status,
            "observation": latest.model_dump(mode="json") if latest else None,
        }
    statuses = {value["status"] for value in channels.values()}
    status = next(
        (
            candidate
            for candidate in ("FAILED", "UNAVAILABLE", "PARTIAL", "STALE", "NOT_QUERIED")
            if candidate in statuses
        ),
        "CURRENT",
    )
    return {"status": status, "channels": channels, "max_age_seconds": max_age.total_seconds()}
