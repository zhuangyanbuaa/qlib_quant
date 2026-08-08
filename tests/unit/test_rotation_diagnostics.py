from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from quant_system.decision.rotation import (
    RotationMember,
    compute_pair_spreads,
    compute_rotation_rows,
    load_rotation_members,
    rotation_posture,
)


def test_compute_rotation_rows_identifies_leading_and_improving_groups() -> None:
    as_of = date(2026, 4, 1)
    sessions = pd.bdate_range("2026-01-07", periods=61).date
    members = (
        RotationMember(
            symbol="HARD1",
            universe_role="ai_alpha",
            sector="Information Technology",
            theme="ai_chips",
            subtheme="accelerators",
            benchmark_etf="QQQ",
        ),
        RotationMember(
            symbol="HARD2",
            universe_role="ai_alpha",
            sector="Information Technology",
            theme="ai_chips",
            subtheme="accelerators",
            benchmark_etf="QQQ",
        ),
        RotationMember(
            symbol="SOFT1",
            universe_role="ai_alpha",
            sector="Information Technology",
            theme="ai_platform_software",
            subtheme="application_software",
            benchmark_etf="QQQ",
        ),
        RotationMember(
            symbol="HEDGE1",
            universe_role="hedge_overlay",
            sector="Consumer Staples",
            theme="defensive_staples",
            subtheme="consumer_staples_quality",
            benchmark_etf="QQQ",
        ),
    )
    prices = _price_frame(
        sessions,
        {
            "QQQ": _piecewise_close(100, 100, 100),
            "HARD1": _piecewise_close(100, 130, 160),
            "HARD2": _piecewise_close(100, 120, 150),
            "SOFT1": _piecewise_close(100, 80, 90),
            "HEDGE1": _piecewise_close(100, 95, 90),
        },
    )

    rows = compute_rotation_rows(
        prices=prices,
        members=members,
        as_of=as_of,
        periods=(20, 60),
        default_benchmark_symbol="QQQ",
    )

    ai_chips = _find_row(rows, "theme", "ai_chips")
    software = _find_row(rows, "theme", "ai_platform_software")
    hedge = _find_row(rows, "universe_role", "hedge_overlay")

    assert ai_chips["rotation_status"] == "LEADING"
    assert ai_chips["valid_20d"] == 2
    assert ai_chips["valid_60d"] == 2
    assert software["rotation_status"] == "IMPROVING"
    assert hedge["rotation_status"] == "LAGGING"


def test_pair_spreads_and_posture_flag_ai_leadership() -> None:
    as_of = date(2026, 4, 1)
    sessions = pd.bdate_range("2026-01-07", periods=61).date
    members = (
        RotationMember(
            symbol="NVDA",
            universe_role="ai_alpha",
            sector="Information Technology",
            theme="ai_chips",
            subtheme="accelerators",
            benchmark_etf="QQQ",
        ),
        RotationMember(
            symbol="COST",
            universe_role="hedge_overlay",
            sector="Consumer Staples",
            theme="defensive_staples",
            subtheme="consumer_staples_quality",
            benchmark_etf="QQQ",
        ),
    )
    prices = _price_frame(
        sessions,
        {
            "QQQ": _piecewise_close(100, 100, 100),
            "NVDA": _piecewise_close(100, 120, 150),
            "COST": _piecewise_close(100, 100, 95),
        },
    )
    rows = compute_rotation_rows(
        prices=prices,
        members=members,
        as_of=as_of,
        periods=(20, 60),
        default_benchmark_symbol="QQQ",
    )

    spreads = compute_pair_spreads(rows)
    posture = rotation_posture(rows, spreads)

    ai_vs_hedge = next(row for row in spreads if row["name"] == "ai_alpha_vs_hedge_overlay")
    assert ai_vs_hedge["spread_relative_20d"] > 0
    assert ai_vs_hedge["spread_relative_60d"] > 0
    assert posture["status"] == "RISK_ON_AI_LEADERSHIP"
    assert posture["leading_themes"] == ["ai_chips"]


def test_load_rotation_members_reads_current_watchlists() -> None:
    members = load_rotation_members(
        (
            Path("configs/universe/ai_watchlist.yaml"),
            Path("configs/universe/hedge_overlay.yaml"),
        )
    )

    by_symbol = {member.symbol: member for member in members}
    assert by_symbol["NVDA"].universe_role == "ai_alpha"
    assert by_symbol["NVDA"].theme == "ai_chips"
    assert by_symbol["COST"].universe_role == "hedge_overlay"
    assert by_symbol["COST"].theme == "defensive_staples"


def _price_frame(
    sessions: pd.Index,
    closes_by_symbol: dict[str, list[float]],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for symbol, closes in closes_by_symbol.items():
        for session, close in zip(sessions, closes, strict=True):
            rows.append(
                {
                    "symbol": symbol,
                    "session_date_ny": session,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1_000_000,
                }
            )
    return pd.DataFrame(rows)


def _piecewise_close(start: float, day_40: float, day_60: float) -> list[float]:
    first = [
        start + (day_40 - start) * index / 40
        for index in range(41)
    ]
    second = [
        day_40 + (day_60 - day_40) * index / 20
        for index in range(1, 21)
    ]
    return first + second


def _find_row(
    rows: list[dict[str, object]],
    group_type: str,
    group: str,
) -> dict[str, object]:
    return next(
        row for row in rows if row["group_type"] == group_type and row["group"] == group
    )
