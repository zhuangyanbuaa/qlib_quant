"""Stable domain enums used in signals, fills, and reports."""

from enum import StrEnum


class MarketRegime(StrEnum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    PROFIT_TARGET = "PROFIT_TARGET"
    TIME_EXIT = "TIME_EXIT"
