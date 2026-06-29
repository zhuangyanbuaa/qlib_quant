import json
from pathlib import Path
from uuid import UUID

import pandas as pd

from quant_system.migration.legacy_prices import migrate_legacy_prices
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    pd.DataFrame(rows).to_csv(path, index=False)


def price_row(symbol: str, session: str, close: float = 101.0) -> dict[str, object]:
    return {
        "date": session,
        "open": 100.0,
        "high": max(102.0, close),
        "low": 99.0,
        "close": close,
        "volume": 1_000,
        "symbol": symbol,
        "atr_20": 2.0,
    }


def test_migration_quarantines_invalid_files_and_reconciles_valid_rows(tmp_path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    write_csv(
        source / "aapl.csv",
        [
            price_row("AAPL", "2026-06-25"),
            price_row("AAPL", "2026-06-26", close=103.0),
            {**price_row("AAPL", "2026-06-24"), "low": 105.0},
        ],
    )
    write_csv(source / "stale.csv", [price_row("STALE", "2026-05-01")])
    write_csv(
        source / "bad.csv",
        [{**price_row("BAD", "2026-06-26"), "low": 105.0}],
    )
    data_root = tmp_path / "data"
    reports = data_root / "reports"
    quarantine = data_root / "quarantine"
    database = data_root / "db" / "analytics.duckdb"

    result = migrate_legacy_prices(
        source,
        data_root=data_root,
        report_directory=reports,
        quarantine_directory=quarantine,
        database_path=database,
        run_id=UUID("11111111-1111-1111-1111-111111111111"),
        stale_after_days=7,
        max_invalid_fraction=0.5,
        batch_size=1,
    )

    assert result.source_file_count == 3
    assert result.migrated_file_count == 2
    assert result.quarantined_file_count == 1
    assert result.quarantined_row_count == 2
    assert result.migrated_row_count == 3
    assert result.stale_symbol_count == 1
    run_quarantine = quarantine / str(result.run_id)
    assert (run_quarantine / "bad.csv").exists()
    assert (run_quarantine / "bad.csv.reason.txt").exists()
    assert (run_quarantine / "rows" / "aapl-invalid.csv").exists()

    summary = json.loads(result.summary_path.read_text())
    assert summary["parquet_file_count"] == 2
    comparison = pd.read_csv(result.comparison_report_path)
    assert comparison["row_count_matches"].all()
    assert comparison["date_range_matches"].all()

    repository = ParquetRepository(data_root)
    with DuckDBAnalytics(database, repository.daily_prices_root) as analytics:
        analytics.refresh_views()
        stale = analytics.query_daily_prices("STALE")
    assert stale.iloc[0]["is_stale"]
    assert "stale_at_migration" in stale.iloc[0]["quality_flags"]


def test_dry_run_writes_inventory_without_parquet(tmp_path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    write_csv(source / "aapl.csv", [price_row("AAPL", "2026-06-26")])

    result = migrate_legacy_prices(
        source,
        data_root=tmp_path / "data",
        report_directory=tmp_path / "reports",
        quarantine_directory=tmp_path / "quarantine",
        database_path=tmp_path / "analytics.duckdb",
        dry_run=True,
    )

    assert result.schema_report_path.exists()
    assert result.comparison_report_path is None
    assert not result.parquet_files
