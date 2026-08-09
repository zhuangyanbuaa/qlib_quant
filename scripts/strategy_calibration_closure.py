#!/usr/bin/env python
"""Run the strategy-calibration closure study.

This script is intentionally outside the production CLI. It writes research
artifacts only and does not mutate strategy configuration, raw data, SQLite
state, or model registries.
"""

from __future__ import annotations

# ruff: noqa: E402
import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from quant_system.backtest.workflow import load_feature_history
from quant_system.decision.hierarchy import (
    calibrate_strategy_context,
    compute_symbol_states,
    load_hierarchy_proxies,
)
from quant_system.decision.premarket import (
    _calibration_candidate_rows,
    load_decision_symbol_benchmarks,
    load_decision_universe,
)
from quant_system.decision.rotation import (
    DEFAULT_ROTATION_PERIODS,
    compute_pair_spreads,
    compute_rotation_rows,
    load_rotation_members,
    rotation_posture,
)
from quant_system.settings import get_settings
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.strategy.buy_the_dip import BuyTheDipStrategy
from quant_system.strategy.calibration import (
    TIER_ORDER,
    TieredCandidateSignal,
    generate_tiered_candidates,
    rules_for_tier,
)
from quant_system.strategy.config import RulesConfig, load_buy_the_dip_config

REGIME_SLICES = (
    ("2026_ytd", date(2026, 1, 1), None),
    ("feb_mar_drift_down", date(2026, 2, 1), date(2026, 3, 31)),
    ("late_mar_reversal", date(2026, 3, 24), date(2026, 4, 10)),
    ("jun_jul_drift_down", date(2026, 6, 1), date(2026, 7, 23)),
    ("late_jul_reversal", date(2026, 7, 24), date(2026, 8, 7)),
)

MANUAL_REVIEW_ACTIONS = {
    "PREPARE_MANUAL_CONDITIONAL_ORDER",
    "RELAXED_WATCHLIST_REVIEW_ONLY",
    "REVIEW_AS_DEFENSIVE_OVERLAY",
}


@dataclass(frozen=True)
class ClosureInputs:
    data_root: Path
    database_path: Path
    report_root: Path
    ai_universe_path: Path
    hedge_universe_path: Path
    benchmark_path: Path
    strategy_config_path: Path
    start: date
    end: date | None
    hold_sessions: int


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    inputs = ClosureInputs(
        data_root=settings.resolved_data_dir,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        report_root=settings.resolved_data_dir / "reports" / "simulations" / "calibration_closure",
        ai_universe_path=args.ai_universe,
        hedge_universe_path=args.hedge_universe,
        benchmark_path=args.benchmarks,
        strategy_config_path=args.strategy_config,
        start=args.start,
        end=args.end,
        hold_sessions=args.hold_sessions,
    )
    summary = run_closure(inputs)
    print(json.dumps(summary, indent=2, sort_keys=True))


