"""Incremental daily-price update orchestration."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

from quant_system.domain.clocks import NyseSessionClock
from quant_system.domain.models import DailyPrice
from quant_system.ingestion.base import DailyPriceProvider, ProviderBatchResult, ProviderError
from quant_system.ingestion.reliability import ProviderGuard, RequestBudgetExceeded
from quant_system.logging import get_logger
from quant_system.quality.reports import PipelineStatus, PriceUpdateReport
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository

logger = get_logger(__name__)


@dataclass(frozen=True)
class PriceUpdateConfig:
    """Operational limits and safety gates for daily updates."""

    bootstrap_start: date
    core_symbols: tuple[str, ...] = ("SPY", "QQQ")
    overlap_sessions: int = 2
    stale_after_sessions: int = 1
    max_failure_fraction: float = 0.10
    batch_size: int = 25
    max_workers: int = 2

    def __post_init__(self) -> None:
        if self.overlap_sessions < 0 or self.stale_after_sessions < 0:
            raise ValueError("session counts must be non-negative")
        if self.batch_size < 1 or self.max_workers < 1:
            raise ValueError("batch_size and max_workers must be positive")
        if not 0 <= self.max_failure_fraction <= 1:
            raise ValueError("max_failure_fraction must be between 0 and 1")


class PriceUpdateService:
    """Update lagging symbols while preserving last-known-good facts on failure."""

    def __init__(
        self,
        *,
        provider: DailyPriceProvider,
        guard: ProviderGuard,
        repository: ParquetRepository,
        database_path: Path,
        report_directory: Path,
        config: PriceUpdateConfig,
        clock: NyseSessionClock | None = None,
    ) -> None:
        self.provider = provider
        self.guard = guard
        self.repository = repository
        self.database_path = database_path
        self.report_directory = report_directory
        self.config = config
        self.clock = clock or NyseSessionClock()

    def run(
        self,
        symbols: tuple[str, ...],
        *,
        now_utc: datetime | None = None,
        run_id: UUID | None = None,
    ) -> tuple[PriceUpdateReport, Path]:
        """Execute one bounded update and return its persisted quality report."""
        started_at = self._normalize_now(now_utc)
        run_id = run_id or uuid4()
        provider_calls_before = self.guard.budget.used
        requested = tuple(
            sorted(
                set(symbol.upper() for symbol in symbols)
                | set(symbol.upper() for symbol in self.config.core_symbols)
            )
        )
        if not requested:
            raise ValueError("at least one symbol is required")
        expected_session = self.clock.latest_completed_session(started_at)
        logger.info(
            "price_update_started",
            extra={
                "run_id": str(run_id),
                "provider": self.provider.name,
                "expected_session": expected_session.isoformat(),
                "requested_symbol_count": len(requested),
            },
        )

        with DuckDBAnalytics(
            self.database_path,
            self.repository.daily_prices_root,
        ) as analytics:
            analytics.refresh_views()
            latest_before = analytics.latest_sessions(requested)

        up_to_date = tuple(
            symbol
            for symbol in requested
            if latest_before.get(symbol, date.min) >= expected_session
        )
        lagging = tuple(symbol for symbol in requested if symbol not in up_to_date)
        batches = self._build_batches(lagging, latest_before)
        logger.info(
            "price_update_batches_planned",
            extra={
                "run_id": str(run_id),
                "up_to_date_symbol_count": len(up_to_date),
                "lagging_symbol_count": len(lagging),
                "batch_count": len(batches),
            },
        )
        batch_results, failed = self._fetch_batches(batches, expected_session)

        warnings: dict[str, str] = {}
        empty: set[str] = set()
        provider_bars = []
        for _batch_number, result in batch_results:
            provider_bars.extend(result.bars)
            empty.update(result.empty_symbols)
            failed.update(result.errors)
            warnings.update(result.warnings)

        records: list[DailyPrice] = []
        for bar in provider_bars:
            if bar.symbol not in requested or bar.session_date > expected_session:
                continue
            available_at = self.clock.available_at_utc(bar.session_date)
            if available_at > started_at:
                warnings[bar.symbol] = "dropped_incomplete_session"
                continue
            records.append(
                DailyPrice(
                    symbol=bar.symbol,
                    timestamp_utc=self.clock.session_close_utc(bar.session_date),
                    session_date_ny=bar.session_date,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    adjusted=bar.adjusted,
                    split_factor=bar.split_factor,
                    dividend=bar.dividend,
                    source=self.provider.name,
                    source_version=self.provider.version,
                    fetched_at_utc=started_at,
                    available_at_utc=available_at,
                    ingestion_run_id=run_id,
                    is_stale=False,
                    quality_flags=bar.quality_flags,
                )
            )

        written_files: list[Path] = []
        if records:
            written_files = self.repository.write_daily_prices(
                records,
                run_id=run_id,
                batch_id="provider",
            )

        with DuckDBAnalytics(
            self.database_path,
            self.repository.daily_prices_root,
        ) as analytics:
            analytics.refresh_views()
            latest_after = analytics.latest_sessions(requested)

        updated = tuple(
            symbol
            for symbol in lagging
            if latest_after.get(symbol, date.min) > latest_before.get(symbol, date.min)
        )
        unchanged = tuple(
            symbol
            for symbol in lagging
            if symbol not in updated and symbol not in failed and symbol not in empty
        )
        missing_sessions = {
            symbol: lag
            for symbol in requested
            if (lag := self._session_lag(latest_after.get(symbol), expected_session)) > 0
        }
        stale = tuple(
            symbol
            for symbol, lag in missing_sessions.items()
            if lag > self.config.stale_after_sessions
        )
        blocked_reasons = self._blocked_reasons(
            requested=requested,
            latest_after=latest_after,
            expected_session=expected_session,
            failed=failed,
            empty=empty,
        )
        if blocked_reasons:
            status = PipelineStatus.BLOCKED
        elif failed or empty or stale or warnings:
            status = PipelineStatus.DEGRADED
        else:
            status = PipelineStatus.SUCCESS

        report = PriceUpdateReport(
            run_id=run_id,
            started_at_utc=started_at,
            completed_at_utc=datetime.now(UTC),
            provider=self.provider.name,
            provider_version=self.provider.version,
            expected_session=expected_session,
            status=status,
            requested_symbols=requested,
            up_to_date_symbols=up_to_date,
            updated_symbols=updated,
            unchanged_symbols=unchanged,
            empty_symbols=tuple(sorted(empty)),
            stale_symbols=stale,
            missing_sessions=missing_sessions,
            failed_symbols=dict(sorted(failed.items())),
            warnings=dict(sorted(warnings.items())),
            rows_written=len(records),
            parquet_files_written=len(written_files),
            provider_calls=self.guard.budget.used - provider_calls_before,
            blocked_reasons=blocked_reasons,
        )
        report_path = report.write(self.report_directory)
        logger.info(
            "price_update_completed",
            extra={
                "run_id": str(run_id),
                "status": status.value,
                "updated_symbol_count": len(updated),
                "failed_symbol_count": len(failed),
                "stale_symbol_count": len(stale),
                "rows_written": len(records),
                "report_path": str(report_path),
            },
        )
        return report, report_path

    def _build_batches(
        self,
        lagging: tuple[str, ...],
        latest_sessions: dict[str, date],
    ) -> list[tuple[int, tuple[str, ...], date]]:
        by_start: dict[date, list[str]] = defaultdict(list)
        for symbol in lagging:
            latest = latest_sessions.get(symbol)
            start = (
                self.clock.overlap_start(latest, self.config.overlap_sessions)
                if latest
                else self.config.bootstrap_start
            )
            by_start[start].append(symbol)

        batches: list[tuple[int, tuple[str, ...], date]] = []
        batch_number = 0
        for start, grouped_symbols in sorted(by_start.items()):
            for offset in range(0, len(grouped_symbols), self.config.batch_size):
                batch = tuple(grouped_symbols[offset : offset + self.config.batch_size])
                batches.append((batch_number, batch, start))
                batch_number += 1
        return batches

    def _fetch_batches(
        self,
        batches: list[tuple[int, tuple[str, ...], date]],
        expected_session: date,
    ) -> tuple[list[tuple[int, ProviderBatchResult]], dict[str, str]]:
        results: list[tuple[int, ProviderBatchResult]] = []
        failed: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            future_batches = {
                executor.submit(
                    self.guard.call,
                    lambda symbols=symbols, start=start: self.provider.fetch_daily(
                        symbols,
                        start=start,
                        end_exclusive=self.clock.exclusive_end(expected_session),
                    ),
                ): (batch_number, symbols)
                for batch_number, symbols, start in batches
            }
            for future in as_completed(future_batches):
                batch_number, symbols = future_batches[future]
                try:
                    results.append((batch_number, future.result()))
                except (ProviderError, RequestBudgetExceeded) as error:
                    reason = f"{type(error).__name__}: {error}"
                    failed.update(dict.fromkeys(symbols, reason))
        return sorted(results), failed

    def _session_lag(self, latest: date | None, expected: date) -> int:
        if latest is None:
            return len(
                self.clock.calendar.sessions_in_range(
                    self.config.bootstrap_start,
                    expected,
                )
            )
        if latest >= expected:
            return 0
        return len(self.clock.calendar.sessions_in_range(latest, expected)) - 1

    def _blocked_reasons(
        self,
        *,
        requested: tuple[str, ...],
        latest_after: dict[str, date],
        expected_session: date,
        failed: dict[str, str],
        empty: set[str],
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        for symbol in self.config.core_symbols:
            if symbol in failed:
                reasons.append(f"core_symbol_failed:{symbol}")
            elif symbol in empty:
                reasons.append(f"core_symbol_empty:{symbol}")
            elif latest_after.get(symbol) != expected_session:
                reasons.append(f"core_symbol_not_current:{symbol}")
        failed_or_empty = set(failed) | empty
        failure_fraction = len(failed_or_empty) / len(requested)
        if failure_fraction > self.config.max_failure_fraction:
            reasons.append(
                f"failure_fraction_exceeded:{len(failed_or_empty)}/{len(requested)}"
            )
        return tuple(reasons)

    @staticmethod
    def _normalize_now(now_utc: datetime | None) -> datetime:
        value = now_utc or datetime.now(UTC)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now_utc must be timezone-aware")
        return value.astimezone(UTC)
