"""Command-line entry point for the quant system."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import typer
from typer.core import TyperGroup

from quant_system import __version__
from quant_system.backtest.daily_scan import run_daily_scan_research
from quant_system.backtest.workflow import run_backtest_workflow, scan_workflow
from quant_system.decision.gates import (
    latest_report_path,
    run_open_gate_workflow,
    run_preopen_refresh_workflow,
)
from quant_system.decision.hierarchy import run_hierarchy_diagnostics_workflow
from quant_system.decision.journal import reconstruct_open_positions
from quant_system.decision.leverage import (
    run_leverage_overlay_universe_workflow,
    run_leverage_overlay_workflow,
)
from quant_system.decision.manual_research import combine_manual_research, import_manual_research
from quant_system.decision.paper import (
    run_paper_advance_workflow,
    run_paper_update_workflow,
)
from quant_system.decision.positions import run_position_check_workflow
from quant_system.decision.premarket import run_premarket_workflow
from quant_system.decision.research import run_research_list_workflow
from quant_system.decision.rotation import run_rotation_diagnostics_workflow
from quant_system.decision.workbench import run_daily_workbench_workflow
from quant_system.domain.clocks import NyseSessionClock
from quant_system.domain.companies import ReviewedCompanyMapping
from quant_system.domain.securities import SecurityLifecycleEvent
from quant_system.ingestion.budgets import SQLiteDailyCallBudget
from quant_system.ingestion.config import (
    load_news_source_settings,
    load_price_source_settings,
)
from quant_system.ingestion.factory import build_news_service
from quant_system.ingestion.prices import PriceUpdateService
from quant_system.ingestion.reliability import (
    ProviderGuard,
    SlidingWindowRateLimiter,
)
from quant_system.ingestion.source_health import check_sources
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
from quant_system.operations.pipeline import run_daily_pipeline
from quant_system.operations.registry import PipelineBusy, pipeline_lock
from quant_system.operations.scheduling import load_schedule, schedule_windows, tick_daily
from quant_system.operations.session_jobs import session_windows, tick_session_jobs
from quant_system.operations.telegram import TelegramClient, TelegramError
from quant_system.quality.prices import PriceDataBlocked
from quant_system.quality.reports import PipelineStatus
from quant_system.sentiment.coverage import load_coverage
from quant_system.sentiment.risk import assess_news_risk
from quant_system.settings import PROJECT_ROOT, get_settings
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.storage.sqlite import OperationsRegistry
from quant_system.strategy.config import load_buy_the_dip_config
from quant_system.strategy.leverage_overlay import (
    load_leverage_overlay_config,
    load_leverage_overlay_universe_config,
)
from quant_system.universe.config import load_daily_digest_config, load_watchlist_config


class PriceGateGroup(TyperGroup):
    """Translate shared price failures into structured CLI gate results."""

    def invoke(self, ctx: typer.Context):
        try:
            return super().invoke(ctx)
        except PriceDataBlocked as error:
            typer.echo(json.dumps(error.report, indent=2, sort_keys=True))
            raise typer.Exit(code=2) from error


app = typer.Typer(
    cls=PriceGateGroup,
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
automation_app = typer.Typer(help="Exchange-calendar scheduling and Telegram notifications.")
app.add_typer(automation_app, name="automation")

DAILY_SCHEDULE_PATH = PROJECT_ROOT / "configs/operations/daily.yaml"


@automation_app.command("tick")
def automation_tick(
    config: Annotated[Path, typer.Option("--config", exists=True)] = DAILY_SCHEDULE_PATH,
):
    try:
        result = tick_daily(get_settings(), load_schedule(config))
        result["session_automation"] = tick_session_jobs(get_settings(), load_schedule(config))
    except Exception as error:
        result = {"status": "FAILED", "error_type": type(error).__name__}
    typer.echo(json.dumps(result, indent=2))
    if result["status"] in {"FAILED", "BLOCKED_CONFIGURATION"} or result.get(
        "session_automation", {}
    ).get("status") in {"FAILED", "BLOCKED_CONFIGURATION"}:
        raise typer.Exit(2)


@automation_app.command("status")
def automation_status(
    config: Annotated[Path, typer.Option("--config", exists=True)] = DAILY_SCHEDULE_PATH,
):
    import sqlite3

    from quant_system.operations.launchd import agent_status

    settings = get_settings()
    schedule = load_schedule(config)
    active, upcoming = schedule_windows(schedule, datetime.now(UTC))
    path = settings.resolved_data_dir / "db/operations.sqlite"
    recent = []
    deliveries = []
    if path.exists():
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            try:
                recent = db.execute(
                    "SELECT session,status,attempts FROM scheduled_daily_runs "
                    "ORDER BY rowid DESC LIMIT 3"
                ).fetchall()
                deliveries = db.execute(
                    "SELECT status,attempts,error_code FROM telegram_outbox "
                    "ORDER BY rowid DESC LIMIT 5"
                ).fetchall()
            except sqlite3.OperationalError:
                pass
    typer.echo(
        json.dumps(
            {
                **agent_status(),
                "telegram_configured": settings.telegram_configured,
                "manual_news": schedule.manual_news,
                "trigger": schedule.trigger,
                "next_trigger_utc": upcoming["due"].isoformat(),
                "current_due_session": active["session"] if active else None,
                "recent_runs": recent,
                "recent_deliveries": deliveries,
                "ranking_policy": schedule.ranking.model_dump(mode="json"),
                "session_jobs": list(schedule.session_jobs),
                "upcoming_jobs": [
                    {"kind": w["kind"], "due_utc": w["due"].isoformat()}
                    for w in session_windows(schedule, datetime.now(UTC))
                    if w["due"] > datetime.now(UTC)
                ][:8],
            },
            indent=2,
        )
    )


@automation_app.command("install")
def automation_install(
    config: Annotated[Path, typer.Option("--config", exists=True)] = DAILY_SCHEDULE_PATH,
):
    from quant_system.operations.launchd import install_agent

    typer.echo(json.dumps(install_agent(get_settings(), config), indent=2))


@automation_app.command("uninstall")
def automation_uninstall():
    from quant_system.operations.launchd import uninstall_agent

    typer.echo(json.dumps(uninstall_agent(), indent=2))


@automation_app.command("collect-intraday")
def automation_collect_intraday(
    symbols: Annotated[str, typer.Option("--symbols", help="Comma-separated candidate symbols.")],
    config: Annotated[Path, typer.Option("--config", exists=True)] = PROJECT_ROOT
    / "configs/sources/intraday.yaml",
):
    from zoneinfo import ZoneInfo

    from quant_system.ingestion.intraday import collect_intraday, load_intraday_settings

    now = datetime.now(UTC)
    session = now.astimezone(ZoneInfo("America/New_York")).date()
    if not NyseSessionClock().calendar.is_session(session):
        typer.echo(json.dumps({"status": "BLOCKED", "reason": "not_an_exchange_session"}))
        raise typer.Exit(2)
    settings = get_settings()
    try:
        with pipeline_lock(settings.resolved_data_dir):
            result = collect_intraday(
                settings,
                load_intraday_settings(config),
                ParquetRepository(settings.resolved_data_dir),
                symbols=tuple(
                    value.strip().upper() for value in symbols.split(",") if value.strip()
                ),
                session=session,
            )
    except Exception as error:
        result = {"status": "FAILED", "error_type": type(error).__name__}
    typer.echo(json.dumps(result, indent=2))
    if result["status"] in {"UNAVAILABLE", "FAILED", "BLOCKED"}:
        raise typer.Exit(2)


@automation_app.command("weekly-review")
def automation_weekly_review():
    from quant_system.operations.weekly import build_weekly_review

    now = datetime.now(UTC)
    settings = get_settings()
    with pipeline_lock(settings.resolved_data_dir):
        result = build_weekly_review(
            settings, session=NyseSessionClock().latest_completed_session(now), cutoff=now
        )
    typer.echo(json.dumps(result, indent=2))


@automation_app.command("model-status")
def automation_model_status(
    config: Annotated[Path, typer.Option("--config", exists=True)] = DAILY_SCHEDULE_PATH,
):
    from quant_system.models.activation import resolve_daily_ranking

    now = datetime.now(UTC)
    settings = get_settings()
    schedule = load_schedule(config)
    _, context = resolve_daily_ranking(
        schedule.ranking,
        database_path=settings.resolved_data_dir / "db/operations.sqlite",
        strategy_config=load_buy_the_dip_config(PROJECT_ROOT / "configs/strategy/buy_the_dip.yaml"),
        session=NyseSessionClock().latest_completed_session(now),
        cutoff=now,
    )
    typer.echo(json.dumps(context, indent=2))


@automation_app.command("telegram-check")
def telegram_check():
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_bot_token.get_secret_value():
        typer.echo(json.dumps({"status": "BOT_TOKEN_NOT_CONFIGURED"}))
        raise typer.Exit(2)
    try:
        result = TelegramClient(settings.telegram_bot_token).check()
        typer.echo(json.dumps(result | {"telegram_configured": settings.telegram_configured}))
    except TelegramError as error:
        typer.echo(json.dumps({"status": "FAILED", "reason": str(error)}))
        raise typer.Exit(2) from None


@automation_app.command("telegram-find-chat")
def telegram_find_chat(
    save: Annotated[
        bool, typer.Option("--save", help="Save an unambiguous private chat ID to .env.")
    ] = False,
):
    from dotenv import set_key

    settings = get_settings()
    if not settings.telegram_bot_token:
        typer.echo(json.dumps({"status": "BOT_TOKEN_NOT_CONFIGURED"}))
        raise typer.Exit(2)
    try:
        chats = TelegramClient(settings.telegram_bot_token).private_chats()
    except TelegramError as error:
        typer.echo(json.dumps({"status": "FAILED", "reason": str(error)}))
        raise typer.Exit(2) from None
    if save and len(chats) == 1 and not settings.telegram_chat_id:
        set_key(str(PROJECT_ROOT / ".env"), "QUANT_TELEGRAM_CHAT_ID", chats[0], quote_mode="never")
        (PROJECT_ROOT / ".env").chmod(0o600)
        get_settings.cache_clear()
    typer.echo(
        json.dumps(
            {
                "status": "READY"
                if get_settings().telegram_configured
                else "NEEDS_CHAT_CONFIGURATION",
                "private_chat_count": len(chats),
                "chat_ids": chats if not save else [],
                "telegram_configured": get_settings().telegram_configured,
            }
        )
    )


@automation_app.command("telegram-test")
def telegram_test():
    settings = get_settings()
    if not settings.telegram_configured:
        typer.echo(json.dumps({"status": "TELEGRAM_NOT_CONFIGURED"}))
        raise typer.Exit(2)
    try:
        message_id = TelegramClient(settings.telegram_bot_token).send(
            settings.telegram_chat_id,
            "量化日报通知测试: 连接成功。人工新闻模式, 按 NYSE 实际收盘后 60 分钟运行。",
        )
        typer.echo(json.dumps({"status": "SENT", "message_id": message_id}))
    except TelegramError as error:
        typer.echo(json.dumps({"status": "FAILED", "reason": str(error)}))
        raise typer.Exit(2) from None


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
        row_count = analytics.connection.execute("SELECT count(*) FROM daily_prices").fetchone()[0]
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
    universe_path: Annotated[
        Path | None,
        typer.Option(
            "--universe",
            exists=True,
            dir_okay=False,
            help="Watchlist YAML whose members and benchmarks should be updated.",
        ),
    ] = None,
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
    try:
        with pipeline_lock(get_settings().resolved_data_dir):
            selected_modes = sum(bool(value) for value in (symbols, all_stored, universe_path))
            if selected_modes > 1:
                raise typer.BadParameter("use only one of --symbols, --all-stored, or --universe")
            source_settings = load_price_source_settings(config_path)
            settings = get_settings()
            repository = ParquetRepository(settings.resolved_data_dir)
            database_path = settings.resolved_data_dir / "db" / "analytics.duckdb"

            if all_stored:
                with DuckDBAnalytics(database_path, repository.daily_prices_root) as analytics:
                    analytics.refresh_views()
                    requested = tuple(analytics.stored_symbols())
            elif universe_path is not None:
                universe = load_watchlist_config(universe_path)
                as_of = NyseSessionClock().latest_completed_session(datetime.now(UTC))
                requested = tuple(
                    sorted(
                        {
                            *(member.symbol for member in universe.active_members(as_of)),
                            *universe.benchmark_symbols,
                        }
                    )
                )
            elif symbols:
                requested = tuple(
                    part.strip().upper() for part in symbols.split(",") if part.strip()
                )
            else:
                requested = source_settings.default_symbols

            provider = YahooFinancePriceAdapter(timeout_seconds=source_settings.timeout_seconds)
            guard = ProviderGuard(
                budget=SQLiteDailyCallBudget(
                    source_settings.daily_call_budget,
                    database_path=settings.resolved_data_dir / "db" / "operations.sqlite",
                    provider=provider.name,
                ),
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
    except PipelineBusy:
        typer.echo(
            json.dumps({"status": "BLOCKED", "blocked_reasons": ["pipeline_already_running"]})
        )
        raise typer.Exit(2) from None


@data_app.command("import-lifecycle")
def import_lifecycle(
    manifest: Annotated[Path, typer.Option("--manifest", exists=True, dir_okay=False)],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Append a reviewed local JSON manifest; preserve event provenance and history."""
    try:
        with pipeline_lock(get_settings().resolved_data_dir):
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            events = tuple(SecurityLifecycleEvent.model_validate(row) for row in payload["events"])
            repository = ParquetRepository(get_settings().resolved_data_dir)
            existing = {
                event.model_dump_json()
                for event in (
                    SecurityLifecycleEvent.model_validate(row)
                    for row in repository.read_security_lifecycle().to_pylist()
                )
            }
            additions = []
            for event in events:
                serialized = event.model_dump_json()
                if serialized not in existing:
                    additions.append(event)
                    existing.add(serialized)
            files = (
                [] if dry_run else repository.write_security_lifecycle(additions, run_id=uuid4())
            )
            typer.echo(
                json.dumps(
                    {
                        "status": "DRY_RUN" if dry_run else "COMPLETED",
                        "events_in_manifest": len(events),
                        "new_events": len(additions),
                        "files_written": len(files),
                    },
                    indent=2,
                )
            )
    except PipelineBusy:
        typer.echo(
            json.dumps({"status": "BLOCKED", "blocked_reasons": ["pipeline_already_running"]})
        )
        raise typer.Exit(2) from None


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
    try:
        with pipeline_lock(get_settings().resolved_data_dir):
            requested = _parse_symbols(symbols)
            source_settings = load_news_source_settings(config_path)
            settings = get_settings()
            repository = ParquetRepository(settings.resolved_data_dir)
            end_utc = _ensure_utc(end or datetime.now(UTC))
            start_utc = _ensure_utc(
                start or (end_utc - timedelta(hours=source_settings.risk.lookback_hours))
            )
            service = build_news_service(settings, source_settings, repository)
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
    except PipelineBusy:
        typer.echo(
            json.dumps({"status": "BLOCKED", "blocked_reasons": ["pipeline_already_running"]})
        )
        raise typer.Exit(2) from None


