"""Sector and theme rotation diagnostics for strategy calibration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pandas as pd

from quant_system.decision.reports import DailyReportArtifacts, write_decision_table_report
from quant_system.domain.clocks import NyseSessionClock
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.universe.config import load_watchlist_config

DEFAULT_ROTATION_PERIODS = (20, 60)


@dataclass(frozen=True)
class RotationMember:
    """A watchlist member with grouping metadata used for rotation diagnostics."""

    symbol: str
    universe_role: str
    sector: str
    theme: str
    subtheme: str
    benchmark_etf: str


def load_rotation_members(paths: tuple[Path, ...]) -> tuple[RotationMember, ...]:
    """Load deduplicated rotation members from watchlist YAML files."""
    members_by_symbol: dict[str, RotationMember] = {}
    for path in paths:
        config = load_watchlist_config(path)
        universe_role = (
            "hedge_overlay" if config.universe_type == "HEDGE_OVERLAY" else "ai_alpha"
        )
        for member in config.symbols:
            members_by_symbol.setdefault(
                member.symbol,
                RotationMember(
                    symbol=member.symbol,
                    universe_role=universe_role,
                    sector=member.sector,
                    theme=member.theme,
                    subtheme=member.subtheme,
                    benchmark_etf=member.benchmark_etf,
                ),
            )
    return tuple(members_by_symbol[symbol] for symbol in sorted(members_by_symbol))


def run_rotation_diagnostics_workflow(
    *,
    repository: ParquetRepository,
    database_path: Path,
    report_root: Path,
    universe_paths: tuple[Path, ...],
    as_of: date,
    benchmark_symbol: str = "QQQ",
    periods: tuple[int, int] = DEFAULT_ROTATION_PERIODS,
    run_id: UUID | None = None,
    generated_at_utc: datetime | None = None,
) -> tuple[dict[str, Any], DailyReportArtifacts]:
    """Generate and persist read-only group rotation diagnostics."""
    run_id = run_id or uuid4()
    generated_at_utc = _ensure_utc(generated_at_utc or datetime.now(UTC))
    benchmark_symbol = benchmark_symbol.upper()
    members = load_rotation_members(universe_paths)
    benchmark_symbols = tuple(
        sorted({benchmark_symbol, *(member.benchmark_etf for member in members)})
    )
    requested_symbols = tuple(sorted({*(member.symbol for member in members), *benchmark_symbols}))
    max_period = max(periods)
    start_date = as_of - timedelta(days=max(220, max_period * 4))

    with DuckDBAnalytics(database_path, repository.daily_prices_root) as analytics:
        analytics.refresh_views()
        prices = analytics.query_price_history(
            requested_symbols,
            start_date=start_date,
            end_date=as_of,
        )

    rows = compute_rotation_rows(
        prices=prices,
        members=members,
        as_of=as_of,
        periods=periods,
        default_benchmark_symbol=benchmark_symbol,
    )
    pair_spreads = compute_pair_spreads(rows)
    posture = rotation_posture(rows, pair_spreads)
    report: dict[str, Any] = {
        "metadata": {
            "run_id": str(run_id),
            "generated_at_utc": generated_at_utc.isoformat(),
            "as_of": as_of.isoformat(),
            "data_cutoff_utc": NyseSessionClock().session_close_utc(as_of).isoformat(),
            "default_benchmark_symbol": benchmark_symbol,
            "periods": list(periods),
            "symbol_count": len(requested_symbols),
            "member_count": len(members),
            "universe_history_policy": "CURRENT_SNAPSHOT_FORWARD_ONLY_FOR_DIAGNOSTICS",
            "decision_scope": "READ_ONLY_CALIBRATION_DIAGNOSTIC",
        },
        "counts": _counts(rows),
        "portfolio_posture": posture,
        "pair_spreads": pair_spreads,
        "rows": rows,
    }
    artifacts = write_decision_table_report(
        report=report,
        rows=rows,
        report_root=report_root,
        as_of=as_of,
        run_id=run_id,
        stem="rotation",
        title="Rotation Diagnostics",
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
        stem="rotation",
        title="Rotation Diagnostics",
    )
    return report, artifacts


def compute_rotation_rows(
    *,
    prices: pd.DataFrame,
    members: tuple[RotationMember, ...],
    as_of: date,
    periods: tuple[int, int] = DEFAULT_ROTATION_PERIODS,
    default_benchmark_symbol: str = "QQQ",
) -> list[dict[str, Any]]:
    """Compute equal-weight relative-strength rows by universe, sector, theme, subtheme."""
    period_short, period_long = periods
    symbol_returns = _symbol_returns(prices, as_of=as_of, periods=periods)
    rows: list[dict[str, Any]] = []
    for group_type, grouped_members in _member_groups(members).items():
        for group_name, group_members in grouped_members.items():
            benchmark_symbol = _group_benchmark(group_members, default_benchmark_symbol)
            row = _group_row(
                group_type=group_type,
                group_name=group_name,
                group_members=group_members,
                benchmark_symbol=benchmark_symbol,
                symbol_returns=symbol_returns,
                period_short=period_short,
                period_long=period_long,
            )
            rows.append(row)
    rows.sort(
        key=lambda row: (
            _safe_rank_value(row[f"relative_return_{period_short}d"]),
            _safe_rank_value(row[f"relative_return_{period_long}d"]),
            row["group_type"],
            row["group"],
        ),
        reverse=True,
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def compute_pair_spreads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute named rotation spreads that are useful for manual review."""
    by_key = {(row["group_type"], row["group"]): row for row in rows}
    pairs = [
        (
            "ai_alpha_vs_hedge_overlay",
            ("universe_role", "ai_alpha"),
            ("universe_role", "hedge_overlay"),
        ),
        (
            "ai_chips_vs_ai_platform_software",
            ("theme", "ai_chips"),
            ("theme", "ai_platform_software"),
        ),
        (
            "semiconductor_supply_chain_vs_ai_platform_software",
            ("theme", "semiconductor_supply_chain"),
            ("theme", "ai_platform_software"),
        ),
        (
            "data_center_power_cooling_vs_ai_platform_software",
            ("theme", "data_center_power_cooling"),
            ("theme", "ai_platform_software"),
        ),
    ]
    spreads: list[dict[str, Any]] = []
    for name, left_key, right_key in pairs:
        left = by_key.get(left_key)
        right = by_key.get(right_key)
        if left is None or right is None:
            continue
        spread: dict[str, Any] = {"name": name, "left": left_key[1], "right": right_key[1]}
        for period in _row_periods(left):
            left_return = left.get(f"return_{period}d")
            right_return = right.get(f"return_{period}d")
            spread[f"spread_return_{period}d"] = _round_or_none(
                None if left_return is None or right_return is None else left_return - right_return
            )
            left_relative = left.get(f"relative_return_{period}d")
            right_relative = right.get(f"relative_return_{period}d")
            spread[f"spread_relative_{period}d"] = _round_or_none(
                None
                if left_relative is None or right_relative is None
                else left_relative - right_relative
            )
        spreads.append(spread)
    return spreads


