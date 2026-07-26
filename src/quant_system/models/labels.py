"""Point-in-time labels for candidate-level model ranking."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class RelativeReturnLabelConfig:
    """Future relative-return label settings."""

    holding_sessions: tuple[int, ...] = (5, 10)
    benchmark_symbol: str = "QQQ"

    def __post_init__(self) -> None:
        if not self.holding_sessions:
            raise ValueError("at least one holding session is required")
        if min(self.holding_sessions) < 1:
            raise ValueError("holding sessions must be positive")


def build_relative_return_labels(
    signals: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    config: RelativeReturnLabelConfig,
) -> pd.DataFrame:
    """Attach future relative returns without filling immature labels."""
    required_signal_columns = {"signal_id", "symbol", "signal_session", "earliest_order_session"}
    missing_signal = required_signal_columns - set(signals.columns)
    if missing_signal:
        raise ValueError(f"signals missing required columns: {sorted(missing_signal)}")
    required_price_columns = {"symbol", "session_date_ny", "close"}
    missing_price = required_price_columns - set(prices.columns)
    if missing_price:
        raise ValueError(f"prices missing required columns: {sorted(missing_price)}")

    normalized_prices = _normalize_prices(prices)
    symbol_prices = {
        symbol: group.sort_values("session_date_ny").reset_index(drop=True)
        for symbol, group in normalized_prices.groupby("symbol", sort=False)
    }
    benchmark = symbol_prices.get(config.benchmark_symbol.upper())
    if benchmark is None:
        raise ValueError(f"benchmark missing from prices: {config.benchmark_symbol}")

    rows: list[dict[str, object]] = []
    for signal in signals.to_dict("records"):
        symbol = str(signal["symbol"]).upper()
        symbol_frame = symbol_prices.get(symbol)
        entry_session = _to_date(signal["earliest_order_session"])
        row: dict[str, object] = {
            "signal_id": str(signal["signal_id"]),
            "symbol": symbol,
            "signal_session": _to_date(signal["signal_session"]),
            "entry_session": entry_session,
        }
        for holding_session in config.holding_sessions:
            row.update(
                _label_for_horizon(
                    symbol_frame=symbol_frame,
                    benchmark_frame=benchmark,
                    entry_session=entry_session,
                    holding_session=holding_session,
                )
            )
        rows.append(row)
    return pd.DataFrame(rows)


def label_columns(holding_sessions: Iterable[int]) -> tuple[str, ...]:
    """Return canonical relative-return label columns for horizons."""
    return tuple(f"relative_return_{horizon}" for horizon in holding_sessions)


def _label_for_horizon(
    *,
    symbol_frame: pd.DataFrame | None,
    benchmark_frame: pd.DataFrame,
    entry_session: date,
    holding_session: int,
) -> dict[str, object]:
    suffix = str(holding_session)
    empty = {
        f"exit_session_{suffix}": pd.NA,
        f"stock_return_{suffix}": pd.NA,
        f"benchmark_return_{suffix}": pd.NA,
        f"relative_return_{suffix}": pd.NA,
        f"label_end_session_{suffix}": pd.NA,
    }
    if symbol_frame is None:
        return empty
    stock_entry_index = _row_index(symbol_frame, entry_session)
    benchmark_entry_index = _row_index(benchmark_frame, entry_session)
    if stock_entry_index is None or benchmark_entry_index is None:
        return empty
    stock_exit_index = stock_entry_index + holding_session
    benchmark_exit_index = benchmark_entry_index + holding_session
    if stock_exit_index >= len(symbol_frame) or benchmark_exit_index >= len(benchmark_frame):
        return empty

    stock_entry = float(symbol_frame.iloc[stock_entry_index]["close"])
    stock_exit = float(symbol_frame.iloc[stock_exit_index]["close"])
    benchmark_entry = float(benchmark_frame.iloc[benchmark_entry_index]["close"])
    benchmark_exit = float(benchmark_frame.iloc[benchmark_exit_index]["close"])
    stock_return = stock_exit / stock_entry - 1
    benchmark_return = benchmark_exit / benchmark_entry - 1
    exit_session = symbol_frame.iloc[stock_exit_index]["session_date_ny"]
    return {
        f"exit_session_{suffix}": exit_session,
        f"stock_return_{suffix}": stock_return,
        f"benchmark_return_{suffix}": benchmark_return,
        f"relative_return_{suffix}": stock_return - benchmark_return,
        f"label_end_session_{suffix}": exit_session,
    }


def _normalize_prices(prices: pd.DataFrame) -> pd.DataFrame:
    frame = prices.loc[:, ["symbol", "session_date_ny", "close"]].copy()
    frame["symbol"] = frame["symbol"].astype("string").str.upper()
    frame["session_date_ny"] = pd.to_datetime(frame["session_date_ny"]).dt.date
    return frame.sort_values(["symbol", "session_date_ny"]).reset_index(drop=True)


def _row_index(frame: pd.DataFrame, session: date) -> int | None:
    matches = frame.index[frame["session_date_ny"] == session].tolist()
    if not matches:
        return None
    return int(matches[0])


def _to_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()
