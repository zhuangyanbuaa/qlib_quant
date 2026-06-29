from datetime import UTC, date, datetime

import pytest

from quant_system.domain.clocks import NyseSessionClock


def test_latest_completed_session_before_and_after_safety_delay() -> None:
    clock = NyseSessionClock()

    assert clock.latest_completed_session(datetime(2026, 6, 29, 20, 15, tzinfo=UTC)) == date(
        2026, 6, 26
    )
    assert clock.latest_completed_session(datetime(2026, 6, 29, 20, 31, tzinfo=UTC)) == date(
        2026, 6, 29
    )


def test_latest_completed_session_on_weekend() -> None:
    clock = NyseSessionClock()

    assert clock.latest_completed_session(datetime(2026, 6, 28, 12, tzinfo=UTC)) == date(
        2026, 6, 26
    )


def test_clock_rejects_non_utc_input() -> None:
    with pytest.raises(ValueError, match="UTC"):
        NyseSessionClock().latest_completed_session(datetime(2026, 6, 29, 20, 31))
