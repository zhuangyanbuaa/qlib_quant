"""Three-level market, sector, and stock diagnostics for strategy calibration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pandas as pd

from quant_system.decision.reports import DailyReportArtifacts, write_decision_table_report
from quant_system.decision.rotation import (
    DEFAULT_ROTATION_PERIODS,
    compute_pair_spreads,
    compute_rotation_rows,
    load_rotation_members,
    rotation_posture,
)
from quant_system.domain.clocks import NyseSessionClock
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.universe.config import load_benchmark_config, load_watchlist_config

MARKET_PROXY_SYMBOLS = ("QQQ", "SPY")
LEADER_LIQUIDITY_TIERS = {"mega", "high"}
REPAIR_STATES = {"REVERSAL_ATTEMPT", "CONFIRMED_REVERSAL", "UPTREND"}
DOWN_STATES = {"DRIFT_DOWN", "WASHOUT", "LAGGING"}


@dataclass(frozen=True)
class HierarchyProxy:
    """A symbol's role in the market/sector/stock diagnostic hierarchy."""

    symbol: str
    layer: str
    role: str
    proxy_for: str
    tradable: bool
    universe_role: str | None = None
    sector: str | None = None
    theme: str | None = None
    subtheme: str | None = None


def load_hierarchy_proxies(
    *,
    universe_paths: tuple[Path, ...],
    benchmark_path: Path,
) -> tuple[HierarchyProxy, ...]:
    """Load market, ETF, leader, and stock proxies from existing configs."""
    proxies: dict[tuple[str, str, str], HierarchyProxy] = {}
    benchmarks = load_benchmark_config(benchmark_path)
    for benchmark in benchmarks.benchmarks:
        layer = "market" if benchmark.symbol in MARKET_PROXY_SYMBOLS else "sector"
        proxies[(layer, benchmark.symbol, benchmark.role)] = HierarchyProxy(
            symbol=benchmark.symbol,
            layer=layer,
            role=f"{layer}_proxy",
            proxy_for=benchmark.role,
            tradable=False,
        )

    for path in universe_paths:
        config = load_watchlist_config(path)
        universe_role = _universe_role(config.universe_type)
        for member in config.symbols:
            role = (
                "leader_stock"
                if member.liquidity_tier in LEADER_LIQUIDITY_TIERS
                else "stock"
            )
            proxies[("stock", member.symbol, universe_role)] = HierarchyProxy(
                symbol=member.symbol,
                layer="stock",
                role=role,
                proxy_for=member.theme,
                tradable=True,
                universe_role=universe_role,
                sector=member.sector,
                theme=member.theme,
                subtheme=member.subtheme,
            )
    return tuple(proxies[key] for key in sorted(proxies))


def _universe_role(universe_type: str) -> str:
    if universe_type == "HEDGE_OVERLAY":
        return "hedge_overlay"
    if universe_type.endswith("_SATELLITE") or universe_type == "RAW_REVIEW":
        return "ai_satellite"
    return "ai_alpha"


