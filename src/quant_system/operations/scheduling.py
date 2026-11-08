"""Exchange-calendar daily scheduling, durable retries and Telegram outbox."""

import fcntl
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from quant_system.domain.clocks import NyseSessionClock
from quant_system.models.activation import DailyRankingPolicy
from quant_system.operations.digest_notifications import notification_pages, redact_pages
from quant_system.operations.pipeline import run_daily_pipeline
from quant_system.operations.registry import PipelineBusy
from quant_system.operations.telegram import TelegramClient, TelegramError
from quant_system.settings import PROJECT_ROOT


class DailySchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(default="daily", pattern=r"^[a-z0-9_-]+$")
    trigger: Literal["after_close", "premarket"] = "after_close"
    after_close_minutes: int = Field(default=60, ge=30, le=360)
    premarket_minutes: int = Field(default=60, ge=10, le=240)
    manual_news: bool = True
    poll_seconds: int = Field(default=60, ge=60, le=600)
    pipeline_attempts: int = Field(default=2, ge=1, le=3)
    pipeline_backoff_minutes: int = Field(default=15, ge=1, le=60)
    notification_attempts: int = Field(default=3, ge=1, le=5)
    log_max_bytes: int = Field(default=5_000_000, ge=1000)
    log_backups: int = Field(default=3, ge=1, le=10)
    ranking: DailyRankingPolicy = DailyRankingPolicy()
    session_jobs: tuple[Literal["premarket", "preopen", "open30", "open60", "weekly"], ...] = ()
    intraday_config_path: Path = PROJECT_ROOT / "configs/sources/intraday.yaml"
    digest_config_path: Path = PROJECT_ROOT / "configs/reports/daily_digest.yaml"
    market_context_config_path: Path = PROJECT_ROOT / "configs/sources/market_context.yaml"

    @model_validator(mode="after")
    def validate_session_jobs(self):
        if len(set(self.session_jobs)) != len(self.session_jobs):
            raise ValueError("session jobs must be unique")
        if "premarket" in self.session_jobs and self.premarket_minutes <= 12:
            raise ValueError("premarket must precede the preopen snapshot window")
        if self.trigger == "premarket" and "premarket" in self.session_jobs:
            raise ValueError("configure one premarket pipeline trigger")
        return self


def load_schedule(path):
    return DailySchedule.model_validate(yaml.safe_load(Path(path).read_text()))


def schedule_windows(config, now):
    if now.utcoffset() is None:
        raise ValueError("schedule clock must be timezone-aware")
    now = now.astimezone(UTC)
    clock = NyseSessionClock()
    day = now.astimezone(ZoneInfo("America/New_York")).date()
    windows = []
    for label in clock.calendar.sessions_in_range(
        day - timedelta(days=10), day + timedelta(days=10)
    ):
        session = label.date()
        order = clock.next_session(session)
        deadline = clock.session_open_utc(order)
        due = (
            clock.session_close_utc(session) + timedelta(minutes=config.after_close_minutes)
            if config.trigger == "after_close"
            else deadline - timedelta(minutes=config.premarket_minutes)
        )
        windows.append({"session": session.isoformat(), "due": due, "deadline": deadline})
    active = [window for window in windows if window["due"] <= now < window["deadline"]]
    future = [window for window in windows if window["due"] > now]
    return (
        max(active, key=lambda window: window["due"]) if active else None,
        min(future, key=lambda window: window["due"]),
    )


@contextmanager
def scheduler_lock(data_root):
    path = Path(data_root) / "db/scheduled-daily.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PipelineBusy("scheduler already running") from None
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def rotate_logs(directory, config):
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.glob("automation*.log"):
        if path.stat().st_size < config.log_max_bytes:
            continue
        for index in range(config.log_backups, 0, -1):
            old = path.with_name(path.name + f".{index}")
            if index == config.log_backups:
                old.unlink(missing_ok=True)
            elif old.exists():
                old.replace(path.with_name(path.name + f".{index + 1}"))
        path.replace(path.with_name(path.name + ".1"))


