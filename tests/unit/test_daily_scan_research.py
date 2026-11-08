from datetime import date

import pandas as pd
import pytest

from quant_system.backtest.daily_scan import (
    forward_outcomes,
    research_statistics,
    visible_snapshot,
)
from quant_system.domain.clocks import NyseSessionClock


def test_snapshot_filters_before_dedup_and_never_uses_future_bars():
    versions = pd.DataFrame(
        [
            {
                "symbol": "A",
                "session_date_ny": "2026-01-02",
                "close": 100,
                "available_at_utc": "2026-01-02T21:30:00Z",
                "fetched_at_utc": "2026-01-03T00:00:00Z",
            },
            {
                "symbol": "A",
                "session_date_ny": "2026-01-02",
                "close": 150,
                "available_at_utc": "2026-01-06T21:30:00Z",
                "fetched_at_utc": "2026-01-07T00:00:00Z",
            },
            {
                "symbol": "A",
                "session_date_ny": "2026-01-07",
                "close": 200,
                "available_at_utc": "2026-01-02T21:30:00Z",
                "fetched_at_utc": "2026-01-03T00:00:00Z",
            },
        ]
    )
    early = visible_snapshot(versions, date(2026, 1, 5))
    assert early.close.tolist() == [100]
    assert visible_snapshot(versions, date(2026, 1, 6)).close.tolist() == [150]


def outcome_prices():
    clock = NyseSessionClock()
    sessions = clock.calendar.sessions_in_range("2026-01-16", "2026-02-10")
    return pd.DataFrame(
        [
            {
                "symbol": "A",
                "session_date_ny": s,
                "open": 100,
                "close": 101 + i,
                "high": 130,
                "low": 99,
                "volume": 10,
                "price_quality_pass": True,
            }
            for i, s in enumerate(sessions)
        ]
    )


def test_outcomes_use_next_open_and_exchange_horizons_not_row_offsets():
    result = forward_outcomes(
        outcome_prices(), symbol="A", session=date(2026, 1, 16), end=date(2026, 2, 10)
    )
    # Monday Jan 19 is MLK day; the fifth future session is Monday Jan 26.
    assert result["outcome_reference_open_session"] == "2026-01-20"
    assert result["outcome_5d_end"] == "2026-01-26"
    assert result["forward_return_5d"] == pytest.approx(0.06)


@pytest.mark.parametrize(
    "mode,expected", [("missing", "MISSING_PRICE"), ("stale", "UNRELIABLE_PRICE")]
)
def test_missing_intermediate_bar_never_compresses_horizon(mode, expected):
    prices = outcome_prices()
    if mode == "missing":
        prices = prices.drop(index=2)
    else:
        prices.loc[2, "price_quality_pass"] = False
    result = forward_outcomes(prices, symbol="A", session=date(2026, 1, 16), end=date(2026, 2, 10))
    assert result["outcome_5d_status"] == expected
    assert result["forward_return_5d"] is None


def test_tail_is_unmatured_even_if_caller_supplies_later_prices():
    result = forward_outcomes(
        outcome_prices(), symbol="A", session=date(2026, 1, 16), end=date(2026, 1, 23)
    )
    assert result["outcome_5d_status"] == "UNMATURED"
    assert result["forward_return_5d"] is None


def test_membership_statistics_are_separate_and_empty_labels_are_null():
    signals = pd.DataFrame(
        [
            {
                "symbol": "A",
                "current_member": True,
                "historical_member": False,
                "forward_return_1d": 0.10,
            },
            {
                "symbol": "B",
                "current_member": True,
                "historical_member": True,
                "forward_return_1d": -0.05,
            },
        ]
    )
    retrospective = research_statistics(signals, "current_member")
    historical = research_statistics(signals, "historical_member")
    assert retrospective["signal_count"] == 2
    assert historical["signal_count"] == 1
    assert retrospective["horizons"]["1"]["mean_return"] == pytest.approx(0.025)
    assert historical["horizons"]["5"]["mean_return"] is None