@data_app.command("check-sources")
def source_check(
    symbols: Annotated[str, typer.Option("--symbols")] = "AAPL,MSFT,NVDA",
    config_path: Annotated[
        Path, typer.Option("--config", exists=True, dir_okay=False)
    ] = PROJECT_ROOT / "configs/sources/news.yaml",
) -> None:
    settings = get_settings()
    result = check_sources(
        settings,
        load_news_source_settings(config_path),
        ParquetRepository(settings.resolved_data_dir),
        _parse_symbols(symbols),
        cutoff_utc=datetime.now(UTC),
    )
    typer.echo(json.dumps(result, indent=2))


@data_app.command("import-company-mappings")
def import_company_mappings(
    manifest: Annotated[Path, typer.Option("--manifest", exists=True, dir_okay=False)],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    try:
        with pipeline_lock(get_settings().resolved_data_dir):
            facts = tuple(
                ReviewedCompanyMapping.model_validate(row)
                for row in json.loads(manifest.read_text())["mappings"]
            )
            repository = ParquetRepository(get_settings().resolved_data_dir)
            existing = {
                ReviewedCompanyMapping.model_validate(row).model_dump_json()
                for row in repository.read_company_mappings().to_pylist()
            }
            additions = []
            for fact in facts:
                if fact.available_at_utc > datetime.now(UTC) or fact.fetched_at_utc > datetime.now(
                    UTC
                ):
                    raise typer.BadParameter("reviewed mapping cannot be imported from the future")
                if fact.model_dump_json() not in existing:
                    additions.append(fact)
                    existing.add(fact.model_dump_json())
            files = [] if dry_run else repository.write_company_mappings(additions, run_id=uuid4())
            typer.echo(
                json.dumps(
                    {
                        "status": "DRY_RUN" if dry_run else "COMPLETED",
                        "new_mappings": len(additions),
                        "files_written": len(files),
                    }
                )
            )
    except PipelineBusy:
        typer.echo(
            json.dumps({"status": "BLOCKED", "blocked_reasons": ["pipeline_already_running"]})
        )
        raise typer.Exit(2) from None


@data_app.command("verify-news")
def verify_news(
    symbols: Annotated[str, typer.Option("--symbols")] = "AAPL,MSFT,NVDA",
    config_path: Annotated[
        Path, typer.Option("--config", exists=True, dir_okay=False)
    ] = PROJECT_ROOT / "configs/sources/news.yaml",
) -> None:
    try:
        with pipeline_lock(get_settings().resolved_data_dir):
            requested = _parse_symbols(symbols)
            if len(set(requested)) > 3:
                raise typer.BadParameter("live verification is limited to three symbols")
            settings = get_settings()
            repository = ParquetRepository(settings.resolved_data_dir)
            config = load_news_source_settings(config_path)
            ready = check_sources(
                settings, config, repository, requested, cutoff_utc=datetime.now(UTC)
            )
            end = datetime.now(UTC)
            report, path = build_news_service(settings, config, repository).run(
                requested, start_utc=end - timedelta(hours=config.risk.lookback_hours), end_utc=end
            )
            outcomes = set(report.coverage_results.values())
            verification = (
                "SUCCESS"
                if outcomes and outcomes <= {"SUCCESS", "EMPTY"}
                else ("UNAVAILABLE" if outcomes == {"UNAVAILABLE"} else "PARTIAL")
            )
            result = report.to_dict() | {
                "readiness": ready,
                "report_path": str(path),
                "verification": verification,
            }
            typer.echo(json.dumps(result, indent=2))
    except PipelineBusy:
        typer.echo(
            json.dumps({"status": "BLOCKED", "blocked_reasons": ["pipeline_already_running"]})
        )
        raise typer.Exit(2) from None


@data_app.command("update-daily")
def update_daily(
    as_of: Annotated[datetime | None, typer.Option("--date")] = None,
    manual_news: Annotated[
        bool,
        typer.Option("--manual-news", help="Skip news APIs and export a manual research package."),
    ] = False,
    replay: Annotated[
        bool, typer.Option("--replay", help="Historical replay; skip network acquisition.")
    ] = False,
    retry: Annotated[
        bool,
        typer.Option("--retry", help="Retry the current request with new acquisition attempt IDs."),
    ] = False,
    refresh_window: Annotated[
        datetime | None,
        typer.Option(
            "--refresh-window",
            formats=["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S"],
        ),
    ] = None,
    universe: Annotated[
        list[Path] | None, typer.Option("--universe", exists=True, dir_okay=False)
    ] = None,
    price_config: Annotated[
        Path, typer.Option("--price-config", exists=True, dir_okay=False)
    ] = PROJECT_ROOT / "configs/sources/prices.yaml",
    news_config: Annotated[
        Path, typer.Option("--news-config", exists=True, dir_okay=False)
    ] = PROJECT_ROOT / "configs/sources/news.yaml",
) -> None:
    result = run_daily_pipeline(
        get_settings(),
        signal_session=as_of.date() if as_of else None,
        mode="replay" if replay else "live",
        universe_paths=tuple(universe) if universe else None,
        price_config_path=price_config,
        news_config_path=news_config,
        refresh_window=_ensure_utc(refresh_window) if refresh_window else None,
        retry=retry,
        manual_news=manual_news,
    )
    typer.echo(json.dumps(result, indent=2, default=str))
    if result["status"] in {"BLOCKED", "FAILED"}:
        raise typer.Exit(2)


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
            coverage=load_coverage(repository),
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


@backtest_app.command("daily-scan")
def backtest_daily_scan(
    start: Annotated[datetime, typer.Option("--start", help="First date, including holidays.")],
    end: Annotated[datetime, typer.Option("--end", help="Last completed evaluation date.")],
) -> None:
    """Replay all watchlists; export scores and outcome labels with conservative no-fill."""
    settings = get_settings()
    summary = run_daily_scan_research(
        repository=ParquetRepository(settings.resolved_data_dir),
        database_path=settings.resolved_data_dir / "db" / "daily_scan_research.duckdb",
        report_root=settings.resolved_data_dir / "reports" / "backtests",
        start=start.date(), end=end.date(),
        strategy_config=load_buy_the_dip_config(
            PROJECT_ROOT / "configs" / "strategy" / "buy_the_dip.yaml"
        ),
        digest_config=load_daily_digest_config(),
        progress=lambda message: typer.echo(message, err=True),
    )
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))


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
    news_cutoff: Annotated[
        datetime | None,
        typer.Option(
            "--news-cutoff",
            formats=["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S"],
            help="Explicit UTC forward news cutoff; defaults to historical price cutoff.",
        ),
    ] = None,
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
        news_cutoff_utc=_ensure_utc(news_cutoff) if news_cutoff else None,
        include_calibration=calibration,
        benchmark_path=benchmark_path,
        include_model_ranking=model_ranking,
        model_settings=load_ranking_baseline_settings(model_config_path) if model_ranking else None,
    )
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@decision_app.command("satellite")
def decision_satellite(
    as_of: Annotated[
        datetime | None,
        typer.Option(
            "--date",
            help="Satellite scan signal session in YYYY-MM-DD form.",
        ),
    ] = None,
    satellite_universe_path: Annotated[
        Path,
        typer.Option(
            "--satellite-universe",
            exists=True,
            dir_okay=False,
            help="AI satellite watchlist YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "universe" / "ai_satellite_watchlist.yaml",
    ai_universe_path: Annotated[
        Path,
        typer.Option(
            "--ai-universe",
            exists=True,
            dir_okay=False,
            help="Core AI alpha watchlist YAML used for context.",
        ),
    ] = PROJECT_ROOT / "configs" / "universe" / "ai_watchlist.yaml",
    hedge_universe_path: Annotated[
        Path,
        typer.Option(
            "--hedge-universe",
            exists=True,
            dir_okay=False,
            help="Defensive hedge overlay YAML used for context.",
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
    news_cutoff: Annotated[
        datetime | None,
        typer.Option(
            "--news-cutoff",
            formats=["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S"],
            help="Explicit UTC forward news cutoff; defaults to historical price cutoff.",
        ),
    ] = None,
) -> None:
    """Generate a separate AI satellite scan without widening the core watchlist."""
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
        universe_paths=(satellite_universe_path,),
        calibration_universe_paths=(
            ai_universe_path,
            satellite_universe_path,
            hedge_universe_path,
        ),
        signal_session=signal_session,
        config=load_buy_the_dip_config(strategy_config_path),
        include_news_risk=news_risk,
        news_lookback_hours=news_source_settings.risk.lookback_hours,
        news_cutoff_utc=_ensure_utc(news_cutoff) if news_cutoff else None,
        include_calibration=calibration,
        benchmark_path=benchmark_path,
        include_model_ranking=model_ranking,
        model_settings=load_ranking_baseline_settings(model_config_path) if model_ranking else None,
        report_stem="satellite",
        report_title="AI Satellite Scan",
    )
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@decision_app.command("research-list")
def decision_research_list(
    as_of: Annotated[
        datetime | None,
        typer.Option(
            "--date",
            help="Research-list signal session in YYYY-MM-DD form.",
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
    max_symbols: Annotated[
        int,
        typer.Option("--max-symbols", min=1, max=50, help="Maximum research symbols."),
    ] = 20,
    news_summary_days: Annotated[
        int,
        typer.Option("--news-days", min=1, max=30, help="News prompt lookback days."),
    ] = 7,
    news_cutoff: Annotated[
        datetime | None,
        typer.Option(
            "--news-cutoff",
            formats=["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S"],
            help="Explicit UTC forward news cutoff; defaults to historical price cutoff.",
        ),
    ] = None,
) -> None:
    """Generate a wider manual research list and copy/paste news prompt."""
    signal_session = (
        as_of.date()
        if as_of is not None
        else NyseSessionClock().latest_completed_session(datetime.now(UTC))
    )
    news_source_settings = load_news_source_settings(news_config_path)
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    report, _artifacts = run_research_list_workflow(
        repository=repository,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        report_root=settings.resolved_data_dir / "reports" / "daily",
        universe_paths=(ai_universe_path, hedge_universe_path),
        signal_session=signal_session,
        config=load_buy_the_dip_config(strategy_config_path),
        include_news_risk=news_risk,
        news_lookback_hours=news_source_settings.risk.lookback_hours,
        news_cutoff_utc=_ensure_utc(news_cutoff) if news_cutoff else None,
        benchmark_path=benchmark_path,
        include_model_ranking=model_ranking,
        model_settings=load_ranking_baseline_settings(model_config_path) if model_ranking else None,
        max_symbols=max_symbols,
        news_summary_days=news_summary_days,
    )
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@data_app.command("import-cnn")
def data_import_cnn(
    source_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Append a sourced CNN transcription with actual local import availability."""
    from quant_system.ingestion.market_context import import_manual_cnn

    settings = get_settings()
    try:
        if source_path.stat().st_size > 100_000:
            raise ValueError("manual CNN input exceeds 100 KB")
        with pipeline_lock(settings.resolved_data_dir):
            result = import_manual_cnn(
                json.loads(source_path.read_text(encoding="utf-8")),
                repository=ParquetRepository(settings.resolved_data_dir),
            )
    except (ValueError, OSError, PipelineBusy) as error:
        raise typer.BadParameter(f"manual CNN import rejected: {type(error).__name__}") from None
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@decision_app.command("daily-workbench")
def decision_daily_workbench(
    digest_config_path: Annotated[
        Path, typer.Option("--digest-config", exists=True, dir_okay=False)
    ] = PROJECT_ROOT / "configs/reports/daily_digest.yaml",
    manual_news: Annotated[
        bool, typer.Option("--manual-news", help="Export manual research; keep news unverified.")
    ] = False,
    as_of: Annotated[
        datetime | None,
        typer.Option(
            "--date",
            help="Daily workbench signal session in YYYY-MM-DD form.",
        ),
    ] = None,
    ai_universe_path: Annotated[
        Path | None,
        typer.Option(
            "--ai-universe",
            exists=True,
            dir_okay=False,
            help="AI alpha watchlist YAML.",
        ),
    ] = None,
    hedge_universe_path: Annotated[
        Path | None,
        typer.Option(
            "--hedge-universe",
            exists=True,
            dir_okay=False,
            help="Defensive hedge overlay YAML.",
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
    leverage_config_path: Annotated[
        Path,
        typer.Option(
            "--leverage-config",
            exists=True,
            dir_okay=False,
            help="Manual 2x overlay strategy YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "strategy" / "leverage_overlay.yaml",
    leverage_universe_path: Annotated[
        Path,
        typer.Option(
            "--leverage-universe",
            exists=True,
            dir_okay=False,
            help="Manual 2x overlay universe YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "universe" / "leverage_overlay_universe.yaml",
    max_research_symbols: Annotated[
        int,
        typer.Option("--max-research-symbols", min=1, max=50),
    ] = 20,
    news_summary_days: Annotated[
        int,
        typer.Option("--news-days", min=1, max=30, help="News prompt lookback days."),
    ] = 7,
    positions: Annotated[
        bool,
        typer.Option("--positions/--no-positions", help="Include manual position checks."),
    ] = True,
    news_cutoff: Annotated[
        datetime | None,
        typer.Option(
            "--news-cutoff",
            formats=["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S"],
            help="Explicit UTC forward news cutoff; defaults to historical price cutoff.",
        ),
    ] = None,
) -> None:
    """Run premarket, research, 2x overlay, positions, and write daily_index."""
    signal_session = (
        as_of.date()
        if as_of is not None
        else NyseSessionClock().latest_completed_session(datetime.now(UTC))
    )
    news_source_settings = load_news_source_settings(news_config_path)
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    from quant_system.universe.config import load_daily_digest_config

    digest_config = load_daily_digest_config(digest_config_path)
    universe_paths = digest_config.universe_paths
    if ai_universe_path is not None or hedge_universe_path is not None:
        universe_paths = (
            ai_universe_path or PROJECT_ROOT / "configs/universe/ai_watchlist.yaml",
            hedge_universe_path or PROJECT_ROOT / "configs/universe/hedge_overlay.yaml",
        )
    report, _artifacts = run_daily_workbench_workflow(
        repository=repository,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        operations_database_path=settings.resolved_data_dir / "db" / "operations.sqlite",
        report_root=settings.resolved_data_dir / "reports" / "daily",
        signal_session=signal_session,
        universe_paths=universe_paths,
        digest_config=digest_config,
        strategy_config=load_buy_the_dip_config(strategy_config_path),
        include_news_risk=news_risk,
        news_lookback_hours=news_source_settings.risk.lookback_hours,
        news_cutoff_utc=_ensure_utc(news_cutoff) if news_cutoff else None,
        manual_news=manual_news,
        benchmark_path=benchmark_path,
        include_model_ranking=model_ranking,
        model_settings=load_ranking_baseline_settings(model_config_path) if model_ranking else None,
        leverage_config=load_leverage_overlay_config(leverage_config_path),
        leverage_universe=load_leverage_overlay_universe_config(leverage_universe_path),
        max_research_symbols=max_research_symbols,
        news_summary_days=news_summary_days,
        include_positions=positions,
    )
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@decision_app.command("import-manual-research")
def decision_import_manual_research(
    package: Annotated[Path, typer.Option("--package", exists=True, dir_okay=False)],
    result: Annotated[Path, typer.Option("--result", exists=True, dir_okay=False)],
    reviewed_by: Annotated[
        str, typer.Option("--reviewed-by", help="Human source/provenance reviewer.")
    ],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Validate reviewed web research, append provenance and generate combined reports."""
    settings = get_settings()
    try:
        with pipeline_lock(settings.resolved_data_dir):
            output = import_manual_research(
                repository=ParquetRepository(settings.resolved_data_dir),
                database_path=settings.resolved_data_dir / "db/analytics.duckdb",
                report_root=settings.resolved_data_dir / "reports/daily",
                package_path=package,
                result_path=result,
                reviewed_by=reviewed_by,
                dry_run=dry_run,
            )
        typer.echo(json.dumps(output, indent=2))
    except PipelineBusy:
        typer.echo(
            json.dumps({"status": "BLOCKED", "blocked_reasons": ["pipeline_already_running"]})
        )
        raise typer.Exit(2) from None
    except (ValueError, OSError, KeyError, TypeError) as error:
        typer.echo(
            json.dumps(
                {
                    "status": "REJECTED",
                    "error_type": type(error).__name__,
                    "reason": str(error)[:1000],
                }
            )
        )
        raise typer.Exit(2) from None


@decision_app.command("combine-manual-research")
def decision_combine_manual_research(
    package: Annotated[Path, typer.Option("--package", exists=True, dir_okay=False)],
    cutoff: Annotated[
        str | None, typer.Option("--cutoff", help="UTC knowledge cutoff; defaults to now.")
    ] = None,
) -> None:
    """Regenerate a research-only report from facts visible at the requested cutoff."""
    settings = get_settings()
    try:
        with pipeline_lock(settings.resolved_data_dir):
            output = combine_manual_research(
                repository=ParquetRepository(settings.resolved_data_dir),
                database_path=settings.resolved_data_dir / "db/analytics.duckdb",
                report_root=settings.resolved_data_dir / "reports/daily",
                package_path=package,
                cutoff_utc=_parse_cli_datetime(cutoff) if cutoff else None,
            )
        typer.echo(json.dumps({"status": "RESEARCH_ONLY", **output}, indent=2))
    except PipelineBusy:
        typer.echo(
            json.dumps({"status": "BLOCKED", "blocked_reasons": ["pipeline_already_running"]})
        )
        raise typer.Exit(2) from None
    except (ValueError, OSError, KeyError, TypeError) as error:
        typer.echo(
            json.dumps(
                {
                    "status": "REJECTED",
                    "error_type": type(error).__name__,
                    "reason": str(error)[:1000],
                }
            )
        )
        raise typer.Exit(2) from None


@decision_app.command("leverage-overlay")
def decision_leverage_overlay(
    as_of: Annotated[
        datetime | None,
        typer.Option(
            "--date",
            help="2x overlay signal session in YYYY-MM-DD form.",
        ),
    ] = None,
    underlying: Annotated[
        str,
        typer.Option("--underlying", help="Underlying common stock symbol, e.g. MU."),
    ] = "MU",
    leveraged_etf: Annotated[
        str | None,
        typer.Option("--leveraged-etf", help="Optional 2x ETF symbol, e.g. MUU."),
    ] = None,
    sector_etf: Annotated[
        str,
        typer.Option("--sector-etf", help="Sector/theme ETF confirmation symbol."),
    ] = "SOXX",
    market_symbol: Annotated[
        str,
        typer.Option("--market", help="Broad market risk-on proxy."),
    ] = "QQQ",
    catalyst_confirmed: Annotated[
        bool,
        typer.Option(
            "--catalyst-confirmed/--catalyst-review-needed",
            help="Whether human/news review already confirmed a valid catalyst.",
        ),
    ] = False,
    leverage_config_path: Annotated[
        Path,
        typer.Option(
            "--leverage-config",
            exists=True,
            dir_okay=False,
            help="Manual 2x overlay strategy YAML.",
        ),
    ] = PROJECT_ROOT / "configs" / "strategy" / "leverage_overlay.yaml",
    scan_all: Annotated[
        bool,
        typer.Option(
            "--all/--single",
            help="Scan the configured manual 2x overlay universe instead of one pair.",
        ),
    ] = False,
    leverage_universe_path: Annotated[
        Path,
        typer.Option(
            "--leverage-universe",
            exists=True,
            dir_okay=False,
            help="Manual 2x overlay universe YAML used with --all.",
        ),
    ] = PROJECT_ROOT / "configs" / "universe" / "leverage_overlay_universe.yaml",
) -> None:
    """Evaluate a subsidiary manual-only 2x ETF overlay."""
    signal_session = (
        as_of.date()
        if as_of is not None
        else NyseSessionClock().latest_completed_session(datetime.now(UTC))
    )
    settings = get_settings()
    repository = ParquetRepository(settings.resolved_data_dir)
    config = load_leverage_overlay_config(leverage_config_path)
    if scan_all:
        report, _artifacts = run_leverage_overlay_universe_workflow(
            repository=repository,
            database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
            report_root=settings.resolved_data_dir / "reports" / "daily",
            signal_session=signal_session,
            config=config,
            universe=load_leverage_overlay_universe_config(leverage_universe_path),
            catalyst_confirmed=catalyst_confirmed,
        )
    else:
        report, _artifacts = run_leverage_overlay_workflow(
            repository=repository,
            database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
            report_root=settings.resolved_data_dir / "reports" / "daily",
            signal_session=signal_session,
            underlying_symbol=underlying,
            leveraged_etf_symbol=leveraged_etf,
            sector_etf=sector_etf,
            market_symbol=market_symbol,
            config=config,
            catalyst_confirmed=catalyst_confirmed,
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
            help=("UTC fill timestamp, e.g. 2026-07-27T13:30:00+00:00. Defaults to now."),
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