def run_closure(inputs: ClosureInputs) -> dict[str, Any]:
    run_id = uuid4()
    generated_at = datetime.now(UTC)
    output_dir = inputs.report_root / generated_at.strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=True)

    repository = ParquetRepository(inputs.data_root)
    config = load_buy_the_dip_config(inputs.strategy_config_path)
    universe_paths = (inputs.ai_universe_path, inputs.hedge_universe_path)
    symbols, role_by_symbol = load_decision_universe(universe_paths)
    benchmark_etf_by_symbol = load_decision_symbol_benchmarks(universe_paths)
    benchmark_symbol = config.strategy.benchmark_symbol.upper()

    sessions = _load_sessions(
        database_path=inputs.database_path,
        repository=repository,
        benchmark_symbol=benchmark_symbol,
        start=inputs.start,
        end=inputs.end,
    )
    if not sessions:
        raise ValueError("no benchmark sessions found for calibration closure")

    end = sessions[-1]
    features = load_feature_history(
        repository=repository,
        database_path=inputs.database_path,
        symbols=symbols,
        benchmark_symbol=benchmark_symbol,
        start=sessions[0],
        end=end,
    )
    close_lookup = _close_lookup(features)
    hierarchy_price_history = _load_hierarchy_prices(
        repository=repository,
        database_path=inputs.database_path,
        universe_paths=universe_paths,
        benchmark_path=inputs.benchmark_path,
        benchmark_symbol=benchmark_symbol,
        start=sessions[0],
        end=end,
    )

    records: list[dict[str, Any]] = []
    session_contexts: list[dict[str, Any]] = []
    variants = ("current", "relaxed_tighter", "relaxed_looser")
    for session in sessions:
        context_payload = _context_for_session(
            prices=hierarchy_price_history,
            universe_paths=universe_paths,
            benchmark_path=inputs.benchmark_path,
            benchmark_symbol=benchmark_symbol,
            as_of=session,
        )
        session_contexts.append(
            {
                "session": session.isoformat(),
                "candidate_tier_context": context_payload["strategy_context"][
                    "candidate_tier_context"
                ],
                "risk_multiplier_hint": context_payload["strategy_context"]["risk_multiplier_hint"],
                "market_state": context_payload["strategy_context"]["market_state"],
                "ai_leader_breadth": context_payload["strategy_context"]["ai_leader_breadth"],
                "ai_vs_hedge_spread_20d": context_payload["strategy_context"].get(
                    "ai_vs_hedge_spread_20d"
                ),
                "rotation_status": context_payload["rotation_summary"].get("status"),
                "reversal_status": context_payload["strategy_context"]
                .get("reversal_context", {})
                .get("status"),
                "reversal_action_hint": context_payload["strategy_context"]
                .get("reversal_context", {})
                .get("action_hint"),
                "ai_leader_repair_fraction": context_payload["strategy_context"]
                .get("reversal_context", {})
                .get("ai_leader_repair_fraction"),
                "ai_leader_drift_fraction": context_payload["strategy_context"]
                .get("reversal_context", {})
                .get("ai_leader_drift_fraction"),
            }
        )
        for variant in variants:
            tiered = _generate_variant_tiered_candidates(
                features=features,
                as_of=session,
                base_config=config.strategy,
                variant=variant,
            )
            tiered = [
                candidate
                for candidate in tiered
                if role_by_symbol.get(candidate.signal.symbol) != "benchmark"
            ]
            rows = _calibration_candidate_rows(
                tiered,
                role_by_symbol=role_by_symbol,
                benchmark_etf_by_symbol=benchmark_etf_by_symbol,
                config=config,
                strategy_context=context_payload["strategy_context"],
                hierarchy_rows=context_payload["hierarchy_rows"],
                rotation_rows=context_payload["rotation_rows"],
            )
            records.extend(
                _candidate_records(
                    rows=rows,
                    session=session,
                    variant=variant,
                    close_lookup=close_lookup,
                    sessions=sessions,
                    hold_sessions=inputs.hold_sessions,
                    benchmark_symbol=benchmark_symbol,
                )
            )

    candidate_frame = pd.DataFrame.from_records(records)
    context_frame = pd.DataFrame.from_records(session_contexts)
    candidate_csv = output_dir / "candidate_records.csv"
    context_csv = output_dir / "session_contexts.csv"
    candidate_frame.to_csv(candidate_csv, index=False)
    context_frame.to_csv(context_csv, index=False)

    summary = _summary_payload(
        candidate_frame=candidate_frame,
        context_frame=context_frame,
        sessions=sessions,
        run_id=run_id,
        generated_at=generated_at,
        output_dir=output_dir,
        candidate_csv=candidate_csv,
        context_csv=context_csv,
        hold_sessions=inputs.hold_sessions,
    )
    summary_json = output_dir / "summary.json"
    summary_md = output_dir / "summary.md"
    summary_json.write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    summary_md.write_text(_markdown_summary(summary), encoding="utf-8")
    summary["artifacts"] = {
        "directory": str(output_dir),
        "summary_json": str(summary_json),
        "summary_markdown": str(summary_md),
        "candidate_csv": str(candidate_csv),
        "context_csv": str(context_csv),
    }
    summary_json.write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    summary_md.write_text(_markdown_summary(summary), encoding="utf-8")
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--hold-sessions", type=int, default=5)
    parser.add_argument(
        "--ai-universe",
        type=Path,
        default=PROJECT_ROOT / "configs" / "universe" / "ai_watchlist.yaml",
    )
    parser.add_argument(
        "--hedge-universe",
        type=Path,
        default=PROJECT_ROOT / "configs" / "universe" / "hedge_overlay.yaml",
    )
    parser.add_argument(
        "--benchmarks",
        type=Path,
        default=PROJECT_ROOT / "configs" / "universe" / "benchmarks.yaml",
    )
    parser.add_argument(
        "--strategy-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "strategy" / "buy_the_dip.yaml",
    )
    return parser.parse_args()