def run_hierarchy_diagnostics_workflow(
    *,
    repository: ParquetRepository,
    database_path: Path,
    report_root: Path,
    universe_paths: tuple[Path, ...],
    benchmark_path: Path,
    as_of: date,
    benchmark_symbol: str = "QQQ",
    run_id: UUID | None = None,
    generated_at_utc: datetime | None = None,
) -> tuple[dict[str, Any], DailyReportArtifacts]:
    """Generate three-level diagnostics and strategy-calibration context."""
    run_id = run_id or uuid4()
    generated_at_utc = _ensure_utc(generated_at_utc or datetime.now(UTC))
    benchmark_symbol = benchmark_symbol.upper()
    proxies = load_hierarchy_proxies(
        universe_paths=universe_paths,
        benchmark_path=benchmark_path,
    )
    rotation_members = load_rotation_members(universe_paths)
    requested_symbols = tuple(
        sorted({benchmark_symbol, *(proxy.symbol for proxy in proxies)})
    )

    with DuckDBAnalytics(database_path, repository.daily_prices_root) as analytics:
        analytics.refresh_views()
        prices = analytics.query_price_history(
            requested_symbols,
            start_date=as_of - timedelta(days=320),
            end_date=as_of,
        )

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
    context = calibrate_strategy_context(
        state_by_symbol=state_by_symbol,
        rotation_summary=rotation_summary,
        pair_spreads=pair_spreads,
    )
    rows = _report_rows(state_by_symbol)
    report: dict[str, Any] = {
        "metadata": {
            "run_id": str(run_id),
            "generated_at_utc": generated_at_utc.isoformat(),
            "as_of": as_of.isoformat(),
            "data_cutoff_utc": NyseSessionClock().session_close_utc(as_of).isoformat(),
            "benchmark_symbol": benchmark_symbol,
            "universe_history_policy": "CURRENT_SNAPSHOT_FORWARD_ONLY_FOR_DIAGNOSTICS",
            "decision_scope": "READ_ONLY_STRATEGY_CALIBRATION_CONTEXT",
        },
        "counts": _counts(rows),
        "strategy_context": context,
        "rotation_posture": rotation_summary,
        "pair_spreads": pair_spreads,
        "rotation_rows": rotation_rows,
        "rows": rows,
    }
    artifacts = write_decision_table_report(
        report=report,
        rows=rows,
        report_root=report_root,
        as_of=as_of,
        run_id=run_id,
        stem="hierarchy",
        title="Three-Level Strategy Diagnostics",
    )
    report["artifacts"] = {
        "directory": str(artifacts.directory),
        "json": str(artifacts.json_path),
        "csv": str(artifacts.csv_path),
        "markdown": str(artifacts.markdown_path),
        "html": str(artifacts.html_path),
    }
    artifacts = write_decision_table_report(
        report=report,
        rows=rows,
        report_root=report_root,
        as_of=as_of,
        run_id=run_id,
        stem="hierarchy",
        title="Three-Level Strategy Diagnostics",
    )
    return report, artifacts


def compute_symbol_states(
    *,
    prices: pd.DataFrame,
    proxies: tuple[HierarchyProxy, ...],
    as_of: date,
    benchmark_symbol: str = "QQQ",
) -> dict[str, dict[str, Any]]:
    """Compute causal trend/reversal states for every hierarchy proxy."""
    metrics = _symbol_metrics(prices, as_of=as_of, benchmark_symbol=benchmark_symbol)
    states: dict[str, dict[str, Any]] = {}
    for proxy in proxies:
        proxy_metrics = metrics.get(proxy.symbol, {})
        state = classify_trend_state(proxy_metrics)
        reversal_context = classify_reversal_context(proxy_metrics, state)
        states[proxy.symbol] = {
            "symbol": proxy.symbol,
            "layer": proxy.layer,
            "role": proxy.role,
            "proxy_for": proxy.proxy_for,
            "tradable": proxy.tradable,
            "universe_role": proxy.universe_role,
            "sector": proxy.sector,
            "theme": proxy.theme,
            "subtheme": proxy.subtheme,
            "trend_state": state,
            **reversal_context,
            **proxy_metrics,
        }
    return states


