from datetime import UTC, date, datetime
from uuid import uuid4

from quant_system.decision.gates import evaluate_gate_row
from quant_system.decision.reports import (
    write_decision_table_report,
    write_premarket_report,
)


def candidate() -> dict[str, object]:
    return {
        "symbol": "NVDA",
        "signal_id": "sig-1",
        "universe_role": "ai_alpha",
        "recommended_action": "PREPARE_MANUAL_CONDITIONAL_ORDER",
        "signal_close": 100.0,
        "limit_price": 100.0,
        "max_gap_up_price": 103.0,
        "gap_down_cancel_below": 95.0,
        "stop_price": 90.0,
        "target_price": 115.0,
        "news_risk": "LOW",
    }


def test_gate_without_snapshot_defers_conservatively() -> None:
    row = evaluate_gate_row(candidate(), snapshot=None, minutes_before_or_after_open=30)

    assert row["decision"] == "DEFER"
    assert row["reason"] == "no_intraday_snapshot_available"


def test_gate_cancels_when_snapshot_gaps_below_plan() -> None:
    row = evaluate_gate_row(
        candidate(),
        snapshot={"symbol": "NVDA", "last_price": 94.0},
        minutes_before_or_after_open=30,
    )

    assert row["decision"] == "CANCEL"
    assert row["reason"] == "gap_down_below_plan"


def test_gate_keeps_when_snapshot_is_inside_bounds() -> None:
    row = evaluate_gate_row(
        candidate(),
        snapshot={"symbol": "NVDA", "last_price": 101.0, "news_risk": "LOW"},
        minutes_before_or_after_open=60,
    )

    assert row["decision"] == "KEEP"
    assert row["reason"] == "within_price_and_risk_bounds"


def test_reports_write_html_artifacts(tmp_path) -> None:
    run_id = uuid4()
    report = {
        "metadata": {
            "run_id": str(run_id),
            "generated_at_utc": datetime(2026, 8, 8, 0, tzinfo=UTC).isoformat(),
            "signal_session": "2026-07-24",
            "earliest_order_session": "2026-07-27",
        },
        "counts": {
            "candidate_count": 0,
            "ai_candidate_count": 0,
            "hedge_candidate_count": 0,
            "medium_news_risk_count": 0,
        },
        "portfolio_posture": {
            "market_regime": "GREEN",
            "status": "CASH_FIRST",
            "message": "No candidates.",
        },
    }

    artifacts = write_premarket_report(
        report=report,
        candidate_rows=[],
        report_root=tmp_path,
        signal_session=date(2026, 7, 24),
        run_id=run_id,
    )

    assert artifacts.html_path.exists()
    assert "<html" in artifacts.html_path.read_text(encoding="utf-8")


def test_generic_decision_report_writes_all_formats(tmp_path) -> None:
    run_id = uuid4()
    artifacts = write_decision_table_report(
        report={
            "metadata": {"run_id": str(run_id), "as_of": "2026-07-24"},
            "counts": {"row_count": 1},
            "rows": [{"symbol": "NVDA", "decision": "KEEP"}],
        },
        rows=[{"symbol": "NVDA", "decision": "KEEP"}],
        report_root=tmp_path,
        as_of=date(2026, 7, 24),
        run_id=run_id,
        stem="open_gate_30",
        title="Open Gate",
    )

    assert artifacts.json_path.exists()
    assert artifacts.csv_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.html_path.exists()
