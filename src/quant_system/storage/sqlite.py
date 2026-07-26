"""SQLite storage for operational state and model registry summaries."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any


class OperationsRegistry:
    """SQLite-backed registry for model run summaries only."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row

    def initialize(self) -> None:
        """Create operational registry tables if needed."""
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
