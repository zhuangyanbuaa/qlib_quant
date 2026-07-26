"""Command-line entry point for the quant system."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from quant_system import __version__
from quant_system.backtest.workflow import run_backtest_workflow, scan_workflow
from quant_system.ingestion.alpha_vantage import AlphaVantageNewsAdapter
from quant_system.ingestion.config import (
    load_news_source_settings,
    load_price_source_settings,
)
from quant_system.ingestion.news import NewsUpdateConfig, NewsUpdateService
from quant_system.ingestion.prices import PriceUpdateService
from quant_system.ingestion.reliability import (
    DailyCallBudget,
    ProviderGuard,
    SlidingWindowRateLimiter,
)
from quant_system.ingestion.sec import SecCompanyEventAdapter
from quant_system.ingestion.yahoo import YahooFinancePriceAdapter
from quant_system.logging import configure_logging, get_logger
from quant_system.migration.legacy_prices import migrate_legacy_prices
from quant_system.models.config import load_ridge_baseline_settings
from quant_system.models.workflow import run_ridge_baseline_workflow
from quant_system.quality.reports import PipelineStatus
from quant_system.sentiment.classifier import FinbertSentimentScorer, RuleBasedSentimentScorer
from quant_system.sentiment.mapping import AliasResolver
from quant_system.sentiment.risk import assess_news_risk
from quant_system.settings import PROJECT_ROOT, get_settings
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.strategy.config import load_buy_the_dip_config

app = typer.Typer(
    name="quant",
    help="Daily quantitative research and trading decision support.",
    no_args_is_help=True,
    add_completion=False,
)
data_app = typer.Typer(help="Manage local market-data storage.", no_args_is_help=True)
strategy_app = typer.Typer(help="Generate rules-only strategy candidates.", no_args_is_help=True)
backtest_app = typer.Typer(help="Run conservative portfolio backtests.", no_args_is_help=True)
model_app = typer.Typer(help="Train and evaluate ranking baselines.", no_args_is_help=True)
app.add_typer(data_app, name="data")
app.add_typer(strategy_app, name="strategy")
app.add_typer(backtest_app, name="backtest")
app.add_typer(model_app, name="model")


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


@data_app.command("update-prices")
def update_prices(
    symbols: Annotated[
        str | None,
        typer.Option(
            "--symbols",
            help="Comma-separated symbols. Core symbols are always included.",
        ),
    ] = None,
    all_stored: Annotated[
        bool,
        typer.Option("--all-stored", help="Update every symbol already in DuckDB."),
    ] = False,
    config_path: Annotated[
        Path,
        typer.Option(
            "--config",
            exists=True,
            dir_okay=False,
            help="Validated price-source YAML configuration.",
        ),
    ] = PROJECT_ROOT / "configs" / "sources" / "prices.yaml",
) -> None:
    """Incrementally update completed daily bars and emit a quality report."""
    if symbols and all_stored:
        raise typer.BadParameter("use either --symbols or --all-stored, not both")
    source_settings = load_price_source_settings(config_path)
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    database_path = settings.resolved_data_dir / "db" / "analytics.duckdb"

    if all_stored:
        with DuckDBAnalytics(database_path, repository.daily_prices_root) as analytics:
            analytics.refresh_views()
            requested = tuple(analytics.stored_symbols())
    elif symbols:
        requested = tuple(part.strip().upper() for part in symbols.split(",") if part.strip())
    else:
        requested = source_settings.default_symbols

    provider = YahooFinancePriceAdapter(timeout_seconds=source_settings.timeout_seconds)
    guard = ProviderGuard(
        budget=DailyCallBudget(source_settings.daily_call_budget),
        limiter=SlidingWindowRateLimiter(source_settings.calls_per_minute),
        retry=source_settings.retry.to_policy(),
    )
    service = PriceUpdateService(
        provider=provider,
        guard=guard,
        repository=repository,
        database_path=database_path,
        report_directory=settings.resolved_data_dir / "reports" / "quality",
        config=source_settings.to_update_config(),
    )
    report, report_path = service.run(requested)
    output = report.to_dict()
    output["report_path"] = str(report_path)
    typer.echo(json.dumps(output, indent=2, sort_keys=True))
    if report.status is PipelineStatus.BLOCKED:
        raise typer.Exit(code=2)


@data_app.command("update-news")
def update_news(
    symbols: Annotated[
        str,
        typer.Option("--symbols", help="Comma-separated symbols to update."),
    ],
    start: Annotated[
        datetime | None,
        typer.Option("--start", help="UTC start timestamp. Defaults to risk lookback."),
    ] = None,
    end: Annotated[
        datetime | None,
        typer.Option("--end", help="UTC end timestamp. Defaults to now."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Fetch and classify without writing Parquet."),
    ] = False,
    config_path: Annotated[
        Path,
        typer.Option(
            "--config",
            exists=True,
            dir_okay=False,
            help="Validated news-source YAML configuration.",
        ),
    ] = PROJECT_ROOT / "configs" / "sources" / "news.yaml",
) -> None:
    """Incrementally update point-in-time news and SEC event records."""
    requested = _parse_symbols(symbols)
    source_settings = load_news_source_settings(config_path)
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    resolver = AliasResolver(source_settings.companies)
    end_utc = _ensure_utc(end or datetime.now(UTC))
    start_utc = _ensure_utc(
        start or (end_utc - timedelta(hours=source_settings.risk.lookback_hours))
    )

    news_provider = None
    news_guard = None
    if source_settings.alpha_vantage.enabled and settings.alpha_vantage_api_key:
        news_provider = AlphaVantageNewsAdapter(
            api_key=settings.alpha_vantage_api_key,
            timeout_seconds=source_settings.alpha_vantage.timeout_seconds,
            limit_per_call=source_settings.alpha_vantage.limit_per_call,
            topics=source_settings.alpha_vantage.topics,
        )
        news_guard = ProviderGuard(
            budget=DailyCallBudget(source_settings.alpha_vantage.daily_call_budget),
            limiter=SlidingWindowRateLimiter(source_settings.alpha_vantage.calls_per_minute),
            retry=source_settings.alpha_vantage.retry.to_policy(),
        )

    event_provider = None
    event_guard = None
    if source_settings.sec.enabled:
        event_provider = SecCompanyEventAdapter(
            cik_by_symbol=resolver.cik_by_symbol,
            user_agent=settings.sec_user_agent,
            timeout_seconds=source_settings.sec.timeout_seconds,
            forms=source_settings.sec.forms,
        )
        event_guard = ProviderGuard(
            budget=DailyCallBudget(source_settings.sec.daily_call_budget),
            limiter=SlidingWindowRateLimiter(source_settings.sec.calls_per_minute),
            retry=source_settings.sec.retry.to_policy(),
        )

    if source_settings.sentiment.scorer == "finbert":
        scorer = FinbertSentimentScorer(
            model_name=source_settings.sentiment.finbert.model_name,
            model_revision=source_settings.sentiment.finbert.model_revision,
            device=source_settings.sentiment.finbert.device,
        )
    else:
        scorer = RuleBasedSentimentScorer()

    service = NewsUpdateService(
        repository=repository,
        report_directory=settings.resolved_data_dir / "reports" / "quality",
        resolver=resolver,
        scorer=scorer,
        config=NewsUpdateConfig(
            alpha_vantage_batch_size=source_settings.alpha_vantage.batch_size,
            high_severity_veto=source_settings.risk.high_severity_veto,
        ),
        news_provider=news_provider,
        news_guard=news_guard,
        event_provider=event_provider,
        event_guard=event_guard,
    )
    report, report_path = service.run(
        requested,
        start_utc=start_utc,
        end_utc=end_utc,
        dry_run=dry_run,
    )
    output = report.to_dict()
    output["report_path"] = str(report_path)
    typer.echo(json.dumps(output, indent=2, sort_keys=True))
    if report.status is PipelineStatus.BLOCKED:
        raise typer.Exit(code=2)


@data_app.command("news-risk")
def news_risk(
    symbols: Annotated[
        str,
        typer.Option("--symbols", help="Comma-separated symbols to assess."),
    ],
    cutoff: Annotated[
        datetime,
        typer.Option("--cutoff", help="UTC point-in-time cutoff."),
    ],
    config_path: Annotated[
        Path,
        typer.Option(
            "--config",
            exists=True,
            dir_okay=False,
            help="Validated news-source YAML configuration.",
        ),
    ] = PROJECT_ROOT / "configs" / "sources" / "news.yaml",
) -> None:
    """Show point-in-time news risk and traceable veto references."""
    requested = _parse_symbols(symbols)
    source_settings = load_news_source_settings(config_path)
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    cutoff_utc = _ensure_utc(cutoff)
    with DuckDBAnalytics(
        settings.resolved_data_dir / "db" / "analytics.duckdb",
        repository.daily_prices_root,
    ) as analytics:
        analytics.refresh_views()
        assessments = assess_news_risk(
            symbols=requested,
            articles=analytics.query_news_articles(
                requested,
                cutoff_utc=cutoff_utc,
                lookback_hours=source_settings.risk.lookback_hours,
            ),
            events=analytics.query_company_events(
                requested,
                cutoff_utc=cutoff_utc,
                lookback_hours=source_settings.risk.lookback_hours,
            ),
            cutoff_utc=cutoff_utc,
            lookback_hours=source_settings.risk.lookback_hours,
        )
    typer.echo(
        json.dumps(
            {
                "cutoff_utc": cutoff_utc.isoformat(),
                "lookback_hours": source_settings.risk.lookback_hours,
                "risk": {symbol: risk.to_dict() for symbol, risk in assessments.items()},
            },
            indent=2,
            sort_keys=True,
        )
    )


@strategy_app.command("scan")
def strategy_scan(
    symbols: Annotated[
        str,
        typer.Option("--symbols", help="Comma-separated symbols to scan."),
    ],
    as_of: Annotated[
        datetime,
        typer.Option("--date", help="Signal session in YYYY-MM-DD form."),
    ],
    config_path: Annotated[
        Path,
        typer.Option(
            "--config",
            exists=True,
            dir_okay=False,
            help="Buy-the-Dip strategy YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "strategy" / "buy_the_dip.yaml",
    news_risk: Annotated[
        bool,
        typer.Option("--news-risk/--no-news-risk", help="Apply Phase 4 news vetoes."),
    ] = True,
    news_config_path: Annotated[
        Path,
        typer.Option(
            "--news-config",
            exists=True,
            dir_okay=False,
            help="News-source YAML used for risk lookback settings.",
        ),
    ] = PROJECT_ROOT / "configs" / "sources" / "news.yaml",
) -> None:
    """Scan one historical or current completed session."""
    requested = _parse_symbols(symbols)
    news_source_settings = load_news_source_settings(news_config_path)
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    signals = scan_workflow(
        repository=repository,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        symbols=requested,
        as_of=as_of.date(),
        config=load_buy_the_dip_config(config_path),
        include_news_risk=news_risk,
        news_lookback_hours=news_source_settings.risk.lookback_hours,
    )
    typer.echo(json.dumps({"date": as_of.date().isoformat(), "signals": signals}, indent=2))


@backtest_app.command("run")
def backtest_run(
    symbols: Annotated[
        str,
        typer.Option("--symbols", help="Comma-separated current-snapshot universe."),
    ],
    start: Annotated[datetime, typer.Option("--start", help="First signal session.")],
    end: Annotated[datetime, typer.Option("--end", help="Last evaluation session.")],
    stress: Annotated[
        bool,
        typer.Option("--stress", help="Run the fixed pessimistic robustness grid."),
    ] = False,
    config_path: Annotated[
        Path,
        typer.Option(
            "--config",
            exists=True,
            dir_okay=False,
            help="Buy-the-Dip strategy YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "strategy" / "buy_the_dip.yaml",
) -> None:
    """Run a cash-aware daily-bar portfolio backtest."""
    requested = _parse_symbols(symbols)
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    summary, _artifacts = run_backtest_workflow(
        repository=repository,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        report_root=settings.resolved_data_dir / "reports" / "backtests",
        symbols=requested,
        start=start.date(),
        end=end.date(),
        config=load_buy_the_dip_config(config_path),
        include_stress=stress,
    )
    typer.echo(json.dumps(summary, indent=2, sort_keys=True))


@model_app.command("ridge-baseline")
def model_ridge_baseline(
    symbols: Annotated[
        str,
        typer.Option("--symbols", help="Comma-separated current-snapshot universe."),
    ],
    start: Annotated[datetime, typer.Option("--start", help="First signal session.")],
    end: Annotated[datetime, typer.Option("--end", help="Last evaluation session.")],
    strategy_config_path: Annotated[
        Path,
        typer.Option(
            "--strategy-config",
            exists=True,
            dir_okay=False,
            help="Buy-the-Dip strategy YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "strategy" / "buy_the_dip.yaml",
    model_config_path: Annotated[
        Path,
        typer.Option(
            "--model-config",
            exists=True,
            dir_okay=False,
            help="Ridge baseline model YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "models" / "ridge_baseline.yaml",
) -> None:
    """Run purged walk-forward Ridge validation on rules-approved candidates."""
    requested = _parse_symbols(symbols)
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    result = run_ridge_baseline_workflow(
        repository=repository,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        report_root=settings.resolved_data_dir / "reports" / "models",
        symbols=requested,
        start=start.date(),
        end=end.date(),
        strategy_config=load_buy_the_dip_config(strategy_config_path),
        model_settings=load_ridge_baseline_settings(model_config_path),
    )
    typer.echo(json.dumps(result.summary, indent=2, sort_keys=True))


def _parse_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(
        dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip())
    )
    if not symbols:
        raise typer.BadParameter("at least one symbol is required")
    return symbols


def _ensure_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def main() -> None:
    """Invoke the Typer application."""
    app()