def classify_trend_state(metrics: dict[str, Any]) -> str:
    """Classify a symbol into a conservative trend/reversal state."""
    required = ("return_5d", "return_20d", "return_60d", "relative_return_20d")
    if any(metrics.get(key) is None for key in required):
        return "INSUFFICIENT_DATA"

    return_5d = float(metrics["return_5d"])
    return_20d = float(metrics["return_20d"])
    return_60d = float(metrics["return_60d"])
    relative_20d = float(metrics["relative_return_20d"])
    drawdown_20d = float(metrics.get("drawdown_20d") or 0)
    drawdown_60d = float(metrics.get("drawdown_60d") or 0)
    rsi14 = float(metrics.get("rsi14") or 50)
    ma20_slope_5d = float(metrics.get("ma20_slope_5d") or 0)
    ma50_slope_20d = float(metrics.get("ma50_slope_20d") or 0)
    down_day_ratio_20d = float(metrics.get("down_day_ratio_20d") or 0)
    above_ma20 = bool(metrics.get("above_ma20"))
    above_ma50 = bool(metrics.get("above_ma50"))

    if (drawdown_20d >= 0.12 or drawdown_60d >= 0.22 or rsi14 <= 32) and return_5d < 0:
        return "WASHOUT"
    if (
        return_20d < 0
        and ma20_slope_5d < 0
        and down_day_ratio_20d >= 0.55
        and drawdown_20d >= 0.03
    ):
        return "DRIFT_DOWN"
    if (
        return_20d > 0
        and relative_20d > 0
        and above_ma20
        and ma20_slope_5d > 0
        and (above_ma50 or ma50_slope_20d >= 0)
    ):
        return "CONFIRMED_REVERSAL"
    if return_5d > 0 and above_ma20 and (relative_20d > -0.02 or rsi14 >= 45):
        return "REVERSAL_ATTEMPT"
    if return_20d > 0 and return_60d > 0 and above_ma20:
        return "UPTREND"
    if return_20d < 0 < return_60d:
        return "WEAKENING"
    return "LAGGING"


def classify_reversal_context(metrics: dict[str, Any], trend_state: str) -> dict[str, Any]:
    """Explain whether a symbol is still drifting lower or trying to repair.

    This is read-only diagnostic context. It does not approve trades by itself; the
    existing hard strategy rules, sector confirmation, and manual-review gates stay
    responsible for candidate eligibility.
    """
    if trend_state == "INSUFFICIENT_DATA":
        return {
            "reversal_phase": "INSUFFICIENT_DATA",
            "reversal_score": None,
            "reversal_reasons": "",
        }

    return_5d = _float_or_none(metrics.get("return_5d"))
    return_20d = _float_or_none(metrics.get("return_20d"))
    relative_20d = _float_or_none(metrics.get("relative_return_20d"))
    drawdown_20d = _float_or_none(metrics.get("drawdown_20d"))
    drawdown_60d = _float_or_none(metrics.get("drawdown_60d"))
    rsi14 = _float_or_none(metrics.get("rsi14"))
    ma20_slope_5d = _float_or_none(metrics.get("ma20_slope_5d"))
    down_day_ratio_20d = _float_or_none(metrics.get("down_day_ratio_20d"))
    above_ma20 = bool(metrics.get("above_ma20"))
    above_ma50 = bool(metrics.get("above_ma50"))
    reclaimed_previous_high = bool(metrics.get("reclaimed_previous_high"))

    score = 0.0
    reasons: list[str] = []
    if trend_state == "CONFIRMED_REVERSAL":
        score += 3.0
        reasons.append("trend_confirmed_reversal")
    elif trend_state == "REVERSAL_ATTEMPT":
        score += 2.0
        reasons.append("trend_reversal_attempt")
    elif trend_state == "UPTREND":
        score += 1.5
        reasons.append("trend_uptrend")
    elif trend_state in {"DRIFT_DOWN", "WASHOUT"}:
        score -= 2.0
        reasons.append(f"trend_{trend_state.lower()}")
    elif trend_state == "WEAKENING":
        score -= 0.5
        reasons.append("trend_weakening")

    if _positive(return_5d):
        score += 1.0
        reasons.append("positive_5d")
    if _positive(return_20d):
        score += 1.0
        reasons.append("positive_20d")
    if _positive(relative_20d):
        score += 1.0
        reasons.append("beating_benchmark_20d")
    if above_ma20:
        score += 1.0
        reasons.append("above_ma20")
    if above_ma50:
        score += 0.5
        reasons.append("above_ma50")
    if _positive(ma20_slope_5d):
        score += 0.75
        reasons.append("ma20_slope_turning_up")
    if reclaimed_previous_high:
        score += 0.75
        reasons.append("reclaimed_previous_high")
    if rsi14 is not None and rsi14 >= 45:
        score += 0.5
        reasons.append("rsi_recovered")

    if drawdown_20d is not None and drawdown_20d >= 0.08:
        score -= 0.75
        reasons.append("deep_20d_drawdown")
    if drawdown_60d is not None and drawdown_60d >= 0.18:
        score -= 0.75
        reasons.append("deep_60d_drawdown")
    if down_day_ratio_20d is not None and down_day_ratio_20d >= 0.55:
        score -= 0.75
        reasons.append("persistent_down_days")

    phase = "NEUTRAL"
    if trend_state == "WASHOUT":
        phase = "CAPITULATION_WASHOUT"
    elif trend_state == "DRIFT_DOWN":
        phase = "DRIFTING_LOWER"
    elif trend_state == "CONFIRMED_REVERSAL" and score >= 5.0:
        phase = "CONFIRMED_REPAIR"
    elif trend_state in {"REVERSAL_ATTEMPT", "CONFIRMED_REVERSAL", "UPTREND"} and score >= 3.5:
        phase = "REPAIR_ATTEMPT"
    elif trend_state == "WEAKENING":
        phase = "WEAKENING"
    elif trend_state == "LAGGING":
        phase = "LAGGING"

    return {
        "reversal_phase": phase,
        "reversal_score": _round_or_none(max(0.0, min(score, 10.0))),
        "reversal_reasons": ";".join(reasons),
    }


