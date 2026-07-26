"""High-level scan and backtest workflows used by the CLI."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pandas as pd

from quant_system.backtest.engine import BacktestEngine
from quant_system.backtest.metrics import calculate_metrics
from quant_system.backtest.reports import BacktestArtifacts, write_backtest_report
from quant_system.backtest.stress import run_stress_grid
from quant_system.features.technical import build_technical_features
from quant_system.sentiment.risk import NewsRiskAssessment, assess_news_risk
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.strategy.buy_the_dip import BuyTheDipStrategy
from quant_system.strategy.config import BuyTheDipConfig


def load_feature_history(
    *,
    repository: ParquetRepository,
    database_path: Path,
    symbols: tuple[str, ...],
    benchmark_symbol: str,
    start: date,
    end: date,
    warmup_calendar_days: int = 450,
) -> pd.DataFrame:
    """Load enough pre-start history to build 200-session causal features."""
    requested = tuple(sorted(set(symbols) | {benchmark_symbol.upper()}))
    with DuckDBAnalytics(database_path, repository.daily_prices_root) as analytics:
        analytics.refresh_views()
        prices = analytics.query_price_history(
            requested,
            start_date=start - timedelta(days=warmup_calendar_days),
            end_date=end,
        )
    if prices.empty:
        raise ValueError("no daily prices found for requested symbols and period")
    return build_technical_features(prices, benchmark_symbol=benchmark_symbol)


def run_backtest_workflow(
    *,
    repository: ParquetRepository,
    database_path: Path,
    report_root: Path,
    symbols: tuple[str, ...],
    start: date,
    end: date,
    config: BuyTheDipConfig,
    include_stress: bool,
    run_id: UUID | None = None,
) -> tuple[dict[str, object], BacktestArtifacts]:
    """Run the rules baseline and persist a complete artifact bundle."""
    if start > end:
        raise ValueError("backtest start must not follow end")
    run_id = run_id or uuid4()
    benchmark_symbol = config.strategy.benchmark_symbol.upper()
    features = load_feature_history(
        repository=repository,
        database_path=database_path,
        symbols=symbols,
        benchmark_symbol=benchmark_symbol,
        start=start,
        end=end,
    )
    strategy = BuyTheDipStrategy(config.strategy)
    result = BacktestEngine(strategy, config).run(features, start=start, end=end)
    benchmark_prices = features.loc[
        (features["symbol"] == benchmark_symbol)
        & features["session_date_ny"].dt.date.between(start, end, inclusive="both"),
        ["session_date_ny", "close"],
    ]
    metrics = calculate_metrics(
        result,
        benchmark_prices=benchmark_prices,
        initial_cash=config.portfolio.initial_cash,
    )
    stress_results = (
        run_stress_grid(
            features,
            benchmark_prices,
            config,
            start=start,
            end=end,
        )
        if include_stress
        else None
    )
    artifacts = write_backtest_report(
        result=result,
        metrics=metrics,
        config=config,
        run_id=run_id,
        start=start,
        end=end,
        symbols=tuple(sorted(set(symbols))),
        report_root=report_root,
        stress_results=stress_results,
    )
    summary = {
        "run_id": str(run_id),
        "status": "COMPLETED",
        "universe_type": config.backtest.universe_type,
        "symbol_count": len(set(symbols)),
        "metrics": metrics,
        "open_positions": list(result.open_positions),
        "report_directory": str(artifacts.directory),
    }
    return summary, artifacts


def scan_workflow(
    *,
    repository: ParquetRepository,
    database_path: Path,
    symbols: tuple[str, ...],
    as_of: date,
    config: BuyTheDipConfig,
    include_news_risk: bool = False,
    news_lookback_hours: int = 72,
) -> list[dict[str, object]]:
    """Generate one-date candidates through the exact backtest strategy code."""
    features = load_feature_history(
        repository=repository,
        database_path=database_path,
        symbols=symbols,
        benchmark_symbol=config.strategy.benchmark_symbol,
        start=as_of,
        end=as_of,
    )
    news_risk: dict[str, NewsRiskAssessment] | None = None
    if include_news_risk:
        from quant_system.domain.clocks import NyseSessionClock

        cutoff = NyseSessionClock().available_at_utc(as_of)
        with DuckDBAnalytics(database_path, repository.daily_prices_root) as analytics:
            analytics.refresh_views()
            news_risk = assess_news_risk(
                symbols=symbols,
                articles=analytics.query_news_articles(
                    symbols,
                    cutoff_utc=cutoff,
                    lookback_hours=news_lookback_hours,
                ),
                events=analytics.query_company_events(
                    symbols,
                    cutoff_utc=cutoff,
                    lookback_hours=news_lookback_hours,
                ),
                cutoff_utc=cutoff,
                lookback_hours=news_lookback_hours,
            )
    signals = BuyTheDipStrategy(config.strategy).generate_signals(
        features,
        as_of=as_of,
        news_risk=news_risk,
    )
    return [signal.model_dump(mode="json") for signal in signals]
