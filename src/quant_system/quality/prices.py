"""Shared daily price gates for historical and current decisions."""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from quant_system.domain.clocks import NyseSessionClock

CORE_PRICE_SYMBOLS = ("SPY", "QQQ")


class PriceDataBlocked(RuntimeError):
    """Critical price failure with a machine-readable downstream gate."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        super().__init__("; ".join(report["blocked_reasons"]))


def price_quality_mask(frame: pd.DataFrame) -> pd.Series:
    """Check bar integrity and explicit stale/blocked metadata, retaining lineage."""
    valid = pd.Series(True, index=frame.index)
    if "is_stale" in frame:
        valid &= ~frame["is_stale"].fillna(True).astype(bool)
    if "price_quality_pass" in frame:
        valid &= frame["price_quality_pass"].fillna(False).astype(bool)
    columns = [name for name in ("open", "high", "low", "close", "volume") if name in frame]
    if columns:
        values = frame[columns].apply(pd.to_numeric, errors="coerce")
        valid &= pd.Series(np.isfinite(values).all(axis=1), index=frame.index)
        for name in ("open", "high", "low", "close"):
            if name in values:
                valid &= values[name] > 0
        if "volume" in values:
            valid &= values["volume"] >= 0
        if {"open", "high", "low", "close"} <= set(values):
            valid &= values["low"] <= values[["open", "close"]].min(axis=1)
            valid &= values["high"] >= values[["open", "close"]].max(axis=1)
            valid &= values["low"] <= values["high"]
    if "quality_flags" in frame:
        valid &= frame["quality_flags"].map(
            lambda flags: (
                not any(
                    str(flag) == "quality_blocked" or str(flag).startswith("invalid_")
                    for flag in flags
                )
            )
        )
    return valid


def critical_symbols(benchmark_symbol: str) -> tuple[str, ...]:
    return tuple(sorted({*CORE_PRICE_SYMBOLS, benchmark_symbol.upper()}))


def require_critical_prices(
    frame: pd.DataFrame,
    *,
    session: date,
    benchmark_symbol: str = "QQQ",
    clock: NyseSessionClock | None = None,
) -> dict[str, Any]:
    """Block when a critical symbol lacks a reliable, visible bar for the session."""
    clock = clock or NyseSessionClock()
    cutoff = clock.available_at_utc(session)
    symbols = critical_symbols(benchmark_symbol)
    reasons: list[str] = []
    latest: dict[str, str | None] = {}
    for symbol in symbols:
        rows = frame.loc[frame["symbol"] == symbol] if "symbol" in frame else pd.DataFrame()
        if not rows.empty and "available_at_utc" in rows:
            rows = rows.loc[pd.to_datetime(rows["available_at_utc"], utc=True) <= cutoff]
        latest[symbol] = (
            str(pd.to_datetime(rows["session_date_ny"]).max().date()) if not rows.empty else None
        )
        today = (
            rows.loc[pd.to_datetime(rows["session_date_ny"]).dt.date == session]
            if not rows.empty
            else rows
        )
        if today.empty:
            reasons.append(f"critical_price_not_current:{symbol}:{session}")
        elif not price_quality_mask(today).all():
            reasons.append(f"critical_price_unreliable:{symbol}:{session}")
    report = {
        "status": "BLOCKED" if reasons else "SUCCESS",
        "signal_session": session.isoformat(),
        "data_cutoff_utc": cutoff.isoformat(),
        "critical_symbols": list(symbols),
        "latest_sessions": latest,
        "blocked_reasons": reasons,
    }
    if reasons:
        raise PriceDataBlocked(report)
    return report