def calibrate_strategy_context(
    *,
    state_by_symbol: dict[str, dict[str, Any]],
    rotation_summary: dict[str, Any],
    pair_spreads: list[dict[str, Any]],
) -> dict[str, Any]:
    """Translate diagnostics into a non-binding strategy calibration posture."""
    qqq_state = state_by_symbol.get("QQQ", {}).get("trend_state", "MISSING")
    spy_state = state_by_symbol.get("SPY", {}).get("trend_state", "MISSING")
    ai_states = [
        row
        for row in state_by_symbol.values()
        if row.get("universe_role") == "ai_alpha" and row.get("role") == "leader_stock"
    ]
    hedge_states = [
        row for row in state_by_symbol.values() if row.get("universe_role") == "hedge_overlay"
    ]
    ai_breadth = _breadth(ai_states)
    hedge_breadth = _breadth(hedge_states)
    reversal_context = _reversal_context(
        qqq_state=qqq_state,
        spy_state=spy_state,
        ai_states=ai_states,
        hedge_states=hedge_states,
    )
    ai_vs_hedge = _find_spread(pair_spreads, "ai_alpha_vs_hedge_overlay")
    spread_20d = ai_vs_hedge.get("spread_relative_20d") if ai_vs_hedge else None
    spread_60d = ai_vs_hedge.get("spread_relative_60d") if ai_vs_hedge else None
    market_risk_off = qqq_state in {"DRIFT_DOWN", "WASHOUT", "LAGGING"} and spy_state in {
        "DRIFT_DOWN",
        "WASHOUT",
        "LAGGING",
    }
    ai_reversal = ai_breadth["reversal_or_confirmed_fraction"] >= 0.45
    ai_down = ai_breadth["drift_or_washout_fraction"] >= 0.45
    defensive_leadership = _is_negative(spread_20d) and (
        _is_negative(spread_60d)
        or rotation_summary.get("status") == "AI_MOMENTUM_WEAKENING"
    )

    if market_risk_off or (ai_down and defensive_leadership):
        tier = "DEFENSIVE"
        risk_multiplier = 0.0
        message = "Market and/or AI leadership is hostile; review cash and defensive overlays."
    elif defensive_leadership or qqq_state in {"DRIFT_DOWN", "WASHOUT"}:
        tier = "STRICT"
        risk_multiplier = 0.5
        if defensive_leadership:
            message = "Use strict entries only; short-window rotation is not supporting AI risk."
        else:
            message = "Use strict entries only; the primary growth-market proxy is weak."
    elif ai_reversal and qqq_state in {
        "REVERSAL_ATTEMPT",
        "CONFIRMED_REVERSAL",
        "UPTREND",
    }:
        tier = "RELAXED_WATCHLIST"
        risk_multiplier = 0.75
        message = "AI leaders show reversal breadth; allow relaxed watchlist review, not auto-buy."
    else:
        tier = "BASELINE"
        risk_multiplier = 1.0
        message = "Use the canonical baseline rules without extra calibration pressure."

    return {
        "candidate_tier_context": tier,
        "risk_multiplier_hint": risk_multiplier,
        "message": message,
        "market_state": {
            "QQQ": qqq_state,
            "SPY": spy_state,
        },
        "ai_leader_breadth": ai_breadth,
        "hedge_breadth": hedge_breadth,
        "reversal_context": reversal_context,
        "ai_vs_hedge_spread_20d": spread_20d,
        "ai_vs_hedge_spread_60d": spread_60d,
    }


