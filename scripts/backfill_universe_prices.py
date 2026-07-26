#!/usr/bin/env python
"""Plan and run bounded price backfills for configured universes.

This script intentionally reuses ``quant data update-prices`` instead of
talking to providers directly. The CLI path already owns provider metadata,
rate limits, retries, quality reports, and immutable Parquet writes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quant_system.domain.clocks import NyseSessionClock
from quant_system.ingestion.config import load_price_source_settings
from quant_system.settings import PROJECT_ROOT
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.universe.config import (
    load_raw_candidate_pool_config,
    load_watchlist_config,
)

DEFAULT_UNIVERSE_CONFIGS = (
    PROJECT_ROOT / "configs" / "universe" / "ai_watchlist.yaml",
    PROJECT_ROOT / "configs" / "universe" / "hedge_overlay.yaml",
)
DEFAULT_PRICE_CONFIG = PROJECT_ROOT / "configs" / "sources" / "prices.yaml"


@dataclass(frozen=True)
class CoverageSnapshot:
    """Point-in-time coverage for a symbol set."""

    expected_session: str
    requested_symbols: tuple[str, ...]
    missing_symbols: tuple[str, ...]
    stale_symbols: tuple[str, ...]
    up_to_date_symbols: tuple[str, ...]
    latest_sessions: dict[str, str | None]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely backfill prices for AI and hedge universes.",
    )
    parser.add_argument(
        "--universe-config",
        action="append",
        type=Path,
        default=[],
        help="Watchlist/overlay YAML to include. Defaults to AI watchlist and hedge overlay.",
    )
    parser.add_argument(
        "--raw-candidates",
        type=Path,
        default=None,
        help="Optional raw candidate pool YAML to include by suggested_action.",
    )
    parser.add_argument(
        "--raw-action",
        action="append",
        default=[],
        help="suggested_action value to include from --raw-candidates; may repeat.",
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="Extra comma-separated symbols to include.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=20,
        help="Outer symbols per quant update-prices invocation.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=45,
        help="Pause between chunks when --execute is set.",
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="Stop after this many chunks; useful for resuming gently.",
    )
    parser.add_argument(
        "--price-config",
        type=Path,
        default=DEFAULT_PRICE_CONFIG,
        help="Price-source config passed to quant data update-prices.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=PROJECT_ROOT / "data",
        help="Runtime data directory.",
    )
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "reports" / "backfill",
        help="Directory for the backfill manifest JSON.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run update-prices. Omit for dry-run planning.",
    )
    return parser.parse_args()


def collect_symbols(
    *,
    universe_configs: tuple[Path, ...],
    raw_candidates: Path | None,
    raw_actions: tuple[str, ...],
    extra_symbols: str,
) -> tuple[str, ...]:
    symbols: list[str] = []
    for config_path in universe_configs:
        watchlist = load_watchlist_config(config_path)
        symbols.extend(watchlist.member_symbols)
        symbols.extend(watchlist.benchmark_symbols)
    if raw_candidates is not None and raw_actions:
        pool = load_raw_candidate_pool_config(raw_candidates)
        wanted = set(raw_actions)
        symbols.extend(
            member.symbol for member in pool.symbols if member.suggested_action in wanted
        )
    symbols.extend(
        part.strip().upper().replace(".", "-")
        for part in extra_symbols.split(",")
        if part.strip()
    )
    return tuple(sorted(dict.fromkeys(symbols)))


def inspect_coverage(
    *,
    data_dir: Path,
    symbols: tuple[str, ...],
    now_utc: datetime | None = None,
) -> CoverageSnapshot:
    repository = ParquetRepository(data_dir)
    expected_session = NyseSessionClock().latest_completed_session(
        now_utc or datetime.now(UTC)
    )
    database_path = data_dir / "db" / "analytics.duckdb"
    with DuckDBAnalytics(database_path, repository.daily_prices_root) as analytics:
        analytics.refresh_views()
        latest = analytics.latest_sessions(symbols)
    missing = tuple(symbol for symbol in symbols if symbol not in latest)
    stale = tuple(
        symbol for symbol in symbols if symbol in latest and latest[symbol] < expected_session
    )
    up_to_date = tuple(
        symbol for symbol in symbols if symbol in latest and latest[symbol] >= expected_session
    )
    return CoverageSnapshot(
        expected_session=expected_session.isoformat(),
        requested_symbols=symbols,
        missing_symbols=missing,
        stale_symbols=stale,
        up_to_date_symbols=up_to_date,
        latest_sessions={
            symbol: latest[symbol].isoformat() if symbol in latest else None
            for symbol in symbols
        },
    )


def build_chunks(
    snapshot: CoverageSnapshot,
    *,
    chunk_size: int,
) -> tuple[tuple[str, ...], ...]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    # Missing symbols first so unavailable or renamed tickers are discovered early.
    lagging = tuple(
        dict.fromkeys((*snapshot.missing_symbols, *snapshot.stale_symbols))
    )
    return tuple(
        lagging[offset : offset + chunk_size]
        for offset in range(0, len(lagging), chunk_size)
    )


def estimate_runtime_seconds(
    *,
    chunk_count: int,
    sleep_seconds: float,
    seconds_per_chunk: float = 30.0,
) -> float:
    if chunk_count == 0:
        return 0.0
    return chunk_count * seconds_per_chunk + max(0, chunk_count - 1) * sleep_seconds


def run_chunks(
    *,
    chunks: tuple[tuple[str, ...], ...],
    sleep_seconds: float,
    max_chunks: int | None,
    price_config: Path,
    execute: bool,
) -> list[dict[str, Any]]:
    selected = chunks[:max_chunks] if max_chunks is not None else chunks
    results: list[dict[str, Any]] = []
    for index, chunk in enumerate(selected, start=1):
        command = [
            sys.executable,
            "-m",
            "quant_system",
            "data",
            "update-prices",
            "--symbols",
            ",".join(chunk),
            "--config",
            str(price_config),
        ]
        started = datetime.now(UTC)
        result: dict[str, Any] = {
            "chunk_number": index,
            "symbol_count": len(chunk),
            "symbols": list(chunk),
            "command": command,
            "started_at_utc": started.isoformat(),
            "executed": execute,
        }
        if execute:
            completed = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            result.update(
                {
                    "returncode": completed.returncode,
                    "stdout_tail": completed.stdout[-4000:],
                    "stderr_tail": completed.stderr[-4000:],
                }
            )
        result["completed_at_utc"] = datetime.now(UTC).isoformat()
        results.append(result)
        if execute and index < len(selected) and sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return results


def write_manifest(
    *,
    manifest_dir: Path,
    payload: dict[str, Any],
) -> Path:
    manifest_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = manifest_dir / f"price-backfill-{run_id}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def main() -> None:
    args = parse_args()
    if args.chunk_size < 1:
        raise SystemExit("--chunk-size must be positive")
    if args.sleep_seconds < 0:
        raise SystemExit("--sleep-seconds must be non-negative")

    universe_configs = tuple(args.universe_config) or DEFAULT_UNIVERSE_CONFIGS
    source_settings = load_price_source_settings(args.price_config)
    symbols = collect_symbols(
        universe_configs=tuple(path.resolve() for path in universe_configs),
        raw_candidates=args.raw_candidates.resolve() if args.raw_candidates else None,
        raw_actions=tuple(args.raw_action),
        extra_symbols=args.symbols,
    )
    snapshot = inspect_coverage(data_dir=args.data_dir, symbols=symbols)
    chunks = build_chunks(snapshot, chunk_size=args.chunk_size)
    estimated_seconds = estimate_runtime_seconds(
        chunk_count=len(chunks),
        sleep_seconds=args.sleep_seconds,
    )
    chunk_results = run_chunks(
        chunks=chunks,
        sleep_seconds=args.sleep_seconds,
        max_chunks=args.max_chunks,
        price_config=args.price_config,
        execute=args.execute,
    )
    payload = {
        "mode": "execute" if args.execute else "dry_run",
        "provider": source_settings.provider,
        "configured_calls_per_minute": source_settings.calls_per_minute,
        "configured_daily_call_budget": source_settings.daily_call_budget,
        "outer_chunk_size": args.chunk_size,
        "outer_sleep_seconds": args.sleep_seconds,
        "expected_session": snapshot.expected_session,
        "requested_symbol_count": len(snapshot.requested_symbols),
        "missing_symbol_count": len(snapshot.missing_symbols),
        "stale_symbol_count": len(snapshot.stale_symbols),
        "up_to_date_symbol_count": len(snapshot.up_to_date_symbols),
        "planned_chunk_count": len(chunks),
        "selected_chunk_count": len(chunk_results),
        "estimated_runtime_minutes": round(estimated_seconds / 60, 1),
        "missing_symbols": list(snapshot.missing_symbols),
        "stale_symbols": list(snapshot.stale_symbols),
        "up_to_date_symbols": list(snapshot.up_to_date_symbols),
        "latest_sessions": snapshot.latest_sessions,
        "chunks": [list(chunk) for chunk in chunks],
        "chunk_results": chunk_results,
    }
    manifest_path = write_manifest(manifest_dir=args.manifest_dir, payload=payload)
    print(
        json.dumps(
            {
                "mode": payload["mode"],
                "manifest_path": str(manifest_path),
                "requested_symbol_count": payload["requested_symbol_count"],
                "missing_symbol_count": payload["missing_symbol_count"],
                "stale_symbol_count": payload["stale_symbol_count"],
                "planned_chunk_count": payload["planned_chunk_count"],
                "selected_chunk_count": payload["selected_chunk_count"],
                "estimated_runtime_minutes": payload["estimated_runtime_minutes"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