def _load_sessions(
    *,
    database_path: Path,
    repository: ParquetRepository,
    benchmark_symbol: str,
    start: date,
    end: date | None,
) -> list[date]:
    with DuckDBAnalytics(database_path, repository.daily_prices_root) as analytics:
        analytics.refresh_views()
        prices = analytics.query_price_history(
            (benchmark_symbol,),
            start_date=start,
            end_date=end or date.today(),
        )
    if prices.empty:
        return []
    frame = prices.copy()
    frame["session_date_ny"] = pd.to_datetime(frame["session_date_ny"]).dt.date
    return sorted(frame["session_date_ny"].unique())


def _load_hierarchy_prices(
    *,
    repository: ParquetRepository,
    database_path: Path,
    universe_paths: tuple[Path, ...],
    benchmark_path: Path,
    benchmark_symbol: str,
    start: date,
    end: date,
) -> pd.DataFrame:
    proxies = load_hierarchy_proxies(
        universe_paths=universe_paths,
        benchmark_path=benchmark_path,
    )
    rotation_members = load_rotation_members(universe_paths)
    requested_symbols = tuple(
        sorted(
            {
                benchmark_symbol,
                *(proxy.symbol for proxy in proxies),
                *(member.benchmark_etf for member in rotation_members),
            }
        )
    )
    with DuckDBAnalytics(database_path, repository.daily_prices_root) as analytics:
        analytics.refresh_views()
        return analytics.query_price_history(
            requested_symbols,
            start_date=start - pd.Timedelta(days=450),
            end_date=end,
        )


def _context_for_session(
    *,
    prices: pd.DataFrame,
    universe_paths: tuple[Path, ...],
    benchmark_path: Path,
    benchmark_symbol: str,
    as_of: date,
) -> dict[str, Any]:
    proxies = load_hierarchy_proxies(
        universe_paths=universe_paths,
        benchmark_path=benchmark_path,
    )
    rotation_members = load_rotation_members(universe_paths)
    state_by_symbol = compute_symbol_states(
        prices=prices,
        proxies=proxies,
        as_of=as_of,
        benchmark_symbol=benchmark_symbol,
    )
    rotation_rows = compute_rotation_rows(
        prices=prices,
        members=rotation_members,
        as_of=as_of,
        periods=DEFAULT_ROTATION_PERIODS,
        default_benchmark_symbol=benchmark_symbol,
    )
    pair_spreads = compute_pair_spreads(rotation_rows)
    rotation_summary = rotation_posture(rotation_rows, pair_spreads)
    strategy_context = calibrate_strategy_context(
        state_by_symbol=state_by_symbol,
        rotation_summary=rotation_summary,
        pair_spreads=pair_spreads,
    )
    return {
        "strategy_context": strategy_context,
        "hierarchy_rows": list(state_by_symbol.values()),
        "rotation_rows": rotation_rows,
        "rotation_summary": rotation_summary,
    }