def _symbol_metrics(
    prices: pd.DataFrame,
    *,
    as_of: date,
    benchmark_symbol: str,
) -> dict[str, dict[str, Any]]:
    if prices.empty:
        return {}
    frame = prices.copy()
    frame["symbol"] = frame["symbol"].str.upper()
    frame["session_date_ny"] = pd.to_datetime(frame["session_date_ny"]).dt.date
    frame = frame.loc[frame["session_date_ny"] <= as_of].sort_values(
        ["symbol", "session_date_ny"]
    )
    raw_metrics: dict[str, dict[str, Any]] = {}
    for symbol, symbol_frame in frame.groupby("symbol", sort=True):
        raw_metrics[symbol] = _single_symbol_metrics(symbol_frame)

    benchmark = raw_metrics.get(benchmark_symbol.upper(), {})
    for metrics in raw_metrics.values():
        for period in (5, 20, 60):
            symbol_return = metrics.get(f"return_{period}d")
            benchmark_return = benchmark.get(f"return_{period}d")
            metrics[f"relative_return_{period}d"] = _round_or_none(
                None
                if symbol_return is None or benchmark_return is None
                else float(symbol_return) - float(benchmark_return)
            )
    return raw_metrics


def _single_symbol_metrics(symbol_frame: pd.DataFrame) -> dict[str, Any]:
    closes = symbol_frame["close"].astype(float).reset_index(drop=True)
    highs = symbol_frame["high"].astype(float).reset_index(drop=True)
    volumes = symbol_frame["volume"].astype(float).reset_index(drop=True)
    latest_close = float(closes.iloc[-1]) if not closes.empty else None
    metrics: dict[str, Any] = {
        "close": _round_or_none(latest_close),
        "return_5d": _window_return(closes, 5),
        "return_20d": _window_return(closes, 20),
        "return_60d": _window_return(closes, 60),
        "drawdown_20d": _drawdown(closes, 20),
        "drawdown_60d": _drawdown(closes, 60),
        "ma5": _rolling_mean(closes, 5),
        "ma20": _rolling_mean(closes, 20),
        "ma50": _rolling_mean(closes, 50),
        "ma20_slope_5d": _ma_slope(closes, 20, 5),
        "ma50_slope_20d": _ma_slope(closes, 50, 20),
        "rsi14": _latest_rsi(closes, 14),
        "down_day_ratio_20d": _down_day_ratio(closes, 20),
        "volume_ratio_20d": _volume_ratio(volumes, 20),
        "reclaimed_previous_high": _reclaimed_previous_high(closes, highs),
    }
    metrics["above_ma20"] = _above(latest_close, metrics["ma20"])
    metrics["above_ma50"] = _above(latest_close, metrics["ma50"])
    return metrics


