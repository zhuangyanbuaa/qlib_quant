"""Atomic operational request budgets shared across local processes."""

import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from quant_system.ingestion.reliability import RequestBudgetExceeded


class SQLiteDailyCallBudget:
    """Reserve attempts durably; reset at UTC midnight, never on process restart."""

    def __init__(
        self,
        maximum_calls: int,
        *,
        database_path: Path,
        provider: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if maximum_calls < 1 or not provider.strip():
            raise ValueError("a positive budget and provider name are required")
        self.maximum_calls = maximum_calls
        self.database_path = database_path.resolve()
        self.provider = provider
        self.clock = clock
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS provider_call_budgets (
                    provider TEXT NOT NULL, day_utc TEXT NOT NULL,
                    used INTEGER NOT NULL CHECK(used >= 0),
                    maximum_calls INTEGER NOT NULL CHECK(maximum_calls > 0),
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY(provider, day_utc)
                )
            """)
        self._reserve(consume=False)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=5)
        try:
            yield connection
        finally:
            connection.close()

    def _reserve(self, *, consume: bool) -> int:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("budget clock must be timezone-aware")
        now = now.astimezone(UTC)
        key = (self.provider, now.date().isoformat())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO provider_call_budgets VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(provider, day_utc) DO UPDATE SET
                maximum_calls=min(maximum_calls, excluded.maximum_calls)
            """,
                (*key, self.maximum_calls, now.isoformat()),
            )
            used, limit = connection.execute(
                "SELECT used, maximum_calls FROM provider_call_budgets "
                "WHERE provider=? AND day_utc=?",
                key,
            ).fetchone()
            exhausted = consume and used >= limit
            if consume and not exhausted:
                used += 1
                connection.execute(
                    "UPDATE provider_call_budgets SET used=?, updated_at_utc=? "
                    "WHERE provider=? AND day_utc=?",
                    (used, now.isoformat(), *key),
                )
            # Persist a stricter configuration even when the attempted call is refused.
            connection.commit()
        if exhausted:
            raise RequestBudgetExceeded(
                f"daily provider call budget exhausted ({self.provider}:{limit})"
            )
        return used

    @property
    def used(self) -> int:
        return self._reserve(consume=False)

    def consume(self) -> None:
        self._reserve(consume=True)


class SQLiteMinuteRateLimit:
    """Reserve rolling-minute slots across processes; refuse excess calls immediately."""

    def __init__(self, maximum_calls, *, database_path, provider, clock=lambda: datetime.now(UTC)):
        if maximum_calls < 1:
            raise ValueError("positive minute limit required")
        self.maximum_calls = maximum_calls
        self.path = database_path
        self.provider = provider
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS provider_minute_slots "
                "(provider TEXT NOT NULL, reserved_at REAL NOT NULL)"
            )

    def acquire(self):
        instant = self.clock()
        if instant.utcoffset() is None:
            raise ValueError("minute budget clock must be aware")
        now = instant.timestamp()
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM provider_minute_slots WHERE provider=? AND reserved_at<=?",
                (self.provider, now - 60),
            )
            count = db.execute(
                "SELECT count(*) FROM provider_minute_slots WHERE provider=?", (self.provider,)
            ).fetchone()[0]
            if count >= self.maximum_calls:
                db.commit()
                raise RequestBudgetExceeded("provider_minute_rate_limit")
            db.execute("INSERT INTO provider_minute_slots VALUES (?,?)", (self.provider, now))
