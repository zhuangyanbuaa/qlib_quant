from datetime import date
from uuid import uuid4

import pandas as pd
import pytest

from quant_system.models.labels import RelativeReturnLabelConfig, build_relative_return_labels


def price_frame() -> pd.DataFrame:
    sessions = pd.to_datetime(
        [
            "2026-01-02",
            "2026-01-05",
            "2026-01-06",
            "2026-01-07",
            "2026-01-08",
        ]
    )
    rows = []
    for symbol, closes in {
        "AAPL": [100.0, 110.0, 121.0, 120.0, 125.0],
        "QQQ": [200.0, 210.0, 210.0, 220.0, 225.0],
    }.items():
        rows.extend(
            {
                "symbol": symbol,
                "session_date_ny": session,
                "close": close,
            }
            for session, close in zip(sessions, closes, strict=True)
        )
    return pd.DataFrame(rows)


def signal_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "signal_id": str(uuid4()),
                "symbol": "AAPL",
                "signal_session": date(2026, 1, 2),
                "earliest_order_session": date(2026, 1, 5),
            },
            {
                "signal_id": str(uuid4()),
                "symbol": "AAPL",
                "signal_session": date(2026, 1, 7),
                "earliest_order_session": date(2026, 1, 8),
            },
        ]
    )


def test_relative_return_label_starts_at_earliest_order_session_not_signal_close() -> None:
    labels = build_relative_return_labels(
        signal_frame().iloc[:1],
        price_frame(),
        config=RelativeReturnLabelConfig(holding_sessions=(1,), benchmark_symbol="QQQ"),
    )

    row = labels.iloc[0]

    assert row["entry_session"] == date(2026, 1, 5)
    assert row["exit_session_1"] == date(2026, 1, 6)
    assert row["stock_return_1"] == pytest.approx(0.10)
    assert row["benchmark_return_1"] == 0.0
    assert row["relative_return_1"] == pytest.approx(0.10)


def test_unmatured_future_return_is_nan() -> None:
    labels = build_relative_return_labels(
        signal_frame().iloc[1:],
        price_frame(),
        config=RelativeReturnLabelConfig(holding_sessions=(2,), benchmark_symbol="QQQ"),
    )

    row = labels.iloc[0]

    assert pd.isna(row["exit_session_2"])
    assert pd.isna(row["relative_return_2"])
