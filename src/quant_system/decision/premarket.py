"""Premarket daily workbench built on canonical scan rules."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pandas as pd

from quant_system.backtest.workflow import load_feature_history
from quant_system.decision.hierarchy import run_hierarchy_diagnostics_workflow
from quant_system.decision.reports import DailyReportArtifacts, write_premarket_report
from quant_system.domain.clocks import NyseSessionClock
from quant_system.models.config import RankingBaselineSettings
from quant_system.models.daily_ranker import (
    attach_model_scores,
    rank_daily_candidates_with_lightgbm,
)
from quant_system.sentiment.risk import NewsRiskAssessment, assess_news_risk
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.strategy.buy_the_dip import BuyTheDipStrategy
from quant_system.strategy.calibration import TieredCandidateSignal, generate_tiered_candidates
from quant_system.strategy.config import BuyTheDipConfig
from quant_system.universe.config import load_watchlist_config

POSITIVE_SYMBOL_STATES = {"REVERSAL_ATTEMPT", "CONFIRMED_REVERSAL", "UPTREND"}
POSITIVE_ROTATION_STATES = {"LEADING", "IMPROVING"}
DEFENSIVE_OVERLAY_REVIEW_THEMES = {"defensive_healthcare", "defensive_staples"}


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
    include_calibration: bool = False,
    benchmark_path: Path | None = None,
    include_model_ranking: bool = False,
    model_settings: RankingBaselineSettings | None = None,
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
    signals = _exclude_context_only_benchmarks(signals, role_by_symbol)
    candidate_rows = _candidate_rows(
        signals,
        role_by_symbol=role_by_symbol,
        config=config,
    )
    hierarchy_context: dict[str, Any] | None = None
    hierarchy_artifacts: DailyReportArtifacts | None = None
    calibration_rows: list[dict[str, Any]] = []
    if include_calibration and benchmark_path is not None:
        hierarchy_report, hierarchy_artifacts = run_hierarchy_diagnostics_workflow(
            repository=repository,
            database_path=database_path,
            report_root=report_root,
            universe_paths=universe_paths,
            benchmark_path=benchmark_path,
            as_of=signal_session,
            benchmark_symbol=benchmark_symbol,
            run_id=run_id,
            generated_at_utc=generated_at_utc,
        )
        hierarchy_context = hierarchy_report["strategy_context"]
        tiered_candidates = generate_tiered_candidates(
            features=features,
            as_of=signal_session,
            base_config=config.strategy,
            news_risk=news_risk,
        )
        tiered_candidates = _exclude_context_only_tiered_benchmarks(
            tiered_candidates,
            role_by_symbol,
        )
        calibration_rows = _calibration_candidate_rows(
            tiered_candidates,
            role_by_symbol=role_by_symbol,
            config=config,
            strategy_context=hierarchy_context,
            hierarchy_rows=hierarchy_report["rows"],
            rotation_rows=hierarchy_report.get("rotation_rows", []),
        )
    model_rank_rows = [*candidate_rows, *calibration_rows]
    model_rank_context = _model_rank_context(
        features=features,
        rows=model_rank_rows,
        signal_session=signal_session,
        config=config,
        include_model_ranking=include_model_ranking,
        model_settings=model_settings,
    )
    ranking = model_rank_context.pop("_ranking", None)
    if ranking is not None:
        attach_model_scores(candidate_rows, ranking)
        attach_model_scores(calibration_rows, ranking)
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
            "calibration_enabled": include_calibration and benchmark_path is not None,
            "model_status": _metadata_model_status(
                has_calibration=hierarchy_context is not None,
                model_rank_context=model_rank_context,
            ),
        },
        "counts": _counts(candidate_rows),
        "calibration_counts": _calibration_counts(calibration_rows),
        "portfolio_posture": _portfolio_posture(
            market_regime=market_regime,
            candidate_rows=candidate_rows,
        ),
        "strategy_context": hierarchy_context
        or {
            "candidate_tier_context": "BASELINE",
            "risk_multiplier_hint": 1.0,
            "message": "Calibration context disabled; use canonical baseline rules.",
        },
        "model_rank_context": model_rank_context,
        "candidates": candidate_rows,
        "calibration_candidate_tiers": calibration_rows,
    }
    if hierarchy_artifacts is not None:
        report["hierarchy_artifacts"] = {
            "directory": str(hierarchy_artifacts.directory),
            "json": str(hierarchy_artifacts.json_path),
            "csv": str(hierarchy_artifacts.csv_path),
            "markdown": str(hierarchy_artifacts.markdown_path),
            "html": str(hierarchy_artifacts.html_path),
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


def _model_rank_context(
    *,
    features: pd.DataFrame,
    rows: list[dict[str, Any]],
    signal_session: date,
    config: BuyTheDipConfig,
    include_model_ranking: bool,
    model_settings: RankingBaselineSettings | None,
) -> dict[str, Any]:
    if not include_model_ranking:
        return {
            "model": "lightgbm_daily_context_v1",
            "status": "DISABLED",
            "decision_scope": "RANK_CONTEXT_ONLY_DOES_NOT_CHANGE_ACTIONS",
        }
    if model_settings is None:
        return {
            "model": "lightgbm_daily_context_v1",
            "status": "UNAVAILABLE_MODEL_SETTINGS_MISSING",
            "decision_scope": "RANK_CONTEXT_ONLY_DOES_NOT_CHANGE_ACTIONS",
        }
    ranking = rank_daily_candidates_with_lightgbm(
        features=features,
        candidate_rows=rows,
        strategy_config=config,
        model_settings=model_settings,
        as_of=signal_session,
    )
    return {**ranking.context, "_ranking": ranking}


def _metadata_model_status(
    *,
    has_calibration: bool,
    model_rank_context: dict[str, Any],
) -> str:
    calibration = "WITH_CALIBRATION_CONTEXT" if has_calibration else "NO_CALIBRATION_CONTEXT"
    status = model_rank_context.get("status", "DISABLED")
    if status == "SCORED":
        return f"RULES_ONLY_{calibration}_LIGHTGBM_RANK_CONTEXT"
    if status == "DISABLED":
        return f"RULES_ONLY_{calibration}_NO_MODEL_ATTACHED"
    return f"RULES_ONLY_{calibration}_LIGHTGBM_RANK_{status}"


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


def _exclude_context_only_benchmarks(
    signals: list[Any],
    role_by_symbol: dict[str, str],
) -> list[Any]:
    return [
        signal
        for signal in signals
        if role_by_symbol.get(signal.symbol) != "benchmark"
    ]


def _exclude_context_only_tiered_benchmarks(
    candidates: list[TieredCandidateSignal],
    role_by_symbol: dict[str, str],
) -> list[TieredCandidateSignal]:
    return [
        candidate
        for candidate in candidates
        if role_by_symbol.get(candidate.signal.symbol) != "benchmark"
    ]


def _calibration_candidate_rows(
    tiered_candidates: list[TieredCandidateSignal],
    *,
    role_by_symbol: dict[str, str],
    config: BuyTheDipConfig,
    strategy_context: dict[str, Any],
    hierarchy_rows: list[dict[str, Any]] | None = None,
    rotation_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = _candidate_rows(
        [candidate.signal for candidate in tiered_candidates],
        role_by_symbol=role_by_symbol,
        config=config,
    )
    context_tier = str(strategy_context["candidate_tier_context"])
    hierarchy_by_symbol = {
        str(row["symbol"]): row for row in (hierarchy_rows or []) if row.get("symbol")
    }
    rotation_by_group = {
        (str(row["group_type"]), str(row["group"])): row
        for row in (rotation_rows or [])
        if row.get("group_type") and row.get("group")
    }
    for row, tiered in zip(rows, tiered_candidates, strict=True):
        row["calibration_rank"] = row.pop("rank")
        row["calibration_tier"] = tiered.calibration_tier
        row["passed_tiers"] = ";".join(tiered.passed_tiers)
        row["context_tier"] = context_tier
        row["baseline_candidate"] = "BASELINE" in tiered.passed_tiers
        relaxed_quality_pass, relaxed_quality_reasons = _relaxed_quality_gate(
            row=row,
            hierarchy_by_symbol=hierarchy_by_symbol,
            rotation_by_group=rotation_by_group,
            strategy_context=strategy_context,
        )
        defensive_quality_pass, defensive_quality_reasons = _defensive_overlay_quality_gate(
            row=row,
            hierarchy_by_symbol=hierarchy_by_symbol,
            rotation_by_group=rotation_by_group,
        )
        row["relaxed_quality_pass"] = relaxed_quality_pass
        row["relaxed_quality_reasons"] = ";".join(relaxed_quality_reasons)
        row["defensive_overlay_quality_pass"] = defensive_quality_pass
        row["defensive_overlay_quality_reasons"] = ";".join(defensive_quality_reasons)
        row["manual_review_allowed"] = _manual_review_allowed(
            row=row,
            calibration_tier=tiered.calibration_tier,
            context_tier=context_tier,
        )
        row["calibration_action"] = _calibration_action(
            row=row,
            calibration_tier=tiered.calibration_tier,
            context_tier=context_tier,
        )
    return rows


def _relaxed_quality_gate(
    *,
    row: dict[str, Any],
    hierarchy_by_symbol: dict[str, dict[str, Any]],
    rotation_by_group: dict[tuple[str, str], dict[str, Any]],
    strategy_context: dict[str, Any],
) -> tuple[bool, tuple[str, ...]]:
    """Check whether a relaxed-only row is strong enough for manual review."""
    if row["calibration_tier"] != "RELAXED":
        return True, ("not_relaxed_tier",)

    symbol_state = hierarchy_by_symbol.get(str(row["symbol"]), {})
    reasons: list[str] = []
    if (
        symbol_state.get("role") == "leader_stock"
        and symbol_state.get("trend_state") in POSITIVE_SYMBOL_STATES
    ):
        reasons.append(f"leader_stock_{symbol_state['trend_state']}")

    theme = symbol_state.get("theme")
    theme_rotation = rotation_by_group.get(("theme", str(theme))) if theme else None
    if _rotation_row_is_positive(theme_rotation):
        reasons.append(f"theme_{theme_rotation['rotation_status']}")

    sector = symbol_state.get("sector")
    sector_rotation = rotation_by_group.get(("sector", str(sector))) if sector else None
    if _rotation_row_is_positive(sector_rotation):
        reasons.append(f"sector_{sector_rotation['rotation_status']}")

    if (
        row["universe_role"] == "ai_alpha"
        and _is_positive(strategy_context.get("ai_vs_hedge_spread_20d"))
    ):
        reasons.append("ai_vs_hedge_spread_20d_positive")

    leader_reversal = any(reason.startswith("leader_stock_") for reason in reasons)
    return leader_reversal, tuple(reasons)


def _defensive_overlay_quality_gate(
    *,
    row: dict[str, Any],
    hierarchy_by_symbol: dict[str, dict[str, Any]],
    rotation_by_group: dict[tuple[str, str], dict[str, Any]],
) -> tuple[bool, tuple[str, ...]]:
    """Check whether a hedge overlay row is high-quality enough for manual review."""
    if row["universe_role"] != "hedge_overlay":
        return True, ("not_hedge_overlay",)

    symbol_state = hierarchy_by_symbol.get(str(row["symbol"]), {})
    theme = str(symbol_state.get("theme") or "")
    reasons: list[str] = []
    if theme not in DEFENSIVE_OVERLAY_REVIEW_THEMES:
        return False, (f"theme_not_reviewable:{theme or 'unknown'}",)
    reasons.append(f"theme_reviewable:{theme}")

    trend_state = symbol_state.get("trend_state")
    if symbol_state.get("role") != "leader_stock" or trend_state not in POSITIVE_SYMBOL_STATES:
        return False, (*reasons, f"symbol_not_leader_reversal:{trend_state or 'unknown'}")
    reasons.append(f"leader_stock_{trend_state}")

    theme_rotation = rotation_by_group.get(("theme", theme))
    if not _rotation_row_is_positive(theme_rotation):
        return False, (*reasons, "theme_rotation_not_positive")
    reasons.append(f"theme_{theme_rotation['rotation_status']}")

    return True, tuple(reasons)


def _rotation_row_is_positive(row: dict[str, Any] | None) -> bool:
    if row is None:
        return False
    return row.get("rotation_status") in POSITIVE_ROTATION_STATES or _is_positive(
        row.get("relative_return_20d")
    )


def _manual_review_allowed(
    *,
    row: dict[str, Any],
    calibration_tier: str,
    context_tier: str,
) -> bool:
    if row["news_risk"] == "MEDIUM":
        return False
    if row["universe_role"] == "hedge_overlay":
        if context_tier in {"DEFENSIVE", "RELAXED_WATCHLIST"}:
            return bool(row.get("defensive_overlay_quality_pass"))
        return False
    if context_tier == "DEFENSIVE":
        return False
    if context_tier == "STRICT":
        return calibration_tier == "STRICT"
    if context_tier == "RELAXED_WATCHLIST":
        if calibration_tier == "RELAXED":
            return bool(row.get("relaxed_quality_pass"))
        return calibration_tier in {"STRICT", "BASELINE", "RELAXED"}
    return calibration_tier in {"STRICT", "BASELINE"}


def _calibration_action(
    *,
    row: dict[str, Any],
    calibration_tier: str,
    context_tier: str,
) -> str:
    if row["news_risk"] == "MEDIUM":
        return "DEFER_FOR_MANUAL_NEWS_REVIEW"
    if row["universe_role"] == "hedge_overlay":
        if not row.get("defensive_overlay_quality_pass"):
            return "WATCH_ONLY_DEFENSIVE_OVERLAY_QUALITY_GATE"
        if context_tier in {"DEFENSIVE", "RELAXED_WATCHLIST"}:
            return "REVIEW_AS_DEFENSIVE_OVERLAY"
        return "WATCH_ONLY_DEFENSIVE_OVERLAY_CONTEXT"
    if context_tier == "DEFENSIVE":
        return "DEFER_AI_ALPHA_DEFENSIVE_CONTEXT"
    if context_tier == "STRICT" and calibration_tier != "STRICT":
        return "WATCH_ONLY_STRICT_CONTEXT"
    if context_tier == "RELAXED_WATCHLIST" and calibration_tier == "RELAXED":
        if not row.get("relaxed_quality_pass"):
            return "WATCH_ONLY_RELAXED_QUALITY_GATE"
        return "RELAXED_WATCHLIST_REVIEW_ONLY"
    if calibration_tier == "RELAXED":
        return "WATCH_ONLY_RELAXED_CALIBRATION"
    return row["recommended_action"]


def _calibration_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "calibration_candidate_count": len(rows),
        "strict_candidate_count": sum(row["calibration_tier"] == "STRICT" for row in rows),
        "baseline_candidate_count": sum(row["baseline_candidate"] for row in rows),
        "relaxed_only_candidate_count": sum(
            row["calibration_tier"] == "RELAXED" for row in rows
        ),
        "manual_review_allowed_count": sum(row["manual_review_allowed"] for row in rows),
        "defensive_defer_count": sum(
            row["calibration_action"] == "DEFER_AI_ALPHA_DEFENSIVE_CONTEXT"
            for row in rows
        ),
    }


def _recommended_action(universe_role: str, news_risk: str) -> str:
    if news_risk == "MEDIUM":
        return "DEFER_FOR_MANUAL_NEWS_REVIEW"
    if universe_role == "hedge_overlay":
        return "REVIEW_AS_DEFENSIVE_OVERLAY"
    return "PREPARE_MANUAL_CONDITIONAL_ORDER"


def _is_positive(value: object) -> bool:
    return value is not None and float(value) > 0


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
