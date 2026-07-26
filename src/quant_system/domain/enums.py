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


class EventSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class NewsEventType(StrEnum):
    ANALYST_RATING = "ANALYST_RATING"
    EARNINGS = "EARNINGS"
    GUIDANCE = "GUIDANCE"
    M_AND_A = "M_AND_A"
    LEGAL_REGULATORY = "LEGAL_REGULATORY"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    FINANCING = "FINANCING"
    PRODUCT = "PRODUCT"
    MACRO = "MACRO"
    SEC_FILING = "SEC_FILING"
    OTHER = "OTHER"