def _generate_variant_tiered_candidates(
    *,
    features: pd.DataFrame,
    as_of: date,
    base_config: RulesConfig,
    variant: str,
) -> list[TieredCandidateSignal]:
    if variant == "current":
        return generate_tiered_candidates(
            features=features,
            as_of=as_of,
            base_config=base_config,
            news_risk=None,
        )
    signals_by_symbol: dict[str, dict[str, Any]] = {}
    for tier in TIER_ORDER:
        tier_config = _rules_for_variant(base_config, tier=tier, variant=variant)
        signals = BuyTheDipStrategy(tier_config).generate_signals(features, as_of=as_of)
        for signal in signals:
            signals_by_symbol.setdefault(signal.symbol, {})[tier] = signal
    tiered: list[TieredCandidateSignal] = []
    for tier_signals in signals_by_symbol.values():
        passed_tiers = tuple(tier for tier in TIER_ORDER if tier in tier_signals)
        calibration_tier = passed_tiers[0]
        tiered.append(
            TieredCandidateSignal(
                signal=tier_signals[calibration_tier],
                calibration_tier=calibration_tier,
                passed_tiers=passed_tiers,
            )
        )
    return sorted(
        tiered,
        key=lambda candidate: (
            TIER_ORDER.index(candidate.calibration_tier),
            -candidate.signal.score,
            candidate.signal.symbol,
        ),
    )


def _rules_for_variant(base_config: RulesConfig, *, tier: str, variant: str) -> RulesConfig:
    if tier != "RELAXED":
        return rules_for_tier(base_config, tier)
    relaxed = rules_for_tier(base_config, "RELAXED")
    if variant == "relaxed_tighter":
        return relaxed.model_copy(
            update={
                "minimum_relative_return_60": base_config.minimum_relative_return_60 - 0.025,
                "minimum_drawdown": 0.04,
                "maximum_drawdown": 0.18,
                "minimum_rsi": max(0.0, base_config.minimum_rsi - 3),
                "maximum_rsi": min(100.0, base_config.maximum_rsi + 5),
                "minimum_atr_drawdown": 0.75,
                "maximum_atr_drawdown": 3.50,
            }
        )
    if variant == "relaxed_looser":
        return relaxed.model_copy(
            update={
                "minimum_relative_return_60": base_config.minimum_relative_return_60 - 0.08,
                "minimum_drawdown": 0.025,
                "maximum_drawdown": 0.25,
                "minimum_rsi": max(0.0, base_config.minimum_rsi - 8),
                "maximum_rsi": min(100.0, base_config.maximum_rsi + 15),
                "minimum_atr_drawdown": 0.40,
                "maximum_atr_drawdown": 4.50,
            }
        )
    raise ValueError(f"unknown variant: {variant}")


def _close_lookup(features: pd.DataFrame) -> dict[tuple[str, date], float]:
    frame = features[["symbol", "session_date_ny", "close"]].copy()
    frame["session_date_ny"] = pd.to_datetime(frame["session_date_ny"]).dt.date
    return {
        (str(row.symbol), row.session_date_ny): float(row.close)
        for row in frame.itertuples(index=False)
    }


def _candidate_records(
    *,
    rows: list[dict[str, Any]],
    session: date,
    variant: str,
    close_lookup: dict[tuple[str, date], float],
    sessions: list[date],
    hold_sessions: int,
    benchmark_symbol: str,
) -> list[dict[str, Any]]:
    session_index = {value: index for index, value in enumerate(sessions)}
    index = session_index[session]
    target_session = (
        sessions[index + hold_sessions] if index + hold_sessions < len(sessions) else None
    )
    records = []
    for row in rows:
        symbol = str(row["symbol"])
        signal_close = close_lookup.get((symbol, session))
        target_close = (
            close_lookup.get((symbol, target_session)) if target_session is not None else None
        )
        benchmark_close = close_lookup.get((benchmark_symbol, session))
        benchmark_target_close = (
            close_lookup.get((benchmark_symbol, target_session))
            if target_session is not None
            else None
        )
        forward_return = _safe_return(signal_close, target_close)
        benchmark_return = _safe_return(benchmark_close, benchmark_target_close)
        records.append(
            {
                "session": session.isoformat(),
                "variant": variant,
                "symbol": symbol,
                "universe_role": row["universe_role"],
                "calibration_tier": row["calibration_tier"],
                "context_tier": row["context_tier"],
                "calibration_action": row["calibration_action"],
                "manual_review_allowed": bool(row["manual_review_allowed"]),
                "baseline_candidate": bool(row["baseline_candidate"]),
                "relaxed_quality_pass": bool(row["relaxed_quality_pass"]),
                "defensive_overlay_quality_pass": bool(row["defensive_overlay_quality_pass"]),
                "benchmark_etf": row.get("benchmark_etf"),
                "sector_confirmation_pass": bool(
                    row.get("sector_confirmation_pass", True)
                ),
                "sector_confirmation_reasons": row.get("sector_confirmation_reasons"),
                "reversal_phase": row.get("reversal_phase"),
                "reversal_score": row.get("reversal_score"),
                "reversal_reasons": row.get("reversal_reasons"),
                "score": float(row["score"]),
                "signal_close": signal_close,
                "target_session": target_session.isoformat()
                if target_session is not None
                else None,
                f"forward_return_{hold_sessions}d": forward_return,
                f"benchmark_return_{hold_sessions}d": benchmark_return,
                f"relative_return_{hold_sessions}d": (
                    None
                    if forward_return is None or benchmark_return is None
                    else forward_return - benchmark_return
                ),
                "matured": forward_return is not None,
            }
        )
    return records


