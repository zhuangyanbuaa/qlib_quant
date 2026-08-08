"""Forward paper-trading ledger for Phase 6 daily workflow checks."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from quant_system.decision.gates import load_json_report
from quant_system.decision.journal import reconstruct_open_positions
from quant_system.decision.positions import evaluate_position
from quant_system.decision.reports import DailyReportArtifacts, write_decision_table_report
from quant_system.domain.clocks import NyseSessionClock
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.storage.sqlite import OperationsRegistry
from quant_system.strategy.config import BuyTheDipConfig


def run_paper_update_workflow(
    *,
    premarket_report_path: Path,
    repository: ParquetRepository,
    database_path: Path,
    operations_database_path: Path,
    report_root: Path,
    config: BuyTheDipConfig,
    gate_report_path: Path | None = None,
    fill_session: date | None = None,
    quantity: int = 1,
    run_id: UUID | None = None,
    generated_at_utc: datetime | None = None,
) -> tuple[dict[str, Any], DailyReportArtifacts]:
    """Create one-share paper entries for eligible premarket candidates."""
    if quantity < 1:
        raise ValueError("paper quantity must be positive")
    run_id = run_id or uuid4()
    generated_at_utc = _ensure_utc(generated_at_utc or datetime.now(UTC))
    premarket = load_json_report(premarket_report_path)
    gates = _gate_decisions(gate_report_path)
    target_session = fill_session or date.fromisoformat(
        premarket["metadata"]["earliest_order_session"]
    )
    bars = _daily_bars(
        repository=repository,
        database_path=database_path,
        symbols=tuple(candidate["symbol"] for candidate in premarket.get("candidates", [])),
        as_of=target_session,
    )
    rows: list[dict[str, Any]] = []
    clock = NyseSessionClock()
    with OperationsRegistry(operations_database_path) as store:
        existing_signals = {
            fill["signal_id"]
            for fill in store.paper_fills()
            if fill["side"] == "BUY" and fill.get("signal_id")
        }
        for candidate in premarket.get("candidates", []):
            row = _paper_entry_row(
                candidate,
                gate_decision=gates.get(candidate["symbol"]),
                bar=bars.get(candidate["symbol"]),
                target_session=target_session,
                config=config,
                existing_signals=existing_signals,
            )
            rows.append(row)
            if row["paper_action"] != "BUY":
                continue
            fill = store.record_paper_fill(
                fill_id=uuid5(
                    NAMESPACE_URL,
                    f"paper:{candidate['signal_id']}:entry:{target_session.isoformat()}",
                ),
                signal_id=candidate["signal_id"],
                symbol=candidate["symbol"],
                side="BUY",
                quantity=quantity,
                price=row["fill_price"],
                commission=row["commission"],
                fill_time_utc=clock.session_open_utc(target_session),
                stop_price=candidate["stop_price"],
                target_price=candidate["target_price"],
                notes=f"paper_entry_from={premarket_report_path}",
            )
            row["fill_id"] = fill["fill_id"]
    report = _paper_report(
        run_id=run_id,
        generated_at_utc=generated_at_utc,
        rows=rows,
        stem="paper_update",
        source_report_path=premarket_report_path,
        gate_report_path=gate_report_path,
        as_of=target_session,
    )
    artifacts = _write_report(
        report=report,
        rows=rows,
        report_root=report_root,
        as_of=target_session,
        run_id=run_id,
        stem="paper_update",
        title="Forward Paper Update",
    )
    return report, artifacts


def run_paper_advance_workflow(
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
    """Advance paper positions and write exit fills when rules trigger."""
    run_id = run_id or uuid4()
    generated_at_utc = _ensure_utc(generated_at_utc or datetime.now(UTC))
    with OperationsRegistry(operations_database_path) as store:
        positions = reconstruct_open_positions(store.paper_fills())
        bars = _daily_bars(
            repository=repository,
            database_path=database_path,
            symbols=tuple(position.symbol for position in positions),
            as_of=as_of,
        )
        rows = []
        clock = NyseSessionClock()
        for position in positions:
            evaluation = evaluate_position(
                position,
                bar=bars.get(position.symbol),
                feature_row=None,
                market_regime="UNKNOWN",
                as_of=as_of,
                config=config,
            )
            row = {
                **evaluation,
                "paper_action": "HOLD",
                "fill_id": None,
            }
            if evaluation["recommended_action"] in {"EXIT_STOP", "EXIT_TARGET", "EXIT_TIME"}:
                exit_price = evaluation["estimated_exit_price"] or evaluation["current_close"]
                fill = store.record_paper_fill(
                    fill_id=uuid5(
                        NAMESPACE_URL,
                        f"paper:{position.symbol}:{position.entry_time_utc.isoformat()}:exit:{as_of}",
                    ),
                    signal_id=position.signal_id,
                    symbol=position.symbol,
                    side="SELL",
                    quantity=position.quantity,
                    price=float(exit_price),
                    commission=position.quantity
                    * float(exit_price)
                    * config.execution.commission_bps
                    / 10_000,
                    fill_time_utc=clock.session_close_utc(as_of),
                    notes=f"paper_exit:{evaluation['recommended_action']}",
                )
                row["paper_action"] = "SELL"
                row["fill_id"] = fill["fill_id"]
            rows.append(row)
    report = _paper_report(
        run_id=run_id,
        generated_at_utc=generated_at_utc,
        rows=rows,
        stem="paper_advance",
        source_report_path=None,
        gate_report_path=None,
        as_of=as_of,
    )
    artifacts = _write_report(
        report=report,
        rows=rows,
        report_root=report_root,
        as_of=as_of,
        run_id=run_id,
        stem="paper_advance",
        title="Forward Paper Advance",
    )
    return report, artifacts


def _paper_entry_row(
    candidate: dict[str, Any],
    *,
    gate_decision: str | None,
    bar: dict[str, float] | None,
    target_session: date,
    config: BuyTheDipConfig,
    existing_signals: set[str],
) -> dict[str, Any]:
    row = {
        "symbol": candidate["symbol"],
        "signal_id": candidate["signal_id"],
        "fill_session": target_session.isoformat(),
        "gate_decision": gate_decision,
        "open_price": None,
        "fill_price": None,
        "commission": None,
        "paper_action": "SKIP",
        "reason": "not_evaluated",
        "fill_id": None,
    }
    if candidate["signal_id"] in existing_signals:
        row["reason"] = "paper_signal_already_filled"
        return row
    if candidate.get("recommended_action") != "PREPARE_MANUAL_CONDITIONAL_ORDER":
        row["reason"] = "not_ai_alpha_order_draft"
        return row
    if gate_decision not in {None, "KEEP"}:
        row["reason"] = f"gate_decision_{gate_decision.lower()}"
        return row
    if bar is None:
        row["reason"] = "missing_entry_bar"
        return row
    gap_return = float(bar["open"]) / float(candidate["signal_close"]) - 1
    if gap_return > config.execution.maximum_gap_up:
        row["reason"] = "entry_gap_up_above_plan"
        return row
    if gap_return < -config.execution.maximum_gap_down:
        row["reason"] = "entry_gap_down_below_plan"
        return row
    fill_price = float(bar["open"]) * (1 + config.execution.slippage_bps / 10_000)
    row.update(
        {
            "open_price": float(bar["open"]),
            "fill_price": fill_price,
            "commission": fill_price * config.execution.commission_bps / 10_000,
            "paper_action": "BUY",
            "reason": "paper_entry_filled_at_daily_open",
        }
    )
    return row


def _gate_decisions(gate_report_path: Path | None) -> dict[str, str]:
    if gate_report_path is None:
        return {}
    report = load_json_report(gate_report_path)
    return {row["symbol"]: row["decision"] for row in report.get("rows", [])}


def _daily_bars(
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


def _paper_report(
    *,
    run_id: UUID,
    generated_at_utc: datetime,
    rows: list[dict[str, Any]],
    stem: str,
    source_report_path: Path | None,
    gate_report_path: Path | None,
    as_of: date,
) -> dict[str, Any]:
    return {
        "metadata": {
            "run_id": str(run_id),
            "generated_at_utc": generated_at_utc.isoformat(),
            "as_of": as_of.isoformat(),
            "workflow": stem,
            "source_report_path": str(source_report_path) if source_report_path else None,
            "gate_report_path": str(gate_report_path) if gate_report_path else None,
            "execution_scope": "forward_paper_only",
        },
        "counts": {
            "row_count": len(rows),
            "buy_count": sum(row.get("paper_action") == "BUY" for row in rows),
            "sell_count": sum(row.get("paper_action") == "SELL" for row in rows),
            "hold_count": sum(row.get("paper_action") == "HOLD" for row in rows),
            "skip_count": sum(row.get("paper_action") == "SKIP" for row in rows),
        },
        "rows": rows,
    }


def _write_report(
    *,
    report: dict[str, Any],
    rows: list[dict[str, Any]],
    report_root: Path,
    as_of: date,
    run_id: UUID,
    stem: str,
    title: str,
) -> DailyReportArtifacts:
    artifacts = write_decision_table_report(
        report=report,
        rows=rows,
        report_root=report_root,
        as_of=as_of,
        run_id=run_id,
        stem=stem,
        title=title,
    )
    report["artifacts"] = {
        "directory": str(artifacts.directory),
        "json": str(artifacts.json_path),
        "csv": str(artifacts.csv_path),
        "markdown": str(artifacts.markdown_path),
        "html": str(artifacts.html_path),
    }
    return write_decision_table_report(
        report=report,
        rows=rows,
        report_root=report_root,
        as_of=as_of,
        run_id=run_id,
        stem=stem,
        title=title,
    )


def _ensure_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

