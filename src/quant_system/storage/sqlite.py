"""SQLite storage for operational state, journal facts, and model registry summaries."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4


class OperationsRegistry:
    """SQLite-backed registry for model run summaries only."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row

    def initialize(self) -> None:
        """Create operational registry tables if needed."""
        self.initialize_model_registry()
        self.initialize_journal()
        self.connection.commit()

    def initialize_model_registry(self) -> None:
        """Create model registry tables if needed."""
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS model_registry (
                run_id TEXT PRIMARY KEY,
                model_name TEXT NOT NULL,
                status TEXT NOT NULL,
                promotion_status TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                report_directory TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            )
            """
        )

    def initialize_journal(self) -> None:
        """Create human journal tables for decisions and manual fills."""
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_decisions (
                decision_id TEXT PRIMARY KEY,
                signal_id TEXT,
                symbol TEXT NOT NULL,
                decision_session TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                report_run_id TEXT,
                created_at_utc TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS fills_manual (
                fill_id TEXT PRIMARY KEY,
                signal_id TEXT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                price REAL NOT NULL CHECK (price > 0),
                commission REAL NOT NULL CHECK (commission >= 0),
                fill_time_utc TEXT NOT NULL,
                stop_price REAL CHECK (stop_price IS NULL OR stop_price > 0),
                target_price REAL CHECK (target_price IS NULL OR target_price > 0),
                notes TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_fills_manual_symbol_time
            ON fills_manual(symbol, fill_time_utc)
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_manual_decisions_symbol_session
            ON manual_decisions(symbol, decision_session)
            """
        )
        self.connection.commit()

    def record_model_run(self, summary: dict[str, Any]) -> None:
        """Insert or replace a model registry summary."""
        self.initialize()
        self.connection.execute(
            """
            INSERT OR REPLACE INTO model_registry (
                run_id,
                model_name,
                status,
                promotion_status,
                config_hash,
                report_directory,
                summary_json,
                created_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                str(summary["run_id"]),
                str(summary["model"]),
                str(summary["status"]),
                str(summary.get("promotion_status", "NOT_EVALUATED")),
                str(summary["config_hash"]),
                str(summary["report_directory"]),
                json.dumps(summary, sort_keys=True, separators=(",", ":")),
                datetime.now(UTC).isoformat(),
            ],
        )
        self.connection.commit()

    def record_manual_decision(
        self,
        *,
        symbol: str,
        decision_session: str,
        action: str,
        reason: str = "",
        signal_id: UUID | str | None = None,
        report_run_id: UUID | str | None = None,
        decision_id: UUID | str | None = None,
        created_at_utc: datetime | None = None,
    ) -> dict[str, Any]:
        """Record a human decision linked to an optional system signal."""
        self.initialize_journal()
        created_at = _ensure_utc(created_at_utc or datetime.now(UTC))
        record = {
            "decision_id": str(decision_id or uuid4()),
            "signal_id": str(signal_id) if signal_id else None,
            "symbol": symbol.upper(),
            "decision_session": decision_session,
            "action": action.upper(),
            "reason": reason,
            "report_run_id": str(report_run_id) if report_run_id else None,
            "created_at_utc": created_at.isoformat(),
        }
        self.connection.execute(
            """
            INSERT INTO manual_decisions (
                decision_id,
                signal_id,
                symbol,
                decision_session,
                action,
                reason,
                report_run_id,
                created_at_utc
            )
            VALUES (
                :decision_id,
                :signal_id,
                :symbol,
                :decision_session,
                :action,
                :reason,
                :report_run_id,
                :created_at_utc
            )
            """,
            record,
        )
        self.connection.commit()
        return record

    def record_manual_fill(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        fill_time_utc: datetime,
        commission: float = 0.0,
        signal_id: UUID | str | None = None,
        stop_price: float | None = None,
        target_price: float | None = None,
        notes: str = "",
        fill_id: UUID | str | None = None,
        created_at_utc: datetime | None = None,
    ) -> dict[str, Any]:
        """Record a manual fill fact in SQLite operations storage."""
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if price <= 0:
            raise ValueError("price must be positive")
        if commission < 0:
            raise ValueError("commission must not be negative")
        normalized_side = side.upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        self.initialize_journal()
        fill_time = _ensure_utc(fill_time_utc)
        created_at = _ensure_utc(created_at_utc or datetime.now(UTC))
        record = {
            "fill_id": str(fill_id or uuid4()),
            "signal_id": str(signal_id) if signal_id else None,
            "symbol": symbol.upper(),
            "side": normalized_side,
            "quantity": int(quantity),
            "price": float(price),
            "commission": float(commission),
            "fill_time_utc": fill_time.isoformat(),
            "stop_price": float(stop_price) if stop_price is not None else None,
            "target_price": float(target_price) if target_price is not None else None,
            "notes": notes,
            "created_at_utc": created_at.isoformat(),
        }
        self.connection.execute(
            """
            INSERT INTO fills_manual (
                fill_id,
                signal_id,
                symbol,
                side,
                quantity,
                price,
                commission,
                fill_time_utc,
                stop_price,
                target_price,
                notes,
                created_at_utc
            )
            VALUES (
                :fill_id,
                :signal_id,
                :symbol,
                :side,
                :quantity,
                :price,
                :commission,
                :fill_time_utc,
                :stop_price,
                :target_price,
                :notes,
                :created_at_utc
            )
            """,
            record,
        )
        self.connection.commit()
        return record

    def manual_fills(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        """Return manual fills sorted by fill time."""
        self.initialize_journal()
        if symbol:
            rows = self.connection.execute(
                """
                SELECT *
                FROM fills_manual
                WHERE symbol = ?
                ORDER BY fill_time_utc, created_at_utc, fill_id
                """,
                [symbol.upper()],
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT *
                FROM fills_manual
                ORDER BY fill_time_utc, created_at_utc, fill_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def manual_decisions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent human decisions."""
        self.initialize_journal()
        rows = self.connection.execute(
            """
            SELECT *
            FROM manual_decisions
            ORDER BY created_at_utc DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_model_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent model registry summaries."""
        self.initialize()
        rows = self.connection.execute(
            """
            SELECT summary_json
            FROM model_registry
            ORDER BY created_at_utc DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [json.loads(row["summary_json"]) for row in rows]

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> OperationsRegistry:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _ensure_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
