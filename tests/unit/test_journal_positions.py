from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from quant_system.decision.journal import OpenPosition, reconstruct_open_positions
from quant_system.decision.positions import evaluate_position
from quant_system.storage.sqlite import OperationsRegistry
from quant_system.strategy.config import load_buy_the_dip_config


def test_operations_registry_records_manual_fill_and_reconstructs_position(tmp_path) -> None:
    database_path = tmp_path / "operations.sqlite"
    with OperationsRegistry(database_path) as registry:
        registry.record_manual_fill(
            symbol="nvda",
            side="BUY",
            quantity=10,
            price=100.0,
            commission=1.0,
            fill_time_utc=datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
            stop_price=90.0,
            target_price=115.0,
        )
        fills = registry.manual_fills()

    positions = reconstruct_open_positions(fills)

    assert len(positions) == 1
    assert positions[0].symbol == "NVDA"
    assert positions[0].quantity == 10
    assert positions[0].average_entry_price == 100.0
    assert positions[0].stop_price == 90.0
    assert positions[0].target_price == 115.0


def test_operations_registry_keeps_paper_fills_separate_from_manual_fills(tmp_path) -> None:
    database_path = tmp_path / "operations.sqlite"
    with OperationsRegistry(database_path) as registry:
        registry.record_paper_fill(
            symbol="NVDA",
            side="BUY",
            quantity=1,
            price=100.0,
            commission=0.1,
            fill_time_utc=datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
            signal_id="sig-1",
            stop_price=90.0,
            target_price=115.0,
        )

        assert registry.manual_fills() == []
        assert len(registry.paper_fills()) == 1


def test_reconstruct_open_positions_reduces_cost_basis_after_partial_sell() -> None:
    fills = [
        {
            "symbol": "NVDA",
            "side": "BUY",
            "quantity": 10,
            "price": 100.0,
            "commission": 1.0,
            "fill_time_utc": "2026-07-27T13:30:00+00:00",
            "created_at_utc": "2026-07-27T13:31:00+00:00",
            "signal_id": None,
            "stop_price": 90.0,
            "target_price": 115.0,
        },
        {
            "symbol": "NVDA",
            "side": "SELL",
            "quantity": 4,
            "price": 110.0,
            "commission": 1.0,
            "fill_time_utc": "2026-07-28T13:30:00+00:00",
            "created_at_utc": "2026-07-28T13:31:00+00:00",
            "signal_id": None,
            "stop_price": None,
            "target_price": None,
        },
    ]

    positions = reconstruct_open_positions(fills)

    assert len(positions) == 1
    assert positions[0].quantity == 6
    assert positions[0].cost_basis == 600.0
    assert positions[0].average_entry_price == 100.0


def test_reconstruct_open_positions_rejects_oversell() -> None:
    fills = [
        {
            "symbol": "NVDA",
            "side": "SELL",
            "quantity": 1,
            "price": 100.0,
            "commission": 0.0,
            "fill_time_utc": "2026-07-27T13:30:00+00:00",
            "created_at_utc": "2026-07-27T13:31:00+00:00",
            "signal_id": None,
            "stop_price": None,
            "target_price": None,
        }
    ]

    with pytest.raises(ValueError, match="SELL quantity exceeds"):
        reconstruct_open_positions(fills)


def test_position_check_uses_worse_open_when_stop_is_gapped_through() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    position = OpenPosition(
        symbol="NVDA",
        quantity=10,
        average_entry_price=100.0,
        cost_basis=1_000.0,
        entry_time_utc=datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
        last_fill_time_utc=datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
        buy_commission=1.0,
        realized_sell_commission=0.0,
        stop_price=90.0,
        target_price=115.0,
    )

    row = evaluate_position(
        position,
        bar={"open": 85.0, "high": 88.0, "low": 80.0, "close": 86.0},
        feature_row=None,
        market_regime="GREEN",
        as_of=date(2026, 7, 28),
        config=config,
    )

    assert row["recommended_action"] == "EXIT_STOP"
    assert row["primary_reason"] == "stop_touched_daily_bar"
    assert row["estimated_exit_price"] == 84.915


def test_position_check_flags_red_regime_as_defensive_rotation() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    position = OpenPosition(
        symbol="NVDA",
        quantity=10,
        average_entry_price=100.0,
        cost_basis=1_000.0,
        entry_time_utc=datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
        last_fill_time_utc=datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
        buy_commission=1.0,
        realized_sell_commission=0.0,
        stop_price=90.0,
        target_price=115.0,
    )

    row = evaluate_position(
        position,
        bar={"open": 100.0, "high": 104.0, "low": 96.0, "close": 101.0},
        feature_row=None,
        market_regime="RED",
        as_of=date(2026, 7, 28),
        config=config,
    )

    assert row["recommended_action"] == "DEFENSIVE_ROTATION"
    assert row["primary_reason"] == "benchmark_regime_red"
