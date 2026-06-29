"""Inventory and migrate legacy yfinance CSV files into canonical Parquet."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pandas as pd
import pyarrow as pa

from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.storage.schemas import DAILY_PRICE_SCHEMA

REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume", "symbol")
PRICE_COLUMNS = ("open", "high", "low", "close")
LEGACY_SOURCE = "legacy_yfinance"
LEGACY_SOURCE_VERSION = "auto-adjust-v1"


@dataclass(frozen=True)
class LegacyFileInventory:
    """Quality and shape information for one legacy price CSV."""

    path: str
    symbol: str
    schema_id: str
    column_count: int
    row_count: int
    unique_session_count: int
    migratable_row_count: int
    duplicate_session_count: int
    min_session_date: date | None
    max_session_date: date | None
    invalid_row_count: int
    is_stale: bool
    status: str
    issues: str


@dataclass(frozen=True)
class MigrationResult:
    """Paths and counts produced by one migration run."""

    run_id: UUID
    source_file_count: int
    migrated_file_count: int
    quarantined_file_count: int
    quarantined_row_count: int
    migrated_row_count: int
    stale_symbol_count: int
    schema_report_path: Path
    comparison_report_path: Path | None
    summary_path: Path
    parquet_files: tuple[Path, ...]


def inventory_legacy_prices(
    source_directory: Path,
    *,
    stale_after_days: int = 7,
    max_invalid_fraction: float = 0.01,
) -> list[LegacyFileInventory]:
    """Inspect every CSV and classify fatal errors, warnings, and staleness."""
    if not 0 <= max_invalid_fraction <= 1:
        raise ValueError("max_invalid_fraction must be between 0 and 1")
    paths = sorted(source_directory.glob("*.csv"))
    inventory = [
        _inspect_csv(path, max_invalid_fraction=max_invalid_fraction) for path in paths
    ]
    valid_max_dates = [
        item.max_session_date
        for item in inventory
        if item.status != "error" and item.max_session_date is not None
    ]
    if not valid_max_dates:
        return inventory

    latest_session = max(valid_max_dates)
    result: list[LegacyFileInventory] = []
    for item in inventory:
        is_stale = bool(
            item.max_session_date
            and (latest_session - item.max_session_date).days > stale_after_days
        )
        issues = [issue for issue in item.issues.split(";") if issue]
        if is_stale:
            issues.append(f"stale_vs_latest_{latest_session.isoformat()}")
        status = item.status
        if status == "ok" and issues:
            status = "warning"
        result.append(
            replace(
                item,
                is_stale=is_stale,
                status=status,
                issues=";".join(issues),
            )
        )
    return result


def migrate_legacy_prices(
    source_directory: Path,
    *,
    data_root: Path,
    report_directory: Path,
    quarantine_directory: Path,
    database_path: Path,
    run_id: UUID | None = None,
    stale_after_days: int = 7,
    max_invalid_fraction: float = 0.01,
    batch_size: int = 100,
    dry_run: bool = False,
) -> MigrationResult:
    """Inventory, quarantine, migrate, and reconcile legacy CSV price data."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    run_id = run_id or uuid4()
    report_directory.mkdir(parents=True, exist_ok=True)
    inventory = inventory_legacy_prices(
        source_directory,
        stale_after_days=stale_after_days,
        max_invalid_fraction=max_invalid_fraction,
    )
    schema_report_path = report_directory / f"legacy-price-schema-{run_id}.csv"
    _write_inventory_report(inventory, schema_report_path)

    invalid = [item for item in inventory if item.status == "error"]
    valid = [item for item in inventory if item.status != "error"]
    run_quarantine_directory = quarantine_directory / str(run_id)
    for item in invalid:
        _quarantine_file(Path(item.path), run_quarantine_directory, item.issues)
    partial = [item for item in valid if item.invalid_row_count]
    for item in partial:
        _quarantine_invalid_rows(
            Path(item.path),
            run_quarantine_directory / "rows",
            item.issues,
        )

    written: list[Path] = []
    if not dry_run:
        repository = ParquetRepository(data_root)
        for batch_number, start in enumerate(range(0, len(valid), batch_size)):
            batch = valid[start : start + batch_size]
            tables = [
                normalize_legacy_price_file(
                    Path(item.path),
                    run_id=run_id,
                    is_stale=item.is_stale,
                )
                for item in batch
            ]
            combined = pa.concat_tables(tables)
            written.extend(
                repository.write_daily_price_table(
                    combined,
                    run_id=run_id,
                    batch_id=f"{batch_number:04d}",
                )
            )

    comparison_report_path: Path | None = None
    migrated_row_count = 0
    if not dry_run:
        repository = ParquetRepository(data_root)
        with DuckDBAnalytics(database_path, repository.daily_prices_root) as analytics:
            analytics.refresh_views()
            comparison = _build_comparison_report(analytics, valid)
        comparison_report_path = report_directory / f"legacy-price-comparison-{run_id}.csv"
        comparison.to_csv(comparison_report_path, index=False)
        migrated_row_count = int(comparison["migrated_rows"].sum())

    summary_path = report_directory / f"legacy-price-summary-{run_id}.json"
    schema_counts: dict[str, int] = {}
    for item in inventory:
        schema_counts[item.schema_id] = schema_counts.get(item.schema_id, 0) + 1
    summary = {
        "run_id": str(run_id),
        "dry_run": dry_run,
        "source_file_count": len(inventory),
        "migrated_file_count": len(valid) if not dry_run else 0,
        "quarantined_file_count": len(invalid),
        "quarantined_row_count": sum(item.invalid_row_count for item in inventory),
        "migrated_row_count": migrated_row_count,
        "stale_symbol_count": sum(item.is_stale for item in valid),
        "schema_counts": schema_counts,
        "schema_report_path": str(schema_report_path),
        "comparison_report_path": (
            str(comparison_report_path) if comparison_report_path else None
        ),
        "parquet_file_count": len(written),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return MigrationResult(
        run_id=run_id,
        source_file_count=len(inventory),
        migrated_file_count=len(valid) if not dry_run else 0,
        quarantined_file_count=len(invalid),
        quarantined_row_count=sum(item.invalid_row_count for item in inventory),
        migrated_row_count=migrated_row_count,
        stale_symbol_count=sum(item.is_stale for item in valid),
        schema_report_path=schema_report_path,
        comparison_report_path=comparison_report_path,
        summary_path=summary_path,
        parquet_files=tuple(written),
    )


def normalize_legacy_price_file(
    path: Path,
    *,
    run_id: UUID,
    is_stale: bool,
) -> pa.Table:
    """Convert one previously validated CSV into the canonical Arrow table."""
    frame = pd.read_csv(path, usecols=list(REQUIRED_COLUMNS), low_memory=False)
    frame.columns = [column.strip().lower() for column in frame.columns]
    invalid = _invalid_row_mask(frame, expected_symbol=path.stem.upper())
    frame = frame.loc[~invalid].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    frame = frame.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    for column in (*PRICE_COLUMNS, "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="raise")

    local_midnight = frame["date"].dt.tz_localize("America/New_York")
    close_time = (local_midnight + pd.Timedelta(hours=16)).dt.tz_convert("UTC")
    available_time = (
        local_midnight + pd.Timedelta(hours=16, minutes=30)
    ).dt.tz_convert("UTC")
    file_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    latest_available = available_time.max().to_pydatetime()
    fetched_at = max(file_mtime, latest_available)
    flags = [
        "legacy_adjusted_prices",
        "legacy_fetched_at_inferred",
        "legacy_available_at_inferred",
    ]
    if is_stale:
        flags.append("stale_at_migration")

    normalized = pd.DataFrame(
        {
            "symbol": frame["symbol"].str.strip().str.upper(),
            "timestamp_utc": close_time,
            "session_date_ny": frame["date"].dt.date,
            "open": frame["open"].astype("float64"),
            "high": frame["high"].astype("float64"),
            "low": frame["low"].astype("float64"),
            "close": frame["close"].astype("float64"),
            "volume": frame["volume"].round().astype("int64"),
            "adjusted": True,
            "split_factor": 1.0,
            "dividend": 0.0,
            "source": LEGACY_SOURCE,
            "source_version": LEGACY_SOURCE_VERSION,
            "fetched_at_utc": fetched_at,
            "available_at_utc": available_time,
            "ingestion_run_id": str(run_id),
            "is_stale": is_stale,
            "quality_flags": [flags.copy() for _ in range(len(frame))],
        }
    )
    return pa.Table.from_pandas(
        normalized,
        schema=DAILY_PRICE_SCHEMA,
        preserve_index=False,
        safe=True,
    )


def _inspect_csv(
    path: Path,
    *,
    max_invalid_fraction: float,
) -> LegacyFileInventory:
    issues: list[str] = []
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            header = [column.strip().lower() for column in next(csv.reader(handle))]
    except (OSError, StopIteration, UnicodeDecodeError, csv.Error) as error:
        return _error_inventory(path, f"unreadable_header:{type(error).__name__}")

    missing = sorted(set(REQUIRED_COLUMNS) - set(header))
    schema_id = hashlib.sha256(",".join(header).encode()).hexdigest()[:12]
    if missing:
        return _error_inventory(
            path,
            f"missing_columns:{','.join(missing)}",
            schema_id=schema_id,
            column_count=len(header),
        )

    try:
        frame = pd.read_csv(path, usecols=list(REQUIRED_COLUMNS), low_memory=False)
    except (OSError, UnicodeDecodeError, pd.errors.ParserError, ValueError) as error:
        return _error_inventory(
            path,
            f"unreadable_rows:{type(error).__name__}",
            schema_id=schema_id,
            column_count=len(header),
        )
    frame.columns = [column.strip().lower() for column in frame.columns]
    expected_symbol = path.stem.upper()
    dates = pd.to_datetime(frame["date"], errors="coerce")
    invalid = _invalid_row_mask(frame, expected_symbol=expected_symbol)

    duplicate_count = int(dates.duplicated(keep="last").sum())
    if duplicate_count:
        issues.append(f"duplicate_sessions:{duplicate_count}")
    invalid_count = int(invalid.sum())
    if invalid_count:
        issues.append(f"invalid_rows:{invalid_count}")
    invalid_fraction = invalid_count / max(len(frame), 1)
    status = (
        "error"
        if invalid_fraction > max_invalid_fraction
        else ("warning" if issues else "ok")
    )
    migratable_dates = dates.loc[~invalid].drop_duplicates(keep="last")
    return LegacyFileInventory(
        path=str(path.resolve()),
        symbol=expected_symbol,
        schema_id=schema_id,
        column_count=len(header),
        row_count=len(frame),
        unique_session_count=int(dates.nunique()),
        migratable_row_count=len(migratable_dates),
        duplicate_session_count=duplicate_count,
        min_session_date=(
            migratable_dates.min().date() if not migratable_dates.empty else None
        ),
        max_session_date=(
            migratable_dates.max().date() if not migratable_dates.empty else None
        ),
        invalid_row_count=invalid_count,
        is_stale=False,
        status=status,
        issues=";".join(issues),
    )


def _error_inventory(
    path: Path,
    issue: str,
    *,
    schema_id: str = "",
    column_count: int = 0,
) -> LegacyFileInventory:
    return LegacyFileInventory(
        path=str(path.resolve()),
        symbol=path.stem.upper(),
        schema_id=schema_id,
        column_count=column_count,
        row_count=0,
        unique_session_count=0,
        migratable_row_count=0,
        duplicate_session_count=0,
        min_session_date=None,
        max_session_date=None,
        invalid_row_count=0,
        is_stale=False,
        status="error",
        issues=issue,
    )


def _write_inventory_report(
    inventory: list[LegacyFileInventory],
    destination: Path,
) -> None:
    frame = pd.DataFrame(asdict(item) for item in inventory)
    frame.to_csv(destination, index=False)


def _quarantine_file(source: Path, quarantine_directory: Path, reason: str) -> None:
    quarantine_directory.mkdir(parents=True, exist_ok=True)
    destination = quarantine_directory / source.name
    if destination.exists():
        digest = hashlib.sha256(str(source).encode()).hexdigest()[:8]
        destination = quarantine_directory / f"{source.stem}-{digest}{source.suffix}"
    shutil.copy2(source, destination)
    destination.with_suffix(destination.suffix + ".reason.txt").write_text(
        reason + "\n",
        encoding="utf-8",
    )


def _quarantine_invalid_rows(
    source: Path,
    quarantine_directory: Path,
    reason: str,
) -> None:
    frame = pd.read_csv(source, low_memory=False)
    normalized = frame.rename(columns=lambda column: str(column).strip().lower())
    invalid = _invalid_row_mask(normalized, expected_symbol=source.stem.upper())
    quarantine_directory.mkdir(parents=True, exist_ok=True)
    destination = quarantine_directory / f"{source.stem}-invalid.csv"
    frame.loc[invalid].to_csv(destination, index=False)
    destination.with_suffix(destination.suffix + ".reason.txt").write_text(
        reason + "\n",
        encoding="utf-8",
    )


def _invalid_row_mask(frame: pd.DataFrame, *, expected_symbol: str) -> pd.Series:
    dates = pd.to_datetime(frame["date"], errors="coerce")
    numeric = {
        column: pd.to_numeric(frame[column], errors="coerce")
        for column in (*PRICE_COLUMNS, "volume")
    }
    symbols = frame["symbol"].astype("string").str.strip().str.upper()
    invalid = dates.isna() | symbols.isna() | (symbols == "") | (symbols != expected_symbol)
    for column in PRICE_COLUMNS:
        invalid |= numeric[column].isna() | ~numeric[column].map(math_isfinite)
        invalid |= numeric[column] <= 0
    invalid |= numeric["volume"].isna() | ~numeric["volume"].map(math_isfinite)
    invalid |= numeric["volume"] < 0
    invalid |= (numeric["volume"] % 1).abs() > 1e-9

    tolerance = {
        column: numeric[column].abs() * 1e-8 + 1e-10 for column in PRICE_COLUMNS
    }
    invalid |= numeric["low"] > numeric["open"] + tolerance["open"]
    invalid |= numeric["low"] > numeric["close"] + tolerance["close"]
    invalid |= numeric["high"] + tolerance["open"] < numeric["open"]
    invalid |= numeric["high"] + tolerance["close"] < numeric["close"]
    invalid |= numeric["low"] > numeric["high"] + tolerance["high"]
    return invalid.fillna(True)


def math_isfinite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _build_comparison_report(
    analytics: DuckDBAnalytics,
    inventory: list[LegacyFileInventory],
) -> pd.DataFrame:
    expected = pd.DataFrame(
        {
            "symbol": [item.symbol for item in inventory],
            "legacy_rows": [item.migratable_row_count for item in inventory],
            "legacy_min_date": [item.min_session_date for item in inventory],
            "legacy_max_date": [item.max_session_date for item in inventory],
            "legacy_stale": [item.is_stale for item in inventory],
        }
    )
    migrated = analytics.connection.execute(
        """
        SELECT
            symbol,
            count(*) AS migrated_rows,
            min(session_date_ny) AS migrated_min_date,
            max(session_date_ny) AS migrated_max_date,
            min(close) AS migrated_close_min,
            max(close) AS migrated_close_max
        FROM daily_prices
        GROUP BY symbol
        ORDER BY symbol
        """
    ).fetchdf()
    comparison = expected.merge(migrated, how="left", on="symbol")
    comparison["migrated_rows"] = comparison["migrated_rows"].fillna(0).astype("int64")
    comparison["row_count_matches"] = (
        comparison["legacy_rows"] == comparison["migrated_rows"]
    )
    comparison["date_range_matches"] = (
        pd.to_datetime(comparison["legacy_min_date"])
        == pd.to_datetime(comparison["migrated_min_date"])
    ) & (
        pd.to_datetime(comparison["legacy_max_date"])
        == pd.to_datetime(comparison["migrated_max_date"])
    )
    return comparison