def _safe_return(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or start == 0:
        return None
    return end / start - 1


def _summary_payload(
    *,
    candidate_frame: pd.DataFrame,
    context_frame: pd.DataFrame,
    sessions: list[date],
    run_id: Any,
    generated_at: datetime,
    output_dir: Path,
    candidate_csv: Path,
    context_csv: Path,
    hold_sessions: int,
) -> dict[str, Any]:
    return_col = f"forward_return_{hold_sessions}d"
    relative_col = f"relative_return_{hold_sessions}d"
    current = candidate_frame.loc[candidate_frame["variant"] == "current"].copy()
    payload: dict[str, Any] = {
        "metadata": {
            "run_id": str(run_id),
            "generated_at_utc": generated_at.isoformat(),
            "decision_scope": "READ_ONLY_STRATEGY_CALIBRATION_CLOSURE",
            "universe_history_policy": "CURRENT_SNAPSHOT_FORWARD_ONLY",
            "return_method": (
                f"candidate quality from signal-session close to +{hold_sessions} "
                "benchmark sessions close; not a fill simulation"
            ),
            "session_start": sessions[0].isoformat(),
            "session_end": sessions[-1].isoformat(),
            "session_count": len(sessions),
            "hold_sessions": hold_sessions,
            "output_dir": str(output_dir),
        },
        "ytd": _summarize_slice(current, return_col=return_col, relative_col=relative_col),
        "regime_slices": {},
        "parameter_sensitivity": {},
        "context_distribution": _context_distribution(context_frame),
        "artifacts": {
            "candidate_csv": str(candidate_csv),
            "context_csv": str(context_csv),
        },
    }
    for name, start, end in REGIME_SLICES:
        effective_end = end or sessions[-1]
        subset = current.loc[
            current["session"].between(start.isoformat(), effective_end.isoformat())
        ]
        payload["regime_slices"][name] = _summarize_slice(
            subset,
            return_col=return_col,
            relative_col=relative_col,
        )
    for variant, subset in candidate_frame.groupby("variant", sort=True):
        payload["parameter_sensitivity"][variant] = _summarize_slice(
            subset,
            return_col=return_col,
            relative_col=relative_col,
        )
    payload["recommendation"] = _recommendation(payload)
    return payload


def _summarize_slice(
    frame: pd.DataFrame,
    *,
    return_col: str,
    relative_col: str,
) -> dict[str, Any]:
    if frame.empty:
        return _empty_summary()
    matured = frame.loc[frame["matured"]].copy()
    manual = matured.loc[matured["manual_review_allowed"]].copy()
    all_manual = frame.loc[frame["manual_review_allowed"]].copy()
    return {
        "candidate_rows": len(frame),
        "manual_review_rows": len(all_manual),
        "matured_rows": len(matured),
        "matured_manual_review_rows": len(manual),
        "sessions_with_candidates": int(frame["session"].nunique()),
        "sessions_with_manual_review": int(all_manual["session"].nunique()),
        "action_counts": dict(Counter(frame["calibration_action"])),
        "context_counts": dict(Counter(frame["context_tier"])),
        "tier_counts": dict(Counter(frame["calibration_tier"])),
        "reversal_phase_counts": _counter_if_present(frame, "reversal_phase"),
        "sector_confirmation": _sector_confirmation_summary(frame),
        "manual_review_stats": _return_stats(manual, return_col, relative_col),
        "all_candidate_stats": _return_stats(matured, return_col, relative_col),
    }


def _empty_summary() -> dict[str, Any]:
    return {
        "candidate_rows": 0,
        "manual_review_rows": 0,
        "matured_rows": 0,
        "matured_manual_review_rows": 0,
        "sessions_with_candidates": 0,
        "sessions_with_manual_review": 0,
        "action_counts": {},
        "context_counts": {},
        "tier_counts": {},
        "reversal_phase_counts": {},
        "sector_confirmation": {
            "ai_rows": 0,
            "ai_pass_count": 0,
            "ai_block_count": 0,
            "ai_block_by_benchmark": {},
            "ai_block_by_context": {},
        },
        "manual_review_stats": _empty_stats(),
        "all_candidate_stats": _empty_stats(),
    }


def _counter_if_present(frame: pd.DataFrame, column: str) -> dict[str, int]:
    if column not in frame.columns:
        return {}
    return dict(Counter(frame[column].dropna()))


def _sector_confirmation_summary(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty or "sector_confirmation_pass" not in frame.columns:
        return {
            "ai_rows": 0,
            "ai_pass_count": 0,
            "ai_block_count": 0,
            "ai_block_by_benchmark": {},
            "ai_block_by_context": {},
        }
    ai = frame.loc[frame["universe_role"] == "ai_alpha"].copy()
    if ai.empty:
        return {
            "ai_rows": 0,
            "ai_pass_count": 0,
            "ai_block_count": 0,
            "ai_block_by_benchmark": {},
            "ai_block_by_context": {},
        }
    blocked = ai.loc[~ai["sector_confirmation_pass"].astype(bool)]
    return {
        "ai_rows": len(ai),
        "ai_pass_count": int(ai["sector_confirmation_pass"].astype(bool).sum()),
        "ai_block_count": len(blocked),
        "ai_block_by_benchmark": dict(Counter(blocked["benchmark_etf"])),
        "ai_block_by_context": dict(Counter(blocked["context_tier"])),
    }


def _return_stats(frame: pd.DataFrame, return_col: str, relative_col: str) -> dict[str, Any]:
    if frame.empty:
        return _empty_stats()
    returns = frame[return_col].dropna().astype(float)
    relatives = frame[relative_col].dropna().astype(float)
    if returns.empty:
        return _empty_stats()
    return {
        "count": len(returns),
        "avg_return": _round(float(returns.mean())),
        "median_return": _round(float(returns.median())),
        "win_rate": _round(float((returns > 0).mean())),
        "avg_relative_return": _round(float(relatives.mean())) if not relatives.empty else None,
        "median_relative_return": _round(float(relatives.median()))
        if not relatives.empty
        else None,
        "relative_win_rate": _round(float((relatives > 0).mean())) if not relatives.empty else None,
        "worst_return": _round(float(returns.min())),
        "best_return": _round(float(returns.max())),
    }


def _empty_stats() -> dict[str, Any]:
    return {
        "count": 0,
        "avg_return": None,
        "median_return": None,
        "win_rate": None,
        "avg_relative_return": None,
        "median_relative_return": None,
        "relative_win_rate": None,
        "worst_return": None,
        "best_return": None,
    }


def _context_distribution(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {}
    payload = {
        "candidate_tier_context": dict(Counter(frame["candidate_tier_context"])),
        "rotation_status": dict(Counter(frame["rotation_status"])),
    }
    if "reversal_status" in frame.columns:
        payload["reversal_status"] = dict(Counter(frame["reversal_status"]))
    if "reversal_action_hint" in frame.columns:
        payload["reversal_action_hint"] = dict(Counter(frame["reversal_action_hint"]))
    return payload


def _recommendation(payload: dict[str, Any]) -> dict[str, Any]:
    ytd_stats = payload["ytd"]["manual_review_stats"]
    manual_count = ytd_stats["count"]
    avg_return = ytd_stats["avg_return"]
    relative = ytd_stats["avg_relative_return"]
    sensitivity = payload["parameter_sensitivity"]
    current_count = (
        sensitivity.get("current", {})
        .get("manual_review_stats", {})
        .get(
            "count",
            0,
        )
    )
    looser_count = (
        sensitivity.get("relaxed_looser", {})
        .get(
            "manual_review_stats",
            {},
        )
        .get("count", 0)
    )
    looser_explodes = bool(current_count and looser_count > current_count * 1.8)

    if manual_count < 30:
        status = "CONTINUE_FORWARD_OBSERVATION"
        reason = "Manual-review sample is still below the 30-trade minimum."
    elif avg_return is not None and avg_return > 0 and relative is not None and relative >= 0:
        status = "MERGE_AS_CONSERVATIVE_CALIBRATION"
        reason = "Manual-review rows have positive absolute and relative expectancy."
    else:
        status = "MERGE_AS_GATING_AND_OBSERVATION_ONLY"
        reason = (
            "The gates control candidate quality, but forward expectancy is not strong "
            "enough to justify automatic risk expansion."
        )

    cautions = [
        "Current watchlist is forward-only and survivorship-biased for pre-selection history.",
        "Return metric is close-to-close candidate quality, not executable fill P&L.",
        "Do not optimize thresholds to the March/July reversal windows.",
    ]
    if looser_explodes:
        cautions.append(
            "Looser relaxed thresholds expand the manual-review set sharply; keep leader gate."
        )
    return {"status": status, "reason": reason, "cautions": cautions}


def _markdown_summary(payload: dict[str, Any]) -> str:
    lines = [
        "# Strategy calibration closure",
        "",
        "## Scope",
        "",
        f"- Run ID: `{payload['metadata']['run_id']}`",
        f"- Sessions: `{payload['metadata']['session_start']}` to "
        f"`{payload['metadata']['session_end']}` "
        f"({payload['metadata']['session_count']})",
        f"- Universe policy: `{payload['metadata']['universe_history_policy']}`",
        f"- Return method: {payload['metadata']['return_method']}",
        "",
        "## Recommendation",
        "",
        f"- Status: `{payload['recommendation']['status']}`",
        f"- Reason: {payload['recommendation']['reason']}",
        "",
        "Cautions:",
        "",
        *[f"- {item}" for item in payload["recommendation"]["cautions"]],
        "",
        "## 2026 YTD current calibration",
        "",
        _summary_table({"2026_ytd": payload["ytd"]}),
        "",
        "## Regime slices",
        "",
        _summary_table(payload["regime_slices"]),
        "",
        "## Parameter sensitivity",
        "",
        _summary_table(payload["parameter_sensitivity"]),
        "",
        "## Context distribution",
        "",
        "```json",
        json.dumps(payload["context_distribution"], indent=2, sort_keys=True),
        "```",
    ]
    return "\n".join(lines) + "\n"


def _summary_table(items: dict[str, Any]) -> str:
    headers = [
        "slice",
        "rows",
        "manual",
        "matured manual",
        "avg 5d",
        "median 5d",
        "win",
        "avg rel",
        "rel win",
    ]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for name, item in items.items():
        stats = item["manual_review_stats"]
        lines.append(
            "| "
            + " | ".join(
                [
                    str(name),
                    str(item["candidate_rows"]),
                    str(item["manual_review_rows"]),
                    str(item["matured_manual_review_rows"]),
                    _percent(stats["avg_return"]),
                    _percent(stats["median_return"]),
                    _percent(stats["win_rate"]),
                    _percent(stats["avg_relative_return"]),
                    _percent(stats["relative_win_rate"]),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.2f}%"


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 6)


if __name__ == "__main__":
    main()
