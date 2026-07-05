from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from quant_system.domain.enums import MarketRegime
from quant_system.domain.trading import CandidateSignal


def test_signal_requires_order_strictly_after_signal() -> None:
    with pytest.raises(ValidationError, match="strictly after"):
        CandidateSignal(
            symbol="AAPL",
            signal_session=date(2026, 6, 26),
            data_cutoff_utc=datetime(2026, 6, 26, 20, tzinfo=UTC),
            signal_time_utc=datetime(2026, 6, 26, 20, 30, tzinfo=UTC),
            earliest_order_session=date(2026, 6, 29),
            earliest_order_time_utc=datetime(2026, 6, 26, 20, 20, tzinfo=UTC),
            score=0.1,
            signal_close=100,
            atr20=5,
            market_regime=MarketRegime.GREEN,
            reasons=("test",),
        )