def rotation_posture(
    rows: list[dict[str, Any]],
    pair_spreads: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize the rotation diagnostic into a compact portfolio posture hint."""
    ai_row = _find_row(rows, "universe_role", "ai_alpha")
    hedge_row = _find_row(rows, "universe_role", "hedge_overlay")
    leading_themes = [
        row["group"]
        for row in rows
        if row["group_type"] == "theme" and row["rotation_status"] == "LEADING"
    ][:3]
    improving_themes = [
        row["group"]
        for row in rows
        if row["group_type"] == "theme" and row["rotation_status"] == "IMPROVING"
    ][:3]
    status = "NEUTRAL"
    message = "No strong rotation tilt detected; keep using rules-first candidate selection."
    spread = _find_spread(pair_spreads, "ai_alpha_vs_hedge_overlay")
    spread_20 = spread.get("spread_relative_20d") if spread else None
    spread_60 = spread.get("spread_relative_60d") if spread else None
    if _is_positive(spread_20) and _is_positive(spread_60):
        status = "RISK_ON_AI_LEADERSHIP"
        message = "AI alpha groups are leading hedge overlay on both 20d and 60d relative strength."
    elif _is_negative(spread_20) and _is_negative(spread_60):
        status = "DEFENSIVE_ROTATION_REVIEW"
        message = "Hedge overlay is stronger than AI alpha on both 20d and 60d relative strength."
    elif _is_negative(spread_20) and _is_positive(spread_60):
        status = "AI_MOMENTUM_WEAKENING"
        message = "AI alpha still has longer-window support but short-window rotation is weakening."
    elif _is_positive(spread_20) and _is_negative(spread_60):
        status = "AI_ROTATION_IMPROVING"
        message = "AI alpha is improving on the short window after lagging on the longer window."
    return {
        "status": status,
        "message": message,
        "ai_alpha_status": ai_row["rotation_status"] if ai_row else "MISSING",
        "hedge_overlay_status": hedge_row["rotation_status"] if hedge_row else "MISSING",
        "leading_themes": leading_themes,
        "improving_themes": improving_themes,
    }


def _member_groups(
    members: tuple[RotationMember, ...],
) -> dict[str, dict[str, tuple[RotationMember, ...]]]:
    dimensions = {
        "universe_role": lambda member: member.universe_role,
        "sector": lambda member: member.sector,
        "theme": lambda member: member.theme,
        "subtheme": lambda member: member.subtheme,
    }
    output: dict[str, dict[str, tuple[RotationMember, ...]]] = {}
    for dimension, getter in dimensions.items():
        grouped: dict[str, list[RotationMember]] = {}
        for member in members:
            grouped.setdefault(getter(member), []).append(member)
        output[dimension] = {
            name: tuple(sorted(values, key=lambda member: member.symbol))
            for name, values in grouped.items()
        }
    return output


def _group_row(
    *,
    group_type: str,
    group_name: str,
    group_members: tuple[RotationMember, ...],
    benchmark_symbol: str,
    symbol_returns: dict[str, dict[int, float | None]],
    period_short: int,
    period_long: int,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "group_type": group_type,
        "group": group_name,
        "member_count": len(group_members),
        "benchmark_symbol": benchmark_symbol,
    }
    for period in (period_short, period_long):
        values = [
            symbol_returns.get(member.symbol, {}).get(period) for member in group_members
        ]
        valid_values = [value for value in values if value is not None]
        benchmark_return = symbol_returns.get(benchmark_symbol, {}).get(period)
        group_return = _mean(valid_values)
        row[f"valid_{period}d"] = len(valid_values)
        row[f"return_{period}d"] = _round_or_none(group_return)
        row[f"benchmark_return_{period}d"] = _round_or_none(benchmark_return)
        relative_return = (
            None
            if group_return is None or benchmark_return is None
            else group_return - benchmark_return
        )
        row[f"relative_return_{period}d"] = _round_or_none(relative_return)
    row["rotation_status"] = _rotation_status(
        row[f"relative_return_{period_short}d"],
        row[f"relative_return_{period_long}d"],
    )
    return row


def _symbol_returns(
    prices: pd.DataFrame,
    *,
    as_of: date,
    periods: tuple[int, int],
) -> dict[str, dict[int, float | None]]:
    if prices.empty:
        return {}
    frame = prices.copy()
    frame["symbol"] = frame["symbol"].str.upper()
    frame["session_date_ny"] = pd.to_datetime(frame["session_date_ny"]).dt.date
    frame = frame.loc[frame["session_date_ny"] <= as_of].sort_values(
        ["symbol", "session_date_ny"]
    )
    returns: dict[str, dict[int, float | None]] = {}
    for symbol, symbol_frame in frame.groupby("symbol", sort=True):
        closes = symbol_frame["close"].astype(float).to_list()
        if not closes:
            continue
        latest_close = closes[-1]
        period_returns: dict[int, float | None] = {}
        for period in periods:
            if len(closes) <= period:
                period_returns[period] = None
                continue
            base_close = closes[-period - 1]
            period_returns[period] = (
                None if base_close <= 0 else float(latest_close / base_close - 1)
            )
        returns[symbol] = period_returns
    return returns


def _group_benchmark(
    group_members: tuple[RotationMember, ...],
    default_benchmark_symbol: str,
) -> str:
    benchmarks = {member.benchmark_etf for member in group_members}
    if len(benchmarks) == 1:
        return next(iter(benchmarks))
    return default_benchmark_symbol.upper()


def _rotation_status(relative_short: float | None, relative_long: float | None) -> str:
    if relative_short is None or relative_long is None:
        return "INSUFFICIENT_DATA"
    if relative_long > 0 and relative_short > 0:
        return "LEADING"
    if relative_long <= 0 < relative_short:
        return "IMPROVING"
    if relative_long > 0 and relative_short <= 0:
        return "WEAKENING"
    return "LAGGING"


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "row_count": len(rows),
        "leading_count": sum(row["rotation_status"] == "LEADING" for row in rows),
        "improving_count": sum(row["rotation_status"] == "IMPROVING" for row in rows),
        "weakening_count": sum(row["rotation_status"] == "WEAKENING" for row in rows),
        "lagging_count": sum(row["rotation_status"] == "LAGGING" for row in rows),
        "insufficient_data_count": sum(
            row["rotation_status"] == "INSUFFICIENT_DATA" for row in rows
        ),
    }


def _find_row(
    rows: list[dict[str, Any]],
    group_type: str,
    group: str,
) -> dict[str, Any] | None:
    return next(
        (row for row in rows if row["group_type"] == group_type and row["group"] == group),
        None,
    )


def _find_spread(rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((row for row in rows if row["name"] == name), None)


def _row_periods(row: dict[str, Any]) -> tuple[int, ...]:
    periods: list[int] = []
    for key in row:
        if key.startswith("return_") and key.endswith("d"):
            periods.append(int(key.removeprefix("return_").removesuffix("d")))
    return tuple(sorted(periods))


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _safe_rank_value(value: object) -> float:
    return float(value) if value is not None else float("-inf")


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _is_positive(value: object) -> bool:
    return value is not None and float(value) > 0


def _is_negative(value: object) -> bool:
    return value is not None and float(value) < 0


def _ensure_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
