"""Command-line entry point for the quant system."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from quant_system import __version__
from quant_system.logging import configure_logging, get_logger
from quant_system.migration.legacy_prices import migrate_legacy_prices
from quant_system.settings import PROJECT_ROOT, get_settings
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository

app = typer.Typer(
    name="quant",
    help="Daily quantitative research and trading decision support.",
    no_args_is_help=True,
    add_completion=False,
)
data_app = typer.Typer(help="Manage local market-data storage.", no_args_is_help=True)
app.add_typer(data_app, name="data")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def cli(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """Run research, data, and decision-support workflows."""
    settings = get_settings()
    configure_logging(settings.log_level)


@app.command()
def health() -> None:
    """Check that configuration and logging can initialize."""
    settings = get_settings()
    logger = get_logger(__name__)
    logger.info("health_check", extra={"app_env": settings.app_env})
    typer.echo("ok")


@app.command("show-config")
def show_config() -> None:
    """Print non-secret effective configuration."""
    settings = get_settings()
    typer.echo(json.dumps(settings.public_config(), indent=2, sort_keys=True))


@data_app.command("migrate-legacy")
def migrate_legacy(
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            exists=True,
            file_okay=False,
            help="Directory containing legacy per-symbol CSV files.",
        ),
    ] = PROJECT_ROOT / "legacy" / "data" / "csv",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Inventory and quarantine without writing Parquet."),
    ] = False,
    stale_after_days: Annotated[
        int,
        typer.Option(min=1, help="Calendar-day lag that marks a symbol stale."),
    ] = 7,
    max_invalid_fraction: Annotated[
        float,
        typer.Option(
            min=0,
            max=1,
            help="Maximum bad-row fraction before quarantining an entire file.",
        ),
    ] = 0.01,
    batch_size: Annotated[
        int,
        typer.Option(min=1, help="Source CSV files per immutable Parquet batch."),
    ] = 100,
    run_id: Annotated[
        UUID | None,
        typer.Option(help="Stable UUID for a reproducible migration run."),
    ] = None,
) -> None:
    """Migrate validated legacy OHLCV data into immutable Parquet."""
    settings = get_settings()
    data_root = settings.resolved_data_dir
    result = migrate_legacy_prices(
        source,
        data_root=data_root,
        report_directory=data_root / "reports" / "migration",
        quarantine_directory=data_root / "quarantine" / "legacy_prices",
        database_path=data_root / "db" / "analytics.duckdb",
        run_id=run_id,
        stale_after_days=stale_after_days,
        max_invalid_fraction=max_invalid_fraction,
        batch_size=batch_size,
        dry_run=dry_run,
    )
    typer.echo(result.summary_path.read_text(encoding="utf-8"))


@data_app.command("bootstrap-duckdb")
def bootstrap_duckdb() -> None:
    """Build analytical views from the current Parquet partitions."""
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    database_path = settings.resolved_data_dir / "db" / "analytics.duckdb"
    with DuckDBAnalytics(database_path, repository.daily_prices_root) as analytics:
        analytics.refresh_views()
        row_count = analytics.connection.execute(
            "SELECT count(*) FROM daily_prices"
        ).fetchone()[0]
    typer.echo(json.dumps({"database": str(database_path), "daily_price_rows": row_count}))


@data_app.command("price-range")
def price_range(symbol: str) -> None:
    """Show the first and last stored session for a symbol."""
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    database_path = settings.resolved_data_dir / "db" / "analytics.duckdb"
    with DuckDBAnalytics(database_path, repository.daily_prices_root) as analytics:
        analytics.refresh_views()
        first_session, last_session = analytics.symbol_date_range(symbol)
    typer.echo(
        json.dumps(
            {
                "symbol": symbol.upper(),
                "first_session": str(first_session) if first_session else None,
                "last_session": str(last_session) if last_session else None,
            }
        )
    )


def main() -> None:
    """Invoke the Typer application."""
    app()
