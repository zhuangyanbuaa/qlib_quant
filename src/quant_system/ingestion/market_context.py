"""Bounded CNN context acquisition and point-in-time VIX from existing raw prices."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field

from quant_system.decision.digest import DigestValue
from quant_system.domain.clocks import NyseSessionClock
from quant_system.domain.market_context import MarketObservation
from quant_system.ingestion.budgets import SQLiteDailyCallBudget, SQLiteMinuteRateLimit
from quant_system.ingestion.reliability import ProviderGuard, RetryPolicy
from quant_system.quality.prices import price_quality_mask
from quant_system.settings import PROJECT_ROOT
from quant_system.storage.duckdb import DuckDBAnalytics

CNN_ENDPOINT = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CNN_SOURCE_VERSION = "cnn-graphdata-v1-unverified"
MANUAL_SOURCE_VERSION = "cnn-manual-import-v1"
VIX_VIEW_VERSION = "vix-context-v1"


class MarketContextSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["none", "cnn"] = "none"
    calls_per_minute: int = Field(default=2, ge=1, le=10)
    daily_call_budget: int = Field(default=4, ge=1, le=20)
    timeout_seconds: float = Field(default=10, gt=0, le=30)
    failure_backoff_seconds: int = Field(default=900, ge=60, le=86400)
    stale_after_sessions: int = Field(default=0, ge=0, le=5)


def load_market_context_settings(path=None):
    path = Path(path or PROJECT_ROOT / "configs/sources/market_context.yaml")
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return MarketContextSettings.model_validate(yaml.safe_load(path.read_text()))


class MarketContextUnavailable(RuntimeError):
    def __init__(self, code, *, retry_after_seconds=0):
        super().__init__(code)
        self.retry_after_seconds = retry_after_seconds


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def cnn_transport(timeout):
    request = Request(
        CNN_ENDPOINT, headers={"Accept": "application/json", "User-Agent": "qlib-quant/0.1"}
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise MarketContextUnavailable("cnn_response_too_large")
        return json.loads(raw)
    except HTTPError as error:
        try:
            delay = max(60, min(86400, int(error.headers.get("Retry-After", "900"))))
        except (TypeError, ValueError):
            delay = 900
        raise MarketContextUnavailable(
            f"cnn_http_{error.code}", retry_after_seconds=delay
        ) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise MarketContextUnavailable("cnn_transport_or_response_invalid") from None


def _utc(value):
    if value.utcoffset() is None:
        raise ValueError("market context clock must be timezone-aware")
    return value.astimezone(UTC)


def _observation(*, value, timestamp, label, source_url, fetched_at, run_id, manual=False):
    observed = _utc(datetime.fromisoformat(timestamp.replace("Z", "+00:00")))
    return MarketObservation(
        series_id="CNN_FEAR_GREED",
        value=value,
        label=label,
        observed_at_utc=observed,
        observation_date=observed.astimezone(ZoneInfo("America/New_York")).date(),
        source="cnn_manual" if manual else "cnn",
        source_url=source_url,
        source_version=MANUAL_SOURCE_VERSION if manual else CNN_SOURCE_VERSION,
        fetched_at_utc=_utc(fetched_at),
        available_at_utc=_utc(fetched_at),
        ingestion_run_id=run_id,
        quality_flags=("manual_transcription",) if manual else ("adapter_not_live_validated",),
    )


def parse_cnn(payload, *, fetched_at, run_id):
    try:
        values = payload["fear_and_greed"]
        return _observation(
            value=values["score"],
            timestamp=values["timestamp"],
            label=values.get("rating", ""),
            source_url=CNN_ENDPOINT,
            fetched_at=fetched_at,
            run_id=run_id,
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        raise MarketContextUnavailable("cnn_invalid_observation") from None


def import_manual_cnn(payload, *, repository, now_utc=None):
    """Import a sourced transcription; caller cannot supply/backdate availability."""
    required = {"value", "observed_at_utc", "source_url"}
    if (
        not isinstance(payload, dict)
        or not required <= payload.keys()
        or payload.keys() - required - {"label"}
    ):
        raise ValueError(
            "manual CNN requires value, observed_at_utc, source_url and optional label"
        )
    url = urlsplit(payload["source_url"])
    if (
        url.scheme != "https"
        or url.hostname not in {"cnn.com", "www.cnn.com", "edition.cnn.com"}
        or url.path.rstrip("/") != "/markets/fear-and-greed"
        or url.username
        or url.password
    ):
        raise ValueError("manual CNN requires the CNN Fear and Greed source page")
    run_id = uuid4()
    record = _observation(
        value=payload["value"],
        timestamp=payload["observed_at_utc"],
        label=payload.get("label", ""),
        source_url=payload["source_url"],
        fetched_at=_utc(now_utc or datetime.now(UTC)),
        run_id=run_id,
        manual=True,
    )
    paths = repository.write_market_observations([record], run_id=run_id)
    return {
        "status": "SUCCESS",
        "rows_written": 1,
        "run_id": str(run_id),
        "paths": [str(p) for p in paths],
    }


def collect_market_context(settings, config, repository, *, now_utc=None, transport=cnn_transport):
    now = _utc(now_utc or datetime.now(UTC))
    if config.provider == "none":
        return {
            "status": "UNAVAILABLE",
            "reason": "cnn_automatic_source_not_verified",
            "rows_written": 0,
        }
    database = settings.resolved_data_dir / "db/operations.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS market_context_backoff "
            "(provider TEXT PRIMARY KEY, next_at TEXT NOT NULL)"
        )
        row = db.execute(
            "SELECT next_at FROM market_context_backoff WHERE provider='cnn'"
        ).fetchone()
        if row and datetime.fromisoformat(row[0]) > now:
            return {"status": "UNAVAILABLE", "reason": "cnn_provider_backoff", "rows_written": 0}
    try:
        # One attempt per collection. Backoff and budgets survive process restarts.
        clock = (lambda: now) if now_utc is not None else lambda: datetime.now(UTC)
        guard = ProviderGuard(
            budget=SQLiteDailyCallBudget(
                config.daily_call_budget, database_path=database, provider="cnn", clock=clock
            ),
            limiter=SQLiteMinuteRateLimit(
                config.calls_per_minute, database_path=database, provider="cnn", clock=clock
            ),
            retry=RetryPolicy(attempts=1),
        )
        payload = guard.call(lambda: transport(config.timeout_seconds))
        fetched = now if now_utc is not None else datetime.now(UTC)
        run_id = uuid4()
        record = parse_cnn(payload, fetched_at=fetched, run_id=run_id)
        paths = repository.write_market_observations([record], run_id=run_id)
        return {
            "status": "SUCCESS",
            "rows_written": 1,
            "source_version": CNN_SOURCE_VERSION,
            "paths": [str(p) for p in paths],
        }
    except Exception as error:
        reason = str(error) if isinstance(error, MarketContextUnavailable) else type(error).__name__
        delay = max(config.failure_backoff_seconds, getattr(error, "retry_after_seconds", 0))
        with sqlite3.connect(database) as db:
            db.execute(
                "INSERT INTO market_context_backoff VALUES ('cnn', ?) "
                "ON CONFLICT(provider) DO UPDATE SET next_at=excluded.next_at",
                ((now + timedelta(seconds=delay)).isoformat(),),
            )
        return {"status": "UNAVAILABLE", "reason": reason, "rows_written": 0}


def _digest_value(record, *, session, stale_after_sessions):
    if record is None:
        return DigestValue(reason="no_observation_before_cutoff").model_dump(mode="json")
    if any(
        flag == "quality_blocked" or flag.startswith("invalid_") for flag in record.quality_flags
    ):
        return DigestValue(
            reason="latest_observation_unreliable", provenance=record.model_dump(mode="json")
        ).model_dump(mode="json")
    clock = NyseSessionClock()
    lag = (
        len(clock.calendar.sessions_in_range(record.observation_date + timedelta(days=1), session))
        if record.observation_date < session
        else 0
    )
    stale = record.is_stale or lag > stale_after_sessions
    return DigestValue(
        status="STALE" if stale else "AVAILABLE",
        value=record.value,
        reason="observation_stale" if stale else "",
        units=record.units,
        provenance=record.model_dump(mode="json") | {"derived_stale": stale, "session_lag": lag},
    ).model_dump(mode="json")


def select_market_context(repository, database_path, *, session, cutoff, config=None):
    config = config or MarketContextSettings()
    cutoff = _utc(cutoff)
    with DuckDBAnalytics(database_path, repository.daily_prices_root) as db:
        db.refresh_views()
        facts = db.query_market_observations(cutoff_utc=cutoff)
        vix_rows = db.query_vix_context(cutoff_utc=cutoff, session=session)
    cnn = None
    for row in facts.to_dict(orient="records"):
        if row["series_id"] == "CNN_FEAR_GREED":
            row = {key: row[key] for key in MarketObservation.model_fields}
            row["observation_date"] = row["observation_date"].date()
            row["quality_flags"] = tuple(row["quality_flags"])
            for key in ("observed_at_utc", "available_at_utc", "fetched_at_utc"):
                row[key] = row[key].to_pydatetime().astimezone(UTC)
            cnn = MarketObservation.model_validate(row)
    vix = None
    vix_reason = None
    if not vix_rows.empty:
        # Do not fall back to an older healthy record when the newest record is invalid.
        latest = vix_rows.tail(1)
        if not price_quality_mask(latest.assign(is_stale=False)).iloc[0]:
            vix_reason = "latest_vix_price_unreliable"
        else:
            row = latest.iloc[0].to_dict()
            origin = {
                key: str(row[key])
                for key in (
                    "symbol",
                    "session_date_ny",
                    "source",
                    "source_version",
                    "fetched_at_utc",
                    "available_at_utc",
                    "ingestion_run_id",
                    "filename",
                )
            }
            vix = MarketObservation(
                series_id="VIX",
                value=float(row["close"]),
                observed_at_utc=row["timestamp_utc"].to_pydatetime().astimezone(UTC),
                observation_date=row["session_date_ny"].date(),
                source=row["source"],
                source_version=row["source_version"] + "+" + VIX_VIEW_VERSION,
                source_url="https://finance.yahoo.com/quote/%5EVIX/",
                fetched_at_utc=row["fetched_at_utc"].to_pydatetime().astimezone(UTC),
                available_at_utc=max(row["fetched_at_utc"], row["available_at_utc"])
                .to_pydatetime()
                .astimezone(UTC),
                ingestion_run_id=row["ingestion_run_id"],
                is_stale=bool(row["is_stale"]),
                quality_flags=tuple(row["quality_flags"]),
                input_reference=json.dumps(origin, sort_keys=True),
            )
    result = {
        name: _digest_value(
            record, session=session, stale_after_sessions=config.stale_after_sessions
        )
        for name, record in (("cnn_fear_greed", cnn), ("vix", vix))
    }
    if vix_reason:
        result["vix"]["reason"] = vix_reason
    return result