class ScheduleState:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS scheduled_daily_runs (
                job_key TEXT PRIMARY KEY, session TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS telegram_outbox (
                delivery_key TEXT PRIMARY KEY, destination_hash TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_at TEXT NOT NULL, text TEXT NOT NULL, message_id INTEGER,
                error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS telegram_delivery_limits (
                destination_hash TEXT PRIMARY KEY, next_at TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(telegram_outbox)")}
        with self.db:
            for column, definition in {
                "bundle_key": "TEXT",
                "part_index": "INTEGER NOT NULL DEFAULT 1",
                "total_parts": "INTEGER NOT NULL DEFAULT 1",
                "expires_at": "TEXT",
            }.items():
                if column not in columns:
                    self.db.execute(f"ALTER TABLE telegram_outbox ADD COLUMN {column} {definition}")
            self.db.execute(
                "UPDATE telegram_outbox SET bundle_key=delivery_key WHERE bundle_key IS NULL"
            )

    def close(self):
        self.db.close()

    def job(self, key, session, now):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO scheduled_daily_runs VALUES (?,?, 'PENDING',0,?,'{}')",
                (key, session, now.isoformat()),
            )
        return dict(
            self.db.execute("SELECT * FROM scheduled_daily_runs WHERE job_key=?", (key,)).fetchone()
        )

    def reserve(self, key, next_at):
        with self.db:
            self.db.execute(
                "UPDATE scheduled_daily_runs SET status='RUNNING',attempts=attempts+1,"
                "next_at=?,result_json='{}' WHERE job_key=?",
                (next_at.isoformat(), key),
            )

    def recover(self, prefix=None):
        query = "SELECT * FROM scheduled_daily_runs WHERE status='RUNNING'"
        if prefix is not None:
            rows = self.db.execute(
                query + " AND substr(job_key,1,?)=?", (len(prefix), prefix)
            ).fetchall()
        else:
            rows = self.db.execute(query).fetchall()
        for row in rows:
            self.finish(
                row["job_key"], "FAILED", {"status": "FAILED", "error_type": "InterruptedPipeline"}
            )
        return rows

    def finish(self, key, status, result):
        result = {"observed_at_utc": datetime.now(UTC).isoformat(), **result}
        with self.db:
            self.db.execute(
                "UPDATE scheduled_daily_runs SET status=?,result_json=? WHERE job_key=?",
                (status, json.dumps(result, default=str), key),
            )

    def enqueue(self, key, destination, text, now):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO telegram_outbox "
                "(delivery_key,destination_hash,status,next_at,text,bundle_key) "
                "VALUES (?,?,'PENDING',?,?,?)",
                (key, destination, now.isoformat(), text, key),
            )

    def enqueue_pages(self, key, destination, pages, now, *, expires_at=None):
        if not pages:
            raise ValueError("notification bundle must contain pages")
        if self.db.execute("SELECT 1 FROM telegram_outbox WHERE delivery_key=?", (key,)).fetchone():
            return  # A legacy single-message delivery owns this key already.
        with self.db:
            for index, text in enumerate(pages, 1):
                if not text or len(text.encode("utf-16-le")) // 2 > 4096:
                    raise ValueError("invalid Telegram page")
                delivery = f"{key}|part:{index:04d}"
                existing = self.db.execute(
                    "SELECT text,total_parts,destination_hash FROM telegram_outbox "
                    "WHERE delivery_key=?",
                    (delivery,),
                ).fetchone()
                if existing and tuple(existing) != (text, len(pages), destination):
                    raise ValueError("immutable notification bundle conflict")
                self.db.execute(
                    "INSERT OR IGNORE INTO telegram_outbox "
                    "(delivery_key,destination_hash,status,next_at,text,bundle_key,part_index,"
                    "total_parts,expires_at) VALUES (?,?,'PENDING',?,?,?,?,?,?)",
                    (
                        delivery,
                        destination,
                        now.isoformat(),
                        text,
                        key,
                        index,
                        len(pages),
                        expires_at.isoformat() if expires_at else None,
                    ),
                )

    def deliver(self, client, chat_id, destination, now, max_attempts, *, bundle_key=None):
        # A crashed reservation may have reached Telegram; never guess and resend it.
        with self.db:
            self.db.execute(
                "UPDATE telegram_outbox SET status='UNKNOWN',error_code='interrupted_delivery' "
                "WHERE destination_hash=? AND status='SENDING'",
                (destination,),
            )
            self.db.execute(
                "UPDATE telegram_outbox SET status='EXPIRED',error_code='notification_expired' "
                "WHERE destination_hash=? AND status IN ('PENDING','RETRY') "
                "AND expires_at IS NOT NULL AND expires_at<=?",
                (destination, now.isoformat()),
            )
        query = "SELECT * FROM telegram_outbox WHERE destination_hash=?"
        args = [destination]
        if bundle_key is not None:
            query += " AND bundle_key=?"
            args.append(bundle_key)
        rows = self.db.execute(query + " ORDER BY rowid", args).fetchall()
        # Telegram's retry-after applies to this destination, not just a single page.
        rate_wait = self.db.execute(
            "SELECT 1 FROM telegram_delivery_limits WHERE destination_hash=? AND next_at>? "
            "UNION ALL SELECT 1 FROM telegram_outbox WHERE destination_hash=? "
            "AND error_code='telegram_rate_limited' AND next_at>?",
            (destination, now.isoformat(), destination, now.isoformat()),
        ).fetchone()
        blocked, attempts, receipts = set(), 0, []
        for row in rows:
            key, status, bundle = row["delivery_key"], row["status"], row["bundle_key"]
            if status != "SENT" and bundle in blocked:
                continue
            if status in {"PENDING", "RETRY"}:
                due = datetime.fromisoformat(row["next_at"]) <= now
                if due and not rate_wait and attempts < 5 and row["attempts"] < max_attempts:
                    with self.db:
                        self.db.execute(
                            "UPDATE telegram_outbox SET status='SENDING',"
                            "attempts=attempts+1 WHERE delivery_key=?",
                            (key,),
                        )
                    attempts += 1
                    try:
                        message_id = client.send(chat_id, row["text"])
                        with self.db:
                            self.db.execute(
                                "UPDATE telegram_outbox SET status='SENT',message_id=?,"
                                "error_code=NULL WHERE delivery_key=?",
                                (message_id, key),
                            )
                        status = "SENT"
                    except TelegramError as error:
                        status = (
                            "UNKNOWN"
                            if error.ambiguous
                            else "RETRY"
                            if error.retry_after and row["attempts"] + 1 < max_attempts
                            else "FAILED"
                        )
                        with self.db:
                            self.db.execute(
                                "UPDATE telegram_outbox SET status=?,next_at=?,error_code=? "
                                "WHERE delivery_key=?",
                                (
                                    status,
                                    (now + timedelta(seconds=error.retry_after)).isoformat(),
                                    str(error),
                                    key,
                                ),
                            )
                        if error.retry_after:
                            with self.db:
                                self.db.execute(
                                    "INSERT INTO telegram_delivery_limits VALUES (?,?) "
                                    "ON CONFLICT(destination_hash) DO UPDATE "
                                    "SET next_at=MAX(next_at,excluded.next_at)",
                                    (
                                        destination,
                                        (now + timedelta(seconds=error.retry_after)).isoformat(),
                                    ),
                                )
                            rate_wait = True
                elif row["attempts"] >= max_attempts:
                    status = "FAILED"
                    with self.db:
                        self.db.execute(
                            "UPDATE telegram_outbox SET status='FAILED' WHERE delivery_key=?",
                            (key,),
                        )
            if status != "SENT":
                blocked.add(bundle)
            receipts.append(
                {
                    "delivery_key": key,
                    "status": status,
                    "part_index": row["part_index"],
                    "total_parts": row["total_parts"],
                }
            )
        # Keep routine status output small; pending pages remain durably queryable.
        return receipts[-max(5, attempts) :]


def notification_text(result, session, *, attempt=1):
    """Compatibility preview; production callers enqueue the complete page list."""
    return "\n\n".join(notification_pages(result, session, attempt=attempt))


def tick_daily(settings, config, *, now_utc=None, pipeline=run_daily_pipeline, client=None):
    now = now_utc or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("schedule clock must be timezone-aware")
    now = now.astimezone(UTC)
    window, upcoming = schedule_windows(config, now)
    base = {
        "next_trigger_utc": upcoming["due"].isoformat(),
        "next_trigger_tokyo": upcoming["due"].astimezone(ZoneInfo("Asia/Tokyo")).isoformat(),
    }
    root = settings.resolved_data_dir
    try:
        with scheduler_lock(root):
            log_root = root / "logs"
            rotate_logs(log_root, config)
            if not settings.telegram_configured:
                return {
                    "status": "BLOCKED_CONFIGURATION",
                    "reason": "telegram_not_configured",
                    **base,
                }
            state = ScheduleState(root / "db/operations.sqlite")
            try:
                destination = hashlib.sha256(
                    (
                        settings.telegram_bot_token.get_secret_value()
                        + "|"
                        + settings.telegram_chat_id
                    ).encode()
                ).hexdigest()
                for interrupted in state.recover(
                    prefix=f"{config.name}|{config.trigger}|{config.manual_news}|"
                ):
                    key = f"{interrupted['job_key']}|{destination}|{interrupted['attempts']}"
                    state.enqueue(
                        key,
                        destination,
                        notification_text(
                            {"status": "FAILED", "error_type": "InterruptedPipeline"},
                            interrupted["session"],
                            attempt=interrupted["attempts"],
                        ),
                        now,
                    )
                result = {"status": "WAITING", **base}
                if window:
                    key = f"{config.name}|{config.trigger}|{config.manual_news}|{window['session']}"
                    job = state.job(key, window["session"], now)
                    if (
                        job["status"] != "DONE"
                        and job["attempts"] < config.pipeline_attempts
                        and datetime.fromisoformat(job["next_at"]) <= now
                    ):
                        state.reserve(key, now + timedelta(minutes=config.pipeline_backoff_minutes))
                        try:
                            ranking_args = (
                                {"ranking_policy": config.ranking}
                                if config.ranking.mode != "off"
                                else {}
                            )
                            output = pipeline(
                                settings,
                                signal_session=datetime.fromisoformat(window["session"]).date(),
                                mode="live",
                                manual_news=config.manual_news,
                                digest_config_path=config.digest_config_path,
                                market_context_config_path=config.market_context_config_path,
                                now_utc=now if now_utc is not None else None,
                                **ranking_args,
                            )
                        except Exception as error:
                            output = {"status": "FAILED", "error_type": type(error).__name__}
                        job_status = (
                            "DONE"
                            if output.get("status") in {"COMPLETED", "DEGRADED"}
                            else "FAILED"
                        )
                        # Full data health remains in the pipeline's report.
                        output = {
                            key: value
                            for key, value in output.items()
                            if key != "data_health" and key != "stages"
                        }
                        output["observed_at_utc"] = (
                            now if now_utc is not None else datetime.now(UTC)
                        ).isoformat()
                        state.finish(key, job_status, output)
                        job = state.job(key, window["session"], now)
                    output = json.loads(job["result_json"])
                    if output:
                        delivery_key = f"{key}|{destination}|{job['attempts']}"
                        pages = notification_pages(
                            output,
                            window["session"],
                            attempt=job["attempts"],
                            max_units=_page_budget(config),
                        )
                        state.enqueue_pages(
                            delivery_key, destination, redact_pages(pages, settings), now
                        )
                    result = {
                        "status": job["status"],
                        "session": window["session"],
                        "attempts": job["attempts"],
                        "pipeline_status": output.get("status"),
                        **base,
                    }
                result["notifications"] = state.deliver(
                    client or TelegramClient(settings.telegram_bot_token),
                    settings.telegram_chat_id,
                    destination,
                    now,
                    config.notification_attempts,
                )
                if result["status"] != "WAITING":
                    with (log_root / "automation.log").open("a") as stream:
                        stream.write(json.dumps({"time_utc": now.isoformat(), **result}) + "\n")
                return result
            finally:
                state.close()
    except PipelineBusy:
        return {"status": "SKIPPED_BUSY", **base}


def _page_budget(config):
    from quant_system.universe.config import load_daily_digest_config

    return load_daily_digest_config(config.digest_config_path).telegram_page_units
