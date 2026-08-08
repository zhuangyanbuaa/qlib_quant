"""Premarket daily workbench built on canonical scan rules."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pandas as pd

from quant_system.backtest.workflow import load_feature_history
from quant_system.decision.reports import DailyReportArtifacts, write_premarket_report
from quant_system.domain.clocks import NyseSessionClock
from quant_system.sentiment.risk import NewsRiskAssessment, assess_news_risk
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.strategy.buy_the_dip import BuyTheDipStrategy
from quant_system.strategy.config import BuyTheDipConfig
from quant_system.universe.config import load_watchlist_config


def load_decision_universe(paths: tuple[Path, ...]) -> tuple[tuple[str, ...], dict[str, str]]:
    """Load unique scan symbols and classify them as AI alpha or hedge overlay."""
    symbols: list[str] = []
    roles: dict[str, str] = {}
    for path in paths:
        config = load_watchlist_config(path)
        role = "hedge_overlay" if config.universe_type == "HEDGE_OVERLAY" else "ai_alpha"
        for member in config.symbols:
            symbols.append(member.symbol)
            roles.setdefault(member.symbol, role)
        symbols.extend(config.benchmark_symbols)
        for benchmark_symbol in config.benchmark_symbols:
            roles.setdefault(benchmark_symbol, "benchmark")
    return tuple(sorted(set(symbols))), roles


def run_premarket_workflow(
    *,
    repository: ParquetRepository,
    database_path: Path,
    report_root: Path,
    universe_paths: tuple[Path, ...],
    signal_session: date,
    config: BuyTheDipConfig,
    include_news_risk: bool,
    news_lookback_hours: int,
    run_id: UUID | None = None,
    generated_at_utc: datetime | None = None,
) -> tuple[dict[str, Any], DailyReportArtifacts]:
    """Generate and persist a daily premarket plan without recording human actions."""
    run_id = run_id or uuid4()
    generated_at_utc = _ensure_utc(generated_at_utc or datetime.now(UTC))
    symbols, role_by_symbol = load_decision_universe(universe_paths)
    benchmark_symbol = config.strategy.benchmark_symbol.upper()
    features = load_feature_history(
        repository=repository,
        database_path=database_path,
        symbols=symbols,
        benchmark_symbol=benchmark_symbol,
        start=signal_session,
        end=signal_session,
    )

    news_risk = _load_news_risk(
        repository=repository,
        database_path=database_path,
        symbols=symbols,
        signal_session=signal_session,
        include_news_risk=include_news_risk,
        news_lookback_hours=news_lookback_hours,
    )
    strategy = BuyTheDipStrategy(config.strategy)
    annotated = strategy.annotate(features)
    signals = strategy.generate_signals(
        features,
        as_of=signal_session,
        news_risk=news_risk,
    )
    candidate_rows = _candidate_rows(
        signals,
        role_by_symbol=role_by_symbol,
        config=config,
    )
    market_regime = _market_regime(
        annotated,
        benchmark_symbol=benchmark_symbol,
        signal_session=signal_session,
    )
    clock = NyseSessionClock()
    earliest_order_session = clock.next_session(signal_session)
    report: dict[str, Any] = {
        "metadata": {
            "run_id": str(run_id),
            "generated_at_utc": generated_at_utc.isoformat(),
            "signal_session": signal_session.isoformat(),
            "data_cutoff_utc": clock.session_close_utc(signal_session).isoformat(),
            "earliest_order_session": earliest_order_session.isoformat(),
            "earliest_order_time_utc": clock.session_open_utc(
                earliest_order_session
            ).isoformat(),
            "symbol_count": len(symbols),
            "news_risk_enabled": include_news_risk,
            "model_status": "RULES_ONLY_NO_MODEL_ATTACHED",
        },
        "counts": _counts(candidate_rows),
        "portfolio_posture": _portfolio_posture(
            market_regime=market_regime,
            candidate_rows=candidate_rows,
        ),
        "candidates": candidate_rows,
    }
    artifacts = write_premarket_report(
        report=report,
        candidate_rows=candidate_rows,
        report_root=report_root,
        signal_session=signal_session,
        run_id=run_id,
    )
    report["artifacts"] = {
        "directory": str(artifacts.directory),
        "json": str(artifacts.json_path),
        "csv": str(artifacts.csv_path),
        "markdown": str(artifacts.markdown_path),
        "html": str(artifacts.html_path),
    }
    artifacts = write_premarket_report(
        report=report,
        candidate_rows=candidate_rows,
        report_root=report_root,
        signal_session=signal_session,
        run_id=run_id,
    )
    return report, artifacts


def _load_news_risk(
    *,
    repository: ParquetRepository,
    database_path: Path,
    symbols: tuple[str, ...],
    signal_session: date,
    include_news_risk: bool,
    news_lookback_hours: int,
) -> dict[str, NewsRiskAssessment] | None:
    if not include_news_risk:
        return None
    cutoff = NyseSessionClock().available_at_utc(signal_session)
    with DuckDBAnalytics(
        database_path,
        repository.daily_prices_root,
        repository.news_articles_root,
        repository.company_events_root,
    ) as analytics:
        analytics.refresh_views()
        return assess_news_risk(
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


def _candidate_rows(
    signals: list[Any],
    *,
    role_by_symbol: dict[str, str],
    config: BuyTheDipConfig,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank, signal in enumerate(signals, start=1):
        stop_price = max(
            0.01,
            signal.signal_close - config.execution.stop_atr_multiple * signal.atr20,
        )
        target_price = signal.signal_close + config.execution.target_atr_multiple * signal.atr20
        rows.append(
            {
                "rank": rank,
                "signal_id": str(signal.signal_id),
                "symbol": signal.symbol,
                "universe_role": role_by_symbol.get(signal.symbol, "unclassified"),
                "signal_session": signal.signal_session.isoformat(),
                "earliest_order_session": signal.earliest_order_session.isoformat(),
                "earliest_order_time_utc": signal.earliest_order_time_utc.isoformat(),
                "score": float(signal.score),
                "signal_close": float(signal.signal_close),
                "atr20": float(signal.atr20),
                "limit_price": round(float(signal.signal_close), 4),
                "max_gap_up_price": round(
                    float(signal.signal_close) * (1 + config.execution.maximum_gap_up),
                    4,
                ),
                "gap_down_cancel_below": round(
                    float(signal.signal_close) * (1 - config.execution.maximum_gap_down),
                    4,
                ),
                "stop_price": round(stop_price, 4),
                "target_price": round(target_price, 4),
                "market_regime": str(signal.market_regime),
                "news_risk": signal.news_risk,
                "reason_count": len(signal.reasons),
                "reasons": ";".join(signal.reasons),
                "recommended_action": _recommended_action(
                    role_by_symbol.get(signal.symbol, "unclassified"),
                    signal.news_risk,
                ),
            }
        )
    return rows


def _recommended_action(universe_role: str, news_risk: str) -> str:
    if news_risk == "MEDIUM":
        return "DEFER_FOR_MANUAL_NEWS_REVIEW"
    if universe_role == "hedge_overlay":
        return "REVIEW_AS_DEFENSIVE_OVERLAY"
    return "PREPARE_MANUAL_CONDITIONAL_ORDER"


def _counts(candidate_rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "candidate_count": len(candidate_rows),
        "ai_candidate_count": sum(
            row["universe_role"] == "ai_alpha" for row in candidate_rows
        ),
        "hedge_candidate_count": sum(
            row["universe_role"] == "hedge_overlay" for row in candidate_rows
        ),
        "medium_news_risk_count": sum(
            row["news_risk"] == "MEDIUM" for row in candidate_rows
        ),
    }


def _market_regime(
    annotated: pd.DataFrame,
    *,
    benchmark_symbol: str,
    signal_session: date,
) -> str:
    benchmark = annotated.loc[
        (annotated["symbol"] == benchmark_symbol)
        & (annotated["session_date_ny"].dt.date == signal_session)
    ]
    if benchmark.empty:
        return "UNKNOWN"
    return str(benchmark.iloc[-1]["market_regime"])


def _portfolio_posture(
    *,
    market_regime: str,
    candidate_rows: list[dict[str, Any]],
) -> dict[str, str]:
    hedge_count = sum(row["universe_role"] == "hedge_overlay" for row in candidate_rows)
    ai_count = sum(row["universe_role"] == "ai_alpha" for row in candidate_rows)
    if market_regime == "RED":
        return {
            "status": "DEFENSIVE_REVIEW",
            "market_regime": market_regime,
            "message": (
                "Benchmark regime is RED. Do not add new Buy-the-Dip alpha exposure; "
                "review cash, stops, and hedge overlay manually."
            ),
        }
    if ai_count == 0 and hedge_count > 0:
        return {
            "status": "DEFENSIVE_OVERLAY_AVAILABLE",
            "market_regime": market_regime,
            "message": (
                "No AI alpha candidates passed rules, but hedge overlay candidates exist. "
                "If portfolio risk is elevated, review defensive rotation manually."
            ),
        }
    if ai_count == 0:
        return {
            "status": "CASH_FIRST",
            "market_regime": market_regime,
            "message": (
                "No rules-approved AI alpha candidates. Keep reserve cash discipline and "
                "avoid forcing trades."
            ),
        }
    return {
        "status": "RISK_ON_SELECTIVE",
        "market_regime": market_regime,
        "message": (
            "Rules-approved candidates exist. Prepare manual conditional-order drafts, "
            "then let pre-open/open gates cancel abnormal setups."
        ),
    }


def _ensure_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