def _report_rows(state_by_symbol: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = list(state_by_symbol.values())
    rows.sort(
        key=lambda row: (
            _layer_rank(str(row["layer"])),
            str(row["proxy_for"]),
            str(row["symbol"]),
        )
    )
    return rows


def _breadth(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "member_count": 0,
            "above_ma20_fraction": None,
            "positive_20d_fraction": None,
            "reversal_or_confirmed_fraction": None,
            "drift_or_washout_fraction": None,
        }
    return {
        "member_count": len(rows),
        "above_ma20_fraction": _round_or_none(
            sum(bool(row.get("above_ma20")) for row in rows) / len(rows)
        ),
        "positive_20d_fraction": _round_or_none(
            sum(_is_positive(row.get("return_20d")) for row in rows) / len(rows)
        ),
        "reversal_or_confirmed_fraction": _round_or_none(
            sum(
                row.get("trend_state") in {"REVERSAL_ATTEMPT", "CONFIRMED_REVERSAL"}
                for row in rows
            )
            / len(rows)
        ),
        "drift_or_washout_fraction": _round_or_none(
            sum(row.get("trend_state") in {"DRIFT_DOWN", "WASHOUT"} for row in rows)
            / len(rows)
        ),
    }


def _reversal_context(
    *,
    qqq_state: str,
    spy_state: str,
    ai_states: list[dict[str, Any]],
    hedge_states: list[dict[str, Any]],
) -> dict[str, Any]:
    ai_repair_fraction = _phase_fraction(
        ai_states,
        {"REPAIR_ATTEMPT", "CONFIRMED_REPAIR"},
    )
    ai_confirmed_fraction = _phase_fraction(ai_states, {"CONFIRMED_REPAIR"})
    ai_drift_fraction = _phase_fraction(
        ai_states,
        {"DRIFTING_LOWER", "CAPITULATION_WASHOUT"},
    )
    hedge_repair_fraction = _phase_fraction(
        hedge_states,
        {"REPAIR_ATTEMPT", "CONFIRMED_REPAIR"},
    )
    market_repairing = qqq_state in REPAIR_STATES and spy_state not in DOWN_STATES
    market_hostile = qqq_state in DOWN_STATES and spy_state in DOWN_STATES

    if market_hostile or (ai_drift_fraction is not None and ai_drift_fraction >= 0.45):
        status = "DOWNTREND_OR_WASHOUT"
        action_hint = "DEFENSE_FIRST"
        message = (
            "AI leaders are still drifting or washed out; reversal entries need "
            "more confirmation."
        )
    elif (
        market_repairing
        and ai_repair_fraction is not None
        and ai_repair_fraction >= 0.35
    ):
        status = "EARLY_REPAIR"
        action_hint = "WATCH_FOR_CONFIRMATION"
        message = (
            "Market and AI leaders are attempting repair; keep this as watch "
            "context until hard rules confirm."
        )
    elif ai_confirmed_fraction is not None and ai_confirmed_fraction >= 0.35:
        status = "CONFIRMED_REPAIR"
        action_hint = "ALLOW_EXISTING_GATES_TO_WORK"
        message = (
            "A meaningful share of AI leaders is in confirmed repair; existing "
            "gates may surface candidates."
        )
    elif hedge_repair_fraction is not None and hedge_repair_fraction >= 0.35:
        status = "DEFENSIVE_REPAIR_LEADING"
        action_hint = "REVIEW_DEFENSIVE_OVERLAY"
        message = "Defensive overlays show stronger repair than AI leaders."
    else:
        status = "NO_CLEAR_REPAIR"
        action_hint = "BASELINE_DISCIPLINE"
        message = "No broad repair signal; use baseline discipline."

    return {
        "status": status,
        "action_hint": action_hint,
        "message": message,
        "market_repairing": market_repairing,
        "market_hostile": market_hostile,
        "ai_leader_repair_fraction": ai_repair_fraction,
        "ai_leader_confirmed_fraction": ai_confirmed_fraction,
        "ai_leader_drift_fraction": ai_drift_fraction,
        "hedge_repair_fraction": hedge_repair_fraction,
    }


def _phase_fraction(rows: list[dict[str, Any]], phases: set[str]) -> float | None:
    if not rows:
        return None
    return _round_or_none(sum(row.get("reversal_phase") in phases for row in rows) / len(rows))


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "row_count": len(rows),
        "market_proxy_count": sum(row["layer"] == "market" for row in rows),
        "sector_proxy_count": sum(row["layer"] == "sector" for row in rows),
        "stock_count": sum(row["layer"] == "stock" for row in rows),
        "leader_stock_count": sum(row["role"] == "leader_stock" for row in rows),
        "drift_down_count": sum(row["trend_state"] == "DRIFT_DOWN" for row in rows),
        "washout_count": sum(row["trend_state"] == "WASHOUT" for row in rows),
        "reversal_attempt_count": sum(
            row["trend_state"] == "REVERSAL_ATTEMPT" for row in rows
        ),
        "confirmed_reversal_count": sum(
            row["trend_state"] == "CONFIRMED_REVERSAL" for row in rows
        ),
        "repair_attempt_count": sum(
            row["reversal_phase"] == "REPAIR_ATTEMPT" for row in rows
        ),
        "confirmed_repair_count": sum(
            row["reversal_phase"] == "CONFIRMED_REPAIR" for row in rows
        ),
    }


