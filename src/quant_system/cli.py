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
from quant_system.decision.gates import (
    latest_report_path,
    run_open_gate_workflow,
    run_preopen_refresh_workflow,
)
from quant_system.decision.hierarchy import run_hierarchy_diagnostics_workflow
from quant_system.decision.journal import reconstruct_open_positions
from quant_system.decision.paper import (
    run_paper_advance_workflow,
    run_paper_update_workflow,
)
from quant_system.decision.positions import run_position_check_workflow
from quant_system.decision.premarket import run_premarket_workflow
from quant_system.decision.rotation import run_rotation_diagnostics_workflow
from quant_system.domain.clocks import NyseSessionClock
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
from quant_system.models.config import (
    load_ranking_baseline_settings,
    load_ridge_baseline_settings,
)
from quant_system.models.workflow import (
    run_ranking_baseline_workflow,
    run_ridge_baseline_workflow,
)
from quant_system.quality.reports import PipelineStatus
from quant_system.sentiment.classifier import FinbertSentimentScorer, RuleBasedSentimentScorer
from quant_system.sentiment.mapping import AliasResolver
from quant_system.sentiment.risk import assess_news_risk
from quant_system.settings import PROJECT_ROOT, get_settings
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.storage.sqlite import OperationsRegistry
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
decision_app = typer.Typer(
    help="Generate daily human decision-support reports.",
    no_args_is_help=True,
)
journal_app = typer.Typer(
    help="Record manual decisions, fills, and holdings.",
    no_args_is_help=True,
)
paper_app = typer.Typer(
    help="Run forward paper-trading ledger updates.",
    no_args_is_help=True,
)
app.add_typer(data_app, name="data")
app.add_typer(strategy_app, name="strategy")
app.add_typer(backtest_app, name="backtest")
app.add_typer(model_app, name="model")
app.add_typer(decision_app, name="decision")
app.add_typer(journal_app, name="journal")
app.add_typer(paper_app, name="paper")


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
        sentiment_model_key = (
            "finbert:"
            f"{source_settings.sentiment.finbert.model_name}:"
            f"{source_settings.sentiment.finbert.model_revision}"
        )
    else:
        scorer = RuleBasedSentimentScorer()
        sentiment_model_key = "rule_based:v1"

    service = NewsUpdateService(
        repository=repository,
        report_directory=settings.resolved_data_dir / "reports" / "quality",
        resolver=resolver,
        scorer=scorer,
        config=NewsUpdateConfig(
            alpha_vantage_batch_size=source_settings.alpha_vantage.batch_size,
            high_severity_veto=source_settings.risk.high_severity_veto,
            sentiment_cache_directory=settings.resolved_data_dir / "cache" / "sentiment",
            sentiment_cache_enabled=source_settings.sentiment.finbert.cache_enabled,
            sentiment_timeout_seconds=(
                source_settings.sentiment.finbert.inference_timeout_seconds
            ),
            sentiment_model_key=sentiment_model_key,
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


@model_app.command("ranking-baseline")
def model_ranking_baseline(
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
            help="Ridge + LightGBM ranking baseline YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "models" / "ranking_baseline.yaml",
) -> None:
    """Compare rules-only candidates, Ridge, and conservative LightGBM."""
    requested = _parse_symbols(symbols)
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    result = run_ranking_baseline_workflow(
        repository=repository,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        operations_database_path=settings.resolved_data_dir / "db" / "operations.sqlite",
        report_root=settings.resolved_data_dir / "reports" / "models",
        symbols=requested,
        start=start.date(),
        end=end.date(),
        strategy_config=load_buy_the_dip_config(strategy_config_path),
        model_settings=load_ranking_baseline_settings(model_config_path),
    )
    typer.echo(json.dumps(result.summary, indent=2, sort_keys=True))


@decision_app.command("premarket")
def decision_premarket(
    as_of: Annotated[
        datetime | None,
        typer.Option(
            "--date",
            help="Signal session in YYYY-MM-DD form. Defaults to latest completed NYSE session.",
        ),
    ] = None,
    ai_universe_path: Annotated[
        Path,
        typer.Option(
            "--ai-universe",
            exists=True,
            dir_okay=False,
            help="AI alpha watchlist YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "universe" / "ai_watchlist.yaml",
    hedge_universe_path: Annotated[
        Path,
        typer.Option(
            "--hedge-universe",
            exists=True,
            dir_okay=False,
            help="Defensive hedge overlay YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "universe" / "hedge_overlay.yaml",
    strategy_config_path: Annotated[
        Path,
        typer.Option(
            "--strategy-config",
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
    calibration: Annotated[
        bool,
        typer.Option(
            "--calibration/--no-calibration",
            help="Attach read-only hierarchy and tiered-candidate calibration context.",
        ),
    ] = True,
    benchmark_path: Annotated[
        Path,
        typer.Option(
            "--benchmarks",
            exists=True,
            dir_okay=False,
            help="Benchmark ETF YAML used by calibration context.",
        ),
    ] = PROJECT_ROOT / "configs" / "universe" / "benchmarks.yaml",
    model_ranking: Annotated[
        bool,
        typer.Option(
            "--model-ranking/--no-model-ranking",
            help="Attach read-only LightGBM rank context without changing actions.",
        ),
    ] = True,
    model_config_path: Annotated[
        Path,
        typer.Option(
            "--model-config",
            exists=True,
            dir_okay=False,
            help="Ridge + LightGBM ranking YAML used for daily rank context.",
        ),
    ] = PROJECT_ROOT / "configs" / "models" / "ranking_baseline.yaml",
) -> None:
    """Generate JSON, CSV, and Markdown artifacts for the premarket plan."""
    signal_session = (
        as_of.date()
        if as_of is not None
        else NyseSessionClock().latest_completed_session(datetime.now(UTC))
    )
    news_source_settings = load_news_source_settings(news_config_path)
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    report, _artifacts = run_premarket_workflow(
        repository=repository,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        report_root=settings.resolved_data_dir / "reports" / "daily",
        universe_paths=(ai_universe_path, hedge_universe_path),
        signal_session=signal_session,
        config=load_buy_the_dip_config(strategy_config_path),
        include_news_risk=news_risk,
        news_lookback_hours=news_source_settings.risk.lookback_hours,
        include_calibration=calibration,
        benchmark_path=benchmark_path,
        include_model_ranking=model_ranking,
        model_settings=load_ranking_baseline_settings(model_config_path)
        if model_ranking
        else None,
    )
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@decision_app.command("positions")
def decision_positions(
    as_of: Annotated[
        datetime | None,
        typer.Option(
            "--date",
            help=(
                "Position check session in YYYY-MM-DD form. "
                "Defaults to latest completed NYSE session."
            ),
        ),
    ] = None,
    strategy_config_path: Annotated[
        Path,
        typer.Option(
            "--strategy-config",
            exists=True,
            dir_okay=False,
            help="Buy-the-Dip strategy YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "strategy" / "buy_the_dip.yaml",
) -> None:
    """Check recorded open positions for HOLD/EXIT/DEFENSIVE actions."""
    check_session = (
        as_of.date()
        if as_of is not None
        else NyseSessionClock().latest_completed_session(datetime.now(UTC))
    )
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    report, _artifacts = run_position_check_workflow(
        repository=repository,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        operations_database_path=settings.resolved_data_dir / "db" / "operations.sqlite",
        report_root=settings.resolved_data_dir / "reports" / "daily",
        as_of=check_session,
        config=load_buy_the_dip_config(strategy_config_path),
    )
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@decision_app.command("rotation")
def decision_rotation(
    as_of: Annotated[
        datetime | None,
        typer.Option(
            "--date",
            help=(
                "Rotation diagnostic session in YYYY-MM-DD form. "
                "Defaults to latest completed NYSE session."
            ),
        ),
    ] = None,
    ai_universe_path: Annotated[
        Path,
        typer.Option(
            "--ai-universe",
            exists=True,
            dir_okay=False,
            help="AI alpha watchlist YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "universe" / "ai_watchlist.yaml",
    hedge_universe_path: Annotated[
        Path,
        typer.Option(
            "--hedge-universe",
            exists=True,
            dir_okay=False,
            help="Defensive hedge overlay YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "universe" / "hedge_overlay.yaml",
    benchmark_symbol: Annotated[
        str,
        typer.Option("--benchmark", help="Default benchmark for mixed groups."),
    ] = "QQQ",
) -> None:
    """Generate read-only sector/theme rotation diagnostics for calibration."""
    diagnostic_session = (
        as_of.date()
        if as_of is not None
        else NyseSessionClock().latest_completed_session(datetime.now(UTC))
    )
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    report, _artifacts = run_rotation_diagnostics_workflow(
        repository=repository,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        report_root=settings.resolved_data_dir / "reports" / "daily",
        universe_paths=(ai_universe_path, hedge_universe_path),
        as_of=diagnostic_session,
        benchmark_symbol=benchmark_symbol,
    )
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@decision_app.command("hierarchy")
def decision_hierarchy(
    as_of: Annotated[
        datetime | None,
        typer.Option(
            "--date",
            help=(
                "Hierarchy diagnostic session in YYYY-MM-DD form. "
                "Defaults to latest completed NYSE session."
            ),
        ),
    ] = None,
    ai_universe_path: Annotated[
        Path,
        typer.Option(
            "--ai-universe",
            exists=True,
            dir_okay=False,
            help="AI alpha watchlist YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "universe" / "ai_watchlist.yaml",
    hedge_universe_path: Annotated[
        Path,
        typer.Option(
            "--hedge-universe",
            exists=True,
            dir_okay=False,
            help="Defensive hedge overlay YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "universe" / "hedge_overlay.yaml",
    benchmark_path: Annotated[
        Path,
        typer.Option(
            "--benchmarks",
            exists=True,
            dir_okay=False,
            help="Benchmark ETF YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "universe" / "benchmarks.yaml",
    benchmark_symbol: Annotated[
        str,
        typer.Option("--benchmark", help="Default benchmark for relative returns."),
    ] = "QQQ",
) -> None:
    """Generate market/sector/stock diagnostics and calibration context."""
    diagnostic_session = (
        as_of.date()
        if as_of is not None
        else NyseSessionClock().latest_completed_session(datetime.now(UTC))
    )
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    report, _artifacts = run_hierarchy_diagnostics_workflow(
        repository=repository,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        report_root=settings.resolved_data_dir / "reports" / "daily",
        universe_paths=(ai_universe_path, hedge_universe_path),
        benchmark_path=benchmark_path,
        as_of=diagnostic_session,
        benchmark_symbol=benchmark_symbol,
    )
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@decision_app.command("preopen-refresh")
def decision_preopen_refresh(
    premarket_report_path: Annotated[
        Path | None,
        typer.Option(
            "--premarket-report",
            exists=True,
            dir_okay=False,
            help="Premarket JSON report. Defaults to latest under data/reports/daily.",
        ),
    ] = None,
    snapshot_path: Annotated[
        Path | None,
        typer.Option(
            "--snapshot",
            exists=True,
            dir_okay=False,
            help="Optional CSV/JSON broker snapshot with symbol,last_price,news_risk.",
        ),
    ] = None,
) -> None:
    """Run a conservative pre-open refresh before manual order entry."""
    settings = get_settings()
    source_report = _resolve_premarket_report(
        premarket_report_path,
        settings.resolved_data_dir,
    )
    report, _artifacts = run_preopen_refresh_workflow(
        premarket_report_path=source_report,
        report_root=settings.resolved_data_dir / "reports" / "daily",
        snapshot_path=snapshot_path,
    )
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@decision_app.command("open-gate")
def decision_open_gate(
    minutes: Annotated[
        int,
        typer.Option("--minutes", min=30, max=60, help="Open gate minute: 30 or 60."),
    ],
    premarket_report_path: Annotated[
        Path | None,
        typer.Option(
            "--premarket-report",
            exists=True,
            dir_okay=False,
            help="Premarket JSON report. Defaults to latest under data/reports/daily.",
        ),
    ] = None,
    snapshot_path: Annotated[
        Path | None,
        typer.Option(
            "--snapshot",
            exists=True,
            dir_okay=False,
            help="Optional CSV/JSON broker snapshot with symbol,last_price,news_risk.",
        ),
    ] = None,
) -> None:
    """Run T+30/T+60 KEEP/DEFER/CANCEL gate checks."""
    settings = get_settings()
    source_report = _resolve_premarket_report(
        premarket_report_path,
        settings.resolved_data_dir,
    )
    report, _artifacts = run_open_gate_workflow(
        premarket_report_path=source_report,
        report_root=settings.resolved_data_dir / "reports" / "daily",
        minutes=minutes,
        snapshot_path=snapshot_path,
    )
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@journal_app.command("add-fill")
def journal_add_fill(
    symbol: Annotated[str, typer.Option("--symbol", help="Ticker symbol.")],
    side: Annotated[str, typer.Option("--side", help="BUY or SELL.")],
    quantity: Annotated[int, typer.Option("--quantity", min=1, help="Filled shares.")],
    price: Annotated[float, typer.Option("--price", min=0.01, help="Fill price.")],
    fill_time: Annotated[
        str | None,
        typer.Option(
            "--fill-time",
            help=(
                "UTC fill timestamp, e.g. 2026-07-27T13:30:00+00:00. "
                "Defaults to now."
            ),
        ),
    ] = None,
    commission: Annotated[
        float,
        typer.Option("--commission", min=0, help="Absolute commission amount."),
    ] = 0.0,
    signal_id: Annotated[
        str | None,
        typer.Option("--signal-id", help="Optional system signal UUID."),
    ] = None,
    stop_price: Annotated[
        float | None,
        typer.Option("--stop-price", min=0.01, help="Optional planned stop price."),
    ] = None,
    target_price: Annotated[
        float | None,
        typer.Option("--target-price", min=0.01, help="Optional planned target price."),
    ] = None,
    notes: Annotated[str, typer.Option("--notes", help="Human note.")] = "",
) -> None:
    """Record a manual broker fill in operations SQLite."""
    settings = get_settings()
    with OperationsRegistry(settings.resolved_data_dir / "db" / "operations.sqlite") as store:
        record = store.record_manual_fill(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            fill_time_utc=_parse_cli_datetime(fill_time) if fill_time else datetime.now(UTC),
            commission=commission,
            signal_id=signal_id,
            stop_price=stop_price,
            target_price=target_price,
            notes=notes,
        )
    typer.echo(json.dumps(record, indent=2, sort_keys=True))


@journal_app.command("decision")
def journal_decision(
    symbol: Annotated[str, typer.Option("--symbol", help="Ticker symbol.")],
    action: Annotated[
        str,
        typer.Option("--action", help="ACCEPT, REJECT, DEFER, WATCH, REDUCE, EXIT."),
    ],
    decision_session: Annotated[
        datetime | None,
        typer.Option("--date", help="Decision session. Defaults to latest completed NYSE session."),
    ] = None,
    reason: Annotated[str, typer.Option("--reason", help="Human reason.")] = "",
    signal_id: Annotated[
        str | None,
        typer.Option("--signal-id", help="Optional system signal UUID."),
    ] = None,
    report_run_id: Annotated[
        str | None,
        typer.Option("--report-run-id", help="Optional premarket report run UUID."),
    ] = None,
) -> None:
    """Record a manual decision linked to an optional system signal."""
    session = (
        decision_session.date()
        if decision_session is not None
        else NyseSessionClock().latest_completed_session(datetime.now(UTC))
    )
    settings = get_settings()
    with OperationsRegistry(settings.resolved_data_dir / "db" / "operations.sqlite") as store:
        record = store.record_manual_decision(
            symbol=symbol,
            decision_session=session.isoformat(),
            action=action,
            reason=reason,
            signal_id=signal_id,
            report_run_id=report_run_id,
        )
    typer.echo(json.dumps(record, indent=2, sort_keys=True))


@journal_app.command("positions")
def journal_positions() -> None:
    """Show open positions reconstructed from manual fills."""
    settings = get_settings()
    with OperationsRegistry(settings.resolved_data_dir / "db" / "operations.sqlite") as store:
        positions = reconstruct_open_positions(store.manual_fills())
    typer.echo(
        json.dumps(
            {"open_positions": [position.to_dict() for position in positions]},
            indent=2,
            sort_keys=True,
        )
    )


@journal_app.command("fills")
def journal_fills(
    symbol: Annotated[
        str | None,
        typer.Option("--symbol", help="Optional ticker filter."),
    ] = None,
) -> None:
    """List manual fills from operations SQLite."""
    settings = get_settings()
    with OperationsRegistry(settings.resolved_data_dir / "db" / "operations.sqlite") as store:
        fills = store.manual_fills(symbol=symbol)
    typer.echo(json.dumps({"fills": fills}, indent=2, sort_keys=True))


@paper_app.command("update")
def paper_update(
    premarket_report_path: Annotated[
        Path | None,
        typer.Option(
            "--premarket-report",
            exists=True,
            dir_okay=False,
            help="Premarket JSON report. Defaults to latest under data/reports/daily.",
        ),
    ] = None,
    gate_report_path: Annotated[
        Path | None,
        typer.Option(
            "--gate-report",
            exists=True,
            dir_okay=False,
            help="Optional open-gate JSON report; non-KEEP rows are skipped.",
        ),
    ] = None,
    fill_session: Annotated[
        datetime | None,
        typer.Option("--fill-session", help="Paper fill session. Defaults to report next session."),
    ] = None,
    quantity: Annotated[
        int,
        typer.Option("--quantity", min=1, help="Paper shares per accepted signal."),
    ] = 1,
    strategy_config_path: Annotated[
        Path,
        typer.Option(
            "--strategy-config",
            exists=True,
            dir_okay=False,
            help="Buy-the-Dip strategy YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "strategy" / "buy_the_dip.yaml",
) -> None:
    """Create forward paper BUY fills for eligible premarket candidates."""
    settings = get_settings()
    source_report = _resolve_premarket_report(
        premarket_report_path,
        settings.resolved_data_dir,
    )
    repository = ParquetRepository(settings.resolved_data_dir)
    report, _artifacts = run_paper_update_workflow(
        premarket_report_path=source_report,
        repository=repository,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        operations_database_path=settings.resolved_data_dir / "db" / "operations.sqlite",
        report_root=settings.resolved_data_dir / "reports" / "daily",
        config=load_buy_the_dip_config(strategy_config_path),
        gate_report_path=gate_report_path,
        fill_session=fill_session.date() if fill_session else None,
        quantity=quantity,
    )
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@paper_app.command("advance")
def paper_advance(
    as_of: Annotated[
        datetime | None,
        typer.Option(
            "--date",
            help="Paper exit-check session. Defaults to latest completed NYSE session.",
        ),
    ] = None,
    strategy_config_path: Annotated[
        Path,
        typer.Option(
            "--strategy-config",
            exists=True,
            dir_okay=False,
            help="Buy-the-Dip strategy YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "strategy" / "buy_the_dip.yaml",
) -> None:
    """Advance open paper positions and record paper SELL fills when exits trigger."""
    settings = get_settings()
    check_session = (
        as_of.date()
        if as_of is not None
        else NyseSessionClock().latest_completed_session(datetime.now(UTC))
    )
    repository = ParquetRepository(settings.resolved_data_dir)
    report, _artifacts = run_paper_advance_workflow(
        repository=repository,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        operations_database_path=settings.resolved_data_dir / "db" / "operations.sqlite",
        report_root=settings.resolved_data_dir / "reports" / "daily",
        as_of=check_session,
        config=load_buy_the_dip_config(strategy_config_path),
    )
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@paper_app.command("positions")
def paper_positions() -> None:
    """Show open paper positions reconstructed from paper fills."""
    settings = get_settings()
    with OperationsRegistry(settings.resolved_data_dir / "db" / "operations.sqlite") as store:
        positions = reconstruct_open_positions(store.paper_fills())
    typer.echo(
        json.dumps(
            {"open_positions": [position.to_dict() for position in positions]},
            indent=2,
            sort_keys=True,
        )
    )


@paper_app.command("fills")
def paper_fills(
    symbol: Annotated[
        str | None,
        typer.Option("--symbol", help="Optional ticker filter."),
    ] = None,
) -> None:
    """List forward paper fills from operations SQLite."""
    settings = get_settings()
    with OperationsRegistry(settings.resolved_data_dir / "db" / "operations.sqlite") as store:
        fills = store.paper_fills(symbol=symbol)
    typer.echo(json.dumps({"fills": fills}, indent=2, sort_keys=True))


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


def _parse_cli_datetime(value: str) -> datetime:
    try:
        return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as error:
        raise typer.BadParameter(f"invalid ISO datetime: {value}") from error


def _resolve_premarket_report(value: Path | None, data_root: Path) -> Path:
    if value is not None:
        return value
    resolved = latest_report_path(data_root / "reports" / "daily", stem="premarket")
    if resolved is None:
        raise typer.BadParameter("no premarket report found; run quant decision premarket")
    return resolved


def main() -> None:
    """Invoke the Typer application."""
    app()
