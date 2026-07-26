"""Position exit checks for manually recorded holdings."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pandas as pd

from quant_system.backtest.workflow import load_feature_history
from quant_system.decision.journal import OpenPosition, reconstruct_open_positions
from quant_system.decision.reports import DailyReportArtifacts, write_position_check_report
from quant_system.domain.clocks import NyseSessionClock
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.storage.sqlite import OperationsRegistry
from quant_system.strategy.buy_the_dip import BuyTheDipStrategy
from quant_system.strategy.config import BuyTheDipConfig

ACTION_REQUIRED = {
    "EXIT_STOP",
    "EXIT_TARGET",
    "EXIT_TIME",
    "DEFENSIVE_ROTATION",
    "REVIEW_MISSING_PLAN",
}


def run_position_check_workflow(
    *,
    repository: ParquetRepository,
    database_path: Path,
    operations_database_path: Path,
    report_root: Path,
    as_of: date,
    config: BuyTheDipConfig,
    run_id: UUID | None = None,
    generated_at_utc: datetime | None = None,
) -> tuple[dict[str, Any], DailyReportArtifacts]:
    """Check manually recorded open positions against daily exit rules."""
    run_id = run_id or uuid4()
    generated_at_utc = _ensure_utc(generated_at_utc or datetime.now(UTC))
    with OperationsRegistry(operations_database_path) as registry:
        positions = reconstruct_open_positions(registry.manual_fills())

    symbols = tuple(position.symbol for position in positions)
    bars = _load_daily_bars(
        repository=repository,
        database_path=database_path,
        symbols=symbols,
        as_of=as_of,
    )
    features, market_regime = _load_position_features_and_regime(
        repository=repository,
        database_path=database_path,
        symbols=symbols,
        as_of=as_of,
        config=config,
    )
    rows = [
        evaluate_position(
            position,
            bar=bars.get(position.symbol),
            feature_row=features.get(position.symbol),
            market_regime=market_regime,
            as_of=as_of,
            config=config,
        )
        for position in positions
    ]
    report: dict[str, Any] = {
        "metadata": {
            "run_id": str(run_id),
            "generated_at_utc": generated_at_utc.isoformat(),
            "as_of": as_of.isoformat(),
            "market_regime": market_regime,
            "model_status": "RULES_AND_MANUAL_JOURNAL_ONLY",
        },
        "counts": {
            "open_position_count": len(rows),
            "action_required_count": sum(
                row["recommended_action"] in ACTION_REQUIRED for row in rows
            ),
            "hold_count": sum(row["recommended_action"] == "HOLD" for row in rows),
        },
        "positions": rows,
    }
    artifacts = write_position_check_report(
        report=report,
        rows=rows,
        report_root=report_root,
        as_of=as_of,
        run_id=run_id,
    )
    report["artifacts"] = {
        "directory": str(artifacts.directory),
        "json": str(artifacts.json_path),
        "csv": str(artifacts.csv_path),
        "markdown": str(artifacts.markdown_path),
    }
    artifacts = write_position_check_report(
        report=report,
        rows=rows,
        report_root=report_root,
        as_of=as_of,
        run_id=run_id,
    )
    return report, artifacts


def evaluate_position(
    position: OpenPosition,
    *,
    bar: dict[str, float] | None,
    feature_row: dict[str, float] | None,
    market_regime: str,
    as_of: date,
    config: BuyTheDipConfig,
) -> dict[str, Any]:
    """Evaluate one open position using pessimistic daily-bar exit assumptions."""
    sessions_held = _sessions_held(position.entry_time_utc, as_of)
    fallback_stop, fallback_target = _fallback_exit_prices(
        position,
        feature_row=feature_row,
        config=config,
    )
    stop_price = position.stop_price or fallback_stop
    target_price = position.target_price or fallback_target
    if bar is None:
        return _position_payload(
            position,
            as_of=as_of,
            current_close=position.average_entry_price,
            stop_price=stop_price,
            target_price=target_price,
            sessions_held=sessions_held,
            recommended_action="REVIEW_MISSING_PLAN",
            primary_reason="missing_price_bar",
            estimated_exit_price=None,
        )

    action = "HOLD"
    reason = "exit_rules_not_triggered"
    estimated_exit_price: float | None = None
    slippage = config.execution.slippage_bps / 10_000
    if stop_price is None or target_price is None:
        action = "REVIEW_MISSING_PLAN"
        reason = "missing_stop_or_target"
    elif float(bar["low"]) <= stop_price:
        action = "EXIT_STOP"
        reason = "stop_touched_daily_bar"
        estimated_exit_price = min(float(bar["open"]), stop_price) * (1 - slippage)
    elif float(bar["high"]) >= target_price:
        action = "EXIT_TARGET"
        reason = "target_touched_daily_bar"
        estimated_exit_price = target_price * (1 - slippage)
    elif sessions_held >= config.execution.holding_sessions:
        action = "EXIT_TIME"
        reason = "holding_period_reached"
        estimated_exit_price = float(bar["close"]) * (1 - slippage)
    elif market_regime == "RED":
        action = "DEFENSIVE_ROTATION"
        reason = "benchmark_regime_red"
    return _position_payload(
        position,
        as_of=as_of,
        current_close=float(bar["close"]),
        stop_price=stop_price,
        target_price=target_price,
        sessions_held=sessions_held,
        recommended_action=action,
        primary_reason=reason,
        estimated_exit_price=estimated_exit_price,
    )


def _position_payload(
    position: OpenPosition,
    *,
    as_of: date,
    current_close: float,
    stop_price: float | None,
    target_price: float | None,
    sessions_held: int,
    recommended_action: str,
    primary_reason: str,
    estimated_exit_price: float | None,
) -> dict[str, Any]:
    unrealized_pnl = (current_close - position.average_entry_price) * position.quantity
    invested = position.average_entry_price * position.quantity
    return {
        "symbol": position.symbol,
        "as_of": as_of.isoformat(),
        "quantity": position.quantity,
        "average_entry_price": round(position.average_entry_price, 4),
        "current_close": round(current_close, 4),
        "market_value": round(current_close * position.quantity, 4),
        "unrealized_pnl": round(unrealized_pnl, 4),
        "unrealized_return_pct": round(100 * unrealized_pnl / invested, 4)
        if invested
        else 0.0,
        "stop_price": round(stop_price, 4) if stop_price is not None else None,
        "target_price": round(target_price, 4) if target_price is not None else None,
        "sessions_held": sessions_held,
        "recommended_action": recommended_action,
        "primary_reason": primary_reason,
        "estimated_exit_price": round(estimated_exit_price, 4)
        if estimated_exit_price is not None
        else None,
        "signal_id": position.signal_id,
        "entry_time_utc": position.entry_time_utc.isoformat(),
        "last_fill_time_utc": position.last_fill_time_utc.isoformat(),
    }


def _fallback_exit_prices(
    position: OpenPosition,
    *,
    feature_row: dict[str, float] | None,
    config: BuyTheDipConfig,
) -> tuple[float | None, float | None]:
    if feature_row is None or pd.isna(feature_row.get("atr20")):
        return None, None
    atr20 = float(feature_row["atr20"])
    stop = max(0.01, position.average_entry_price - config.execution.stop_atr_multiple * atr20)
    target = position.average_entry_price + config.execution.target_atr_multiple * atr20
    return stop, target


def _load_daily_bars(
    *,
    repository: ParquetRepository,
    database_path: Path,
    symbols: tuple[str, ...],
    as_of: date,
) -> dict[str, dict[str, float]]:
    if not symbols:
        return {}
    with DuckDBAnalytics(database_path, repository.daily_prices_root) as analytics:
        analytics.refresh_views()
        frame = analytics.query_price_history(symbols, start_date=as_of, end_date=as_of)
    return {
        str(row["symbol"]): {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        for _, row in frame.iterrows()
    }


def _load_position_features_and_regime(
    *,
    repository: ParquetRepository,
    database_path: Path,
    symbols: tuple[str, ...],
    as_of: date,
    config: BuyTheDipConfig,
) -> tuple[dict[str, dict[str, float]], str]:
    if not symbols:
        return {}, "UNKNOWN"
    try:
        features = load_feature_history(
            repository=repository,
            database_path=database_path,
            symbols=symbols,
            benchmark_symbol=config.strategy.benchmark_symbol,
            start=as_of,
            end=as_of,
        )
    except ValueError:
        return {}, "UNKNOWN"
    annotated = BuyTheDipStrategy(config.strategy).annotate(features)
    today = annotated.loc[annotated["session_date_ny"].dt.date == as_of]
    by_symbol = {
        str(row["symbol"]): row.to_dict()
        for _, row in today.loc[today["symbol"].isin(symbols)].iterrows()
    }
    benchmark = today.loc[today["symbol"] == config.strategy.benchmark_symbol.upper()]
    market_regime = "UNKNOWN" if benchmark.empty else str(benchmark.iloc[-1]["market_regime"])
    return by_symbol, market_regime


def _sessions_held(entry_time_utc: datetime, as_of: date) -> int:
    entry_date = pd.Timestamp(entry_time_utc).tz_convert("America/New_York").date()
    clock = NyseSessionClock()
    start = clock.calendar.date_to_session(entry_date, direction="next")
    end = clock.calendar.date_to_session(as_of, direction="previous")
    if end < start:
        return 0
    return len(clock.calendar.sessions_in_range(start, end))


def _ensure_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