def _window_return(closes: pd.Series, period: int) -> float | None:
    if len(closes) <= period:
        return None
    base = float(closes.iloc[-period - 1])
    latest = float(closes.iloc[-1])
    return _round_or_none(None if base <= 0 else latest / base - 1)


def _drawdown(closes: pd.Series, period: int) -> float | None:
    if len(closes) < period:
        return None
    latest = float(closes.iloc[-1])
    high = float(closes.tail(period).max())
    return _round_or_none(None if high <= 0 else 1 - latest / high)


def _rolling_mean(values: pd.Series, period: int) -> float | None:
    if len(values) < period:
        return None
    return _round_or_none(float(values.tail(period).mean()))


def _ma_slope(closes: pd.Series, ma_period: int, slope_period: int) -> float | None:
    if len(closes) < ma_period + slope_period:
        return None
    ma = closes.rolling(ma_period, min_periods=ma_period).mean()
    current = float(ma.iloc[-1])
    previous = float(ma.iloc[-slope_period - 1])
    return _round_or_none(None if previous <= 0 else current / previous - 1)


def _latest_rsi(closes: pd.Series, period: int) -> float | None:
    if len(closes) <= period:
        return None
    delta = closes.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    average_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    average_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    if pd.isna(average_gain.iloc[-1]) or pd.isna(average_loss.iloc[-1]):
        return None
    if average_loss.iloc[-1] == 0:
        return 100.0 if average_gain.iloc[-1] > 0 else 50.0
    relative_strength = average_gain.iloc[-1] / average_loss.iloc[-1]
    return _round_or_none(100 - 100 / (1 + relative_strength))


def _down_day_ratio(closes: pd.Series, period: int) -> float | None:
    if len(closes) <= period:
        return None
    changes = closes.diff().tail(period)
    return _round_or_none(float((changes < 0).sum() / len(changes)))


def _volume_ratio(volumes: pd.Series, period: int) -> float | None:
    if len(volumes) < period + 1:
        return None
    trailing = float(volumes.tail(period).mean())
    previous = float(volumes.iloc[-period - 1])
    return _round_or_none(None if trailing <= 0 else previous / trailing)


def _reclaimed_previous_high(closes: pd.Series, highs: pd.Series) -> bool | None:
    if len(closes) < 2 or len(highs) < 2:
        return None
    return bool(float(closes.iloc[-1]) > float(highs.iloc[-2]))


def _above(value: float | None, threshold: object) -> bool | None:
    if value is None or threshold is None:
        return None
    return bool(float(value) > float(threshold))


def _layer_rank(layer: str) -> int:
    return {"market": 0, "sector": 1, "stock": 2}.get(layer, 99)


def _find_spread(rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((row for row in rows if row["name"] == name), None)


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _is_positive(value: object) -> bool:
    return value is not None and float(value) > 0


def _is_negative(value: object) -> bool:
    return value is not None and float(value) < 0


def _positive(value: float | None) -> bool:
    return value is not None and value > 0


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _ensure_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
