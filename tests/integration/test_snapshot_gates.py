import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from quant_system.decision.gates import (
    load_snapshot,
    run_open_gate_workflow,
    run_preopen_refresh_workflow,
)
from quant_system.domain.clocks import NyseSessionClock


def snapshot_row(cutoff, **changes):
    return (
        dict(
            symbol="NVDA",
            last_price=101,
            quote_at_utc=(cutoff - timedelta(seconds=30)).isoformat(),
            session_date_ny=cutoff.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
            news_risk="LOW",
            liquidity_status="OK",
            source="broker_fixture",
            source_version="1",
            fetched_at_utc=cutoff.isoformat(),
            available_at_utc=cutoff.isoformat(),
            ingestion_run_id=str(uuid4()),
            is_stale=False,
            quality_flags=[],
        )
        | changes
    )


def run_gate(
    tmp_path,
    signal_session=date(2026, 6, 26),
    changes=None,
    *,
    generated=None,
    preopen=False,
    duplicate=False,
):
    clock = NyseSessionClock()
    order = clock.next_session(signal_session)
    cutoff = clock.session_open_utc(order) + timedelta(minutes=-10 if preopen else 30)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "metadata": {
                    "signal_session": signal_session.isoformat(),
                    "earliest_order_session": order.isoformat(),
                },
                "candidates": [
                    {
                        "symbol": "NVDA",
                        "recommended_action": "PREPARE_MANUAL_CONDITIONAL_ORDER",
                        "max_gap_up_price": 103,
                        "gap_down_cancel_below": 95,
                        "news_risk": "LOW",
                    }
                ],
            }
        )
    )
    row = snapshot_row(cutoff, **(changes or {}))
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"rows": [row, row] if duplicate else [row]}))
    workflow = run_preopen_refresh_workflow if preopen else run_open_gate_workflow
    kwargs = {} if preopen else {"minutes": 30}
    report, artifacts = workflow(
        premarket_report_path=plan,
        report_root=tmp_path / "reports",
        snapshot_path=path,
        generated_at_utc=generated or cutoff,
        **kwargs,
    )
    assert json.loads(artifacts.json_path.read_text())["rows"] == report["rows"]
    return report, cutoff


@pytest.mark.parametrize(
    "signal_session,order,cutoff_string",
    [
        (date(2026, 6, 26), "2026-06-29", "2026-06-29T14:00:00+00:00"),
        (date(2026, 7, 2), "2026-07-06", "2026-07-06T14:00:00+00:00"),
        (date(2026, 3, 6), "2026-03-09", "2026-03-09T14:00:00+00:00"),
        (date(2026, 10, 30), "2026-11-02", "2026-11-02T15:00:00+00:00"),
        (date(2026, 11, 25), "2026-11-27", "2026-11-27T15:00:00+00:00"),
    ],
)
def test_exchange_calendar_gate_and_snapshot_provenance(
    tmp_path, signal_session, order, cutoff_string
):
    report, cutoff = run_gate(tmp_path, signal_session)
    assert report["rows"][0]["decision"] == "KEEP"
    assert report["metadata"]["gate_cutoff_utc"] == cutoff.isoformat()
    assert report["metadata"]["gate_cutoff_utc"] == cutoff_string
    assert report["metadata"]["order_session"] == order
    assert report["rows"][0]["snapshot_source"] == "broker_fixture"


@pytest.mark.parametrize(
    "changes",
    [
        {"last_price": float("nan")},
        {"last_price": float("inf")},
        {"last_price": 0},
        {"source": ""},
        {"source_version": ""},
        {"is_stale": True},
        {"quality_flags": ["invalid_quote"]},
        {"session_date_ny": "2026-06-26"},
        {"news_risk": "UNKNOWN"},
        {"liquidity_status": "UNKNOWN"},
        {"fetched_at_utc": "2026-06-29T14:01:00Z"},
        {"available_at_utc": "2026-06-29T14:01:00Z", "fetched_at_utc": "2026-06-29T14:01:00Z"},
        {"quote_at_utc": "2026-06-29T13:00:00Z"},
    ],
)
def test_unreliable_snapshots_cannot_keep(tmp_path, changes):
    report, _ = run_gate(tmp_path, changes=changes)
    assert report["rows"][0]["decision"] == "DEFER"


def test_duplicate_rows_and_gate_run_outside_window_defer(tmp_path):
    report, cutoff = run_gate(tmp_path, duplicate=True)
    assert report["rows"][0]["reason"] == "duplicate_snapshot_symbol"
    early, _ = run_gate(tmp_path, generated=cutoff - timedelta(minutes=1))
    assert early["rows"][0]["reason"] == "gate_not_reached"
    expired, _ = run_gate(tmp_path, generated=datetime(2026, 6, 30, tzinfo=UTC))
    assert expired["rows"][0]["reason"] == "order_session_expired"
    late, _ = run_gate(tmp_path, generated=cutoff + timedelta(minutes=6))
    assert late["rows"][0]["reason"] == "gate_window_expired"
    preopen, _ = run_gate(tmp_path, preopen=True)
    assert preopen["rows"][0]["decision"] == "KEEP"


def test_legacy_snapshot_without_lineage_deferred(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps([{"symbol": "NVDA", "last_price": 101}]))
    assert load_snapshot(path)["NVDA"]["snapshot_error"] == "invalid_snapshot_contract"


def test_csv_flags_and_early_close_expiration(tmp_path):
    import csv

    cutoff = datetime(2026, 11, 27, 15, tzinfo=UTC)
    row = snapshot_row(cutoff)
    row["quality_flags"] = "[]"
    path = tmp_path / "snapshot.csv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    assert load_snapshot(path)["NVDA"]["last_price"] == 101
    report, _ = run_gate(
        tmp_path,
        signal_session=date(2026, 11, 25),
        generated=datetime(2026, 11, 27, 18, tzinfo=UTC),
    )
    assert report["rows"][0]["reason"] == "order_session_expired"
