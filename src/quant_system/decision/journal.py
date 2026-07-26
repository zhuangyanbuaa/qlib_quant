"""Manual trade journal helpers built on SQLite operations storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class OpenPosition:
    """A long-only open position reconstructed from manual fills."""

    symbol: str
    quantity: int
    average_entry_price: float
    cost_basis: float
    entry_time_utc: datetime
    last_fill_time_utc: datetime
    buy_commission: float
    realized_sell_commission: float
    signal_id: str | None = None
    stop_price: float | None = None
    target_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["entry_time_utc"] = self.entry_time_utc.isoformat()
        payload["last_fill_time_utc"] = self.last_fill_time_utc.isoformat()
        return payload


def reconstruct_open_positions(fills: list[dict[str, Any]]) -> list[OpenPosition]:
    """Reconstruct long-only open positions from manual BUY/SELL fills."""
    states: dict[str, dict[str, Any]] = {}
    ordered = sorted(fills, key=lambda row: (row["fill_time_utc"], row["created_at_utc"]))
    for row in ordered:
        symbol = str(row["symbol"]).upper()
        side = str(row["side"]).upper()
        quantity = int(row["quantity"])
        price = float(row["price"])
        commission = float(row["commission"])
        fill_time = _parse_utc(str(row["fill_time_utc"]))
        state = states.setdefault(
            symbol,
            {
                "quantity": 0,
                "cost_basis": 0.0,
                "entry_time_utc": fill_time,
                "last_fill_time_utc": fill_time,
                "buy_commission": 0.0,
                "realized_sell_commission": 0.0,
                "signal_id": None,
                "stop_price": None,
                "target_price": None,
            },
        )
        if side == "BUY":
            if state["quantity"] == 0:
                state["entry_time_utc"] = fill_time
                state["cost_basis"] = 0.0
                state["buy_commission"] = 0.0
                state["realized_sell_commission"] = 0.0
            state["quantity"] += quantity
            state["cost_basis"] += quantity * price
            state["buy_commission"] += commission
            state["signal_id"] = state["signal_id"] or row.get("signal_id")
            state["stop_price"] = row.get("stop_price") or state["stop_price"]
            state["target_price"] = row.get("target_price") or state["target_price"]
        elif side == "SELL":
            if quantity > state["quantity"]:
                raise ValueError(f"SELL quantity exceeds open position for {symbol}")
            average_cost = state["cost_basis"] / state["quantity"]
            state["quantity"] -= quantity
            state["cost_basis"] -= average_cost * quantity
            state["realized_sell_commission"] += commission
            if state["quantity"] == 0:
                state["cost_basis"] = 0.0
        else:
            raise ValueError(f"unsupported fill side: {side}")
        state["last_fill_time_utc"] = fill_time

    positions: list[OpenPosition] = []
    for symbol, state in sorted(states.items()):
        if state["quantity"] <= 0:
            continue
        positions.append(
            OpenPosition(
                symbol=symbol,
                quantity=int(state["quantity"]),
                average_entry_price=float(state["cost_basis"] / state["quantity"]),
                cost_basis=float(state["cost_basis"]),
                entry_time_utc=state["entry_time_utc"],
                last_fill_time_utc=state["last_fill_time_utc"],
                buy_commission=float(state["buy_commission"]),
                realized_sell_commission=float(state["realized_sell_commission"]),
                signal_id=state["signal_id"],
                stop_price=state["stop_price"],
                target_price=state["target_price"],
            )
        )
    return positions


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)

