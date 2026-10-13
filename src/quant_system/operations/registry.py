"""Crash-safe process exclusion and durable pipeline progress."""

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class PipelineBusy(RuntimeError):
    pass


@contextmanager
def pipeline_lock(data_root: Path):
    path = data_root / "db" / "daily-pipeline.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PipelineBusy("another data pipeline is running") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class PipelineRegistry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                run_id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL, created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL, summary_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS pipeline_steps (
                run_id TEXT NOT NULL, step TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL, updated_at_utc TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(run_id,step)
            );
        """)

    def close(self):
        self.connection.close()

    def start(self, key):
        now = datetime.now(UTC).isoformat()
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO pipeline_runs VALUES (?,?,?,?,?,?)",
                (str(uuid4()), key, "RUNNING", now, now, "{}"),
            )
        return dict(
            self.connection.execute(
                "SELECT * FROM pipeline_runs WHERE request_key=?", (key,)
            ).fetchone()
        )

    def step(self, run_id, name):
        row = self.connection.execute(
            "SELECT * FROM pipeline_steps WHERE run_id=? AND step=?", (run_id, name)
        ).fetchone()
        return dict(row) if row else None

    def begin_step(self, run_id, name):
        with self.connection:
            self.connection.execute(
                """INSERT INTO pipeline_steps VALUES (?,?, 'RUNNING',1,?,'{}')
                ON CONFLICT(run_id,step) DO UPDATE SET status='RUNNING',
                attempts=attempts+1, updated_at_utc=excluded.updated_at_utc""",
                (run_id, name, datetime.now(UTC).isoformat()),
            )

    def end_step(self, run_id, name, status, result):
        with self.connection:
            self.connection.execute(
                "UPDATE pipeline_steps SET status=?,result_json=?,updated_at_utc=? "
                "WHERE run_id=? AND step=?",
                (
                    status,
                    json.dumps(result, default=str),
                    datetime.now(UTC).isoformat(),
                    run_id,
                    name,
                ),
            )

    def finish(self, run_id, status, summary):
        with self.connection:
            self.connection.execute(
                "UPDATE pipeline_runs SET status=?,summary_json=?,updated_at_utc=? WHERE run_id=?",
                (status, json.dumps(summary, default=str), datetime.now(UTC).isoformat(), run_id),
            )

    def stages(self, run_id):
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM pipeline_steps WHERE run_id=? ORDER BY rowid", (run_id,)
            )
        ]
