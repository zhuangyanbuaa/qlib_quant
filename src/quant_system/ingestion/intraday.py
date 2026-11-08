"""Forward-only broker imports and bounded Alpaca trade observations."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field

from quant_system.domain.snapshots import IntradaySnapshot
from quant_system.ingestion.budgets import SQLiteDailyCallBudget, SQLiteMinuteRateLimit
from quant_system.ingestion.reliability import ProviderGuard, RetryPolicy
from quant_system.settings import PROJECT_ROOT


class IntradaySourceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["none", "broker_file", "alpaca"] = "none"
    broker_path: Path = Path("data/inbox/intraday.json")
    feed: Literal["iex", "sip"] = "iex"
    batch_size: int = Field(default=25, ge=1, le=100)
    calls_per_minute: int = Field(default=10, ge=1, le=60)
    daily_call_budget: int = Field(default=20, ge=1, le=100)
    timeout_seconds: float = Field(default=15, gt=0, le=30)
    max_quote_age_seconds: int = Field(default=120, ge=1, le=300)


def load_intraday_settings(path):
    return IntradaySourceSettings.model_validate(yaml.safe_load(Path(path).read_text()))


class IntradayUnavailable(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def alpaca_transport(symbols, feed, settings, timeout):
    url = "https://data.alpaca.markets/v2/stocks/trades/latest?" + urlencode(
        {"symbols": ",".join(symbols), "feed": feed}
    )
    request = Request(
        url,
        headers={
            "APCA-API-KEY-ID": settings.alpaca_api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": settings.alpaca_secret_key.get_secret_value(),
            "Accept": "application/json",
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise IntradayUnavailable("alpaca_response_too_large")
        return json.loads(raw)
    except HTTPError as error:
        raise IntradayUnavailable(f"alpaca_http_{error.code}") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise IntradayUnavailable("alpaca_transport_or_response_invalid") from None


def parse_alpaca_trades(payload, *, symbols, session, fetched_at, run_id, config):
    if not isinstance(payload, dict) or not isinstance(payload.get("trades"), dict):
        raise IntradayUnavailable("alpaca_invalid_trade_response")
    records = []
    for symbol in symbols:
        trade = payload["trades"].get(symbol)
        if trade is None:
            continue
        try:
            quote = datetime.fromisoformat(trade["t"].replace("Z", "+00:00"))
            if quote.utcoffset() is None:
                raise ValueError("missing_quote_timezone")
            quote = quote.astimezone(UTC)
            flags = ["trade_only_liquidity_unverified", "news_unverified"]
            if config.feed == "iex":
                flags.append("partial_exchange_coverage")
            if (
                not isinstance(trade.get("s"), int)
                or isinstance(trade.get("s"), bool)
                or trade["s"] <= 0
            ):
                raise ValueError("invalid_trade_size")
            record = IntradaySnapshot(
                symbol=symbol,
                last_price=trade["p"],
                quote_at_utc=quote,
                session_date_ny=session,
                source=f"alpaca_{config.feed}",
                source_version="latest-trades-v1",
                fetched_at_utc=fetched_at,
                available_at_utc=fetched_at,
                ingestion_run_id=run_id,
                is_stale=fetched_at - quote > timedelta(seconds=config.max_quote_age_seconds),
                quality_flags=tuple(flags),
                news_risk="UNKNOWN",
                liquidity_status="UNKNOWN",
                note=json.dumps(
                    {
                        "trade_id": trade.get("i"),
                        "exchange": trade.get("x"),
                        "conditions": trade.get("c", []),
                    }
                ),
            )
            records.append(record)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise IntradayUnavailable("alpaca_invalid_trade_fact") from None
    return records


def collect_intraday(
    settings, config, repository, *, symbols, session, now_utc=None, transport=alpaca_transport
):
    start = now_utc or datetime.now(UTC)
    if start.utcoffset() is None:
        raise ValueError("intraday clock must be aware")
    start = start.astimezone(UTC)
    symbols = tuple(sorted(set(symbols)))
    if any(not re.fullmatch(r"[A-Z0-9.^_-]{1,32}", symbol) for symbol in symbols):
        raise ValueError("invalid_intraday_symbols")
    if config.provider == "none":
        return {
            "status": "UNAVAILABLE",
            "reason": "intraday_source_not_configured",
            "rows_written": 0,
        }
    if not symbols:
        return {"status": "SKIPPED", "reason": "no_candidate_symbols", "rows_written": 0}
    run_id = uuid4()
    records = []
    try:
        if config.provider == "broker_file":
            path = config.broker_path
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            if path.stat().st_size > 2_000_000:
                raise IntradayUnavailable("broker_snapshot_too_large")
            payload = json.loads(path.read_text())
            rows = payload.get("rows") if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                raise IntradayUnavailable("invalid_broker_rows")
            facts = [IntradaySnapshot.model_validate(row) for row in rows]
            if len({fact.symbol for fact in facts}) != len(facts):
                raise IntradayUnavailable("duplicate_broker_symbol")
            fetched = start if now_utc is not None else datetime.now(UTC)
            for fact in facts:
                if fact.symbol not in symbols:
                    continue
                if (
                    fact.session_date_ny != session
                    or max(fact.quote_at_utc, fact.fetched_at_utc, fact.available_at_utc) > fetched
                ):
                    raise IntradayUnavailable("broker_time_or_session_mismatch")
                origin = fact.model_dump(mode="json")
                records.append(
                    fact.model_copy(
                        update={
                            "fetched_at_utc": fetched,
                            "available_at_utc": fetched,
                            "ingestion_run_id": run_id,
                            "source_version": fact.source_version + "+broker-import-v1",
                            "is_stale": fact.is_stale
                            or fetched - fact.quote_at_utc
                            > timedelta(seconds=config.max_quote_age_seconds),
                            "note": json.dumps({"origin": origin}, ensure_ascii=False),
                        }
                    )
                )
            paths = repository.write_intraday_snapshots(records, run_id=run_id) if records else []
        else:
            paths = []
            if (
                not settings.alpaca_api_key
                or not settings.alpaca_secret_key
                or not settings.alpaca_api_key.get_secret_value()
                or not settings.alpaca_secret_key.get_secret_value()
            ):
                raise IntradayUnavailable("alpaca_credentials_missing")
            guard = ProviderGuard(
                budget=SQLiteDailyCallBudget(
                    config.daily_call_budget,
                    database_path=settings.resolved_data_dir / "db/operations.sqlite",
                    provider="alpaca_intraday",
                ),
                limiter=SQLiteMinuteRateLimit(
                    config.calls_per_minute,
                    database_path=settings.resolved_data_dir / "db/operations.sqlite",
                    provider="alpaca_intraday",
                ),
                retry=RetryPolicy(attempts=1),
            )
            for offset in range(0, len(symbols), config.batch_size):
                batch = symbols[offset : offset + config.batch_size]
                payload = guard.call(
                    lambda batch=batch: transport(
                        batch, config.feed, settings, config.timeout_seconds
                    )
                )
                fetched = start if now_utc is not None else datetime.now(UTC)
                batch_records = parse_alpaca_trades(
                    payload,
                    symbols=batch,
                    session=session,
                    fetched_at=fetched,
                    run_id=run_id,
                    config=config,
                )
                if batch_records:
                    paths.extend(
                        repository.write_intraday_snapshots(
                            batch_records, run_id=run_id, batch_id=f"batch-{offset}"
                        )
                    )
                    records.extend(batch_records)
        return {
            "status": "SUCCESS"
            if len(records) == len(symbols) and all(not r.is_stale for r in records)
            else "DEGRADED",
            "rows_written": len(records),
            "requested_symbol_count": len(symbols),
            "paths": [str(p) for p in paths],
        }
    except Exception as error:
        # Public errors are controlled codes; exceptions may contain account secrets.
        reason = str(error) if isinstance(error, IntradayUnavailable) else type(error).__name__
        return {
            "status": "DEGRADED" if config.provider == "alpaca" and records else "UNAVAILABLE",
            "reason": reason,
            "rows_written": len(records) if config.provider == "alpaca" else 0,
        }


def select_intraday(repository, *, session, cutoff):
    rows = repository.read_intraday_snapshots().to_pylist()
    selected = {}
    for row in sorted(rows, key=lambda row: (row["quote_at_utc"], row["available_at_utc"])):
        if (
            row["session_date_ny"] == session
            and max(row["quote_at_utc"], row["available_at_utc"], row["fetched_at_utc"]) <= cutoff
        ):
            selected[row["symbol"]] = row
    return list(selected.values())
