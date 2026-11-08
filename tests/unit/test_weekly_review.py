import json
from datetime import UTC, date, datetime
from pathlib import Path

from quant_system.operations.scheduling import ScheduleState
from quant_system.operations.weekly import build_weekly_review
from quant_system.settings import Settings
from quant_system.storage.sqlite import OperationsRegistry


def write_report(root, day, run, generated, count):
    path = root / "reports/daily" / day / run
    path.mkdir(parents=True)
    metadata = dict(signal_session=day, generated_at_utc=generated)
    (path / "daily_index.json").write_text(
        json.dumps(
            {
                "metadata": metadata,
                "summary": {"candidate_count": count},
                "manual_research": {"package_id": run},
            }
        )
    )
    (path / "premarket.json").write_text(
        json.dumps(
            {
                "metadata": metadata,
                "candidates": [{"symbol": "AAPL", "signal_id": "signal", "limit_price": 100}],
            }
        )
    )


def test_weekly_uses_latest_daily_report_and_pit_separate_fills(tmp_path):
    write_report(tmp_path, "2026-10-12", "old", "2026-10-12T21:00:00+00:00", 1)
    write_report(tmp_path, "2026-10-12", "new", "2026-10-13T12:00:00+00:00", 2)
    write_report(tmp_path, "2026-10-12", "future", "2026-10-17T12:00:00+00:00", 99)
    db = tmp_path / "db/operations.sqlite"
    with OperationsRegistry(db) as registry:
        common = dict(
            symbol="AAPL",
            side="BUY",
            quantity=1,
            price=101,
            signal_id="signal",
            fill_time_utc=datetime(2026, 10, 13, 14, tzinfo=UTC),
            created_at_utc=datetime(2026, 10, 13, 14, tzinfo=UTC),
        )
        registry.record_manual_fill(**common)
        registry.record_paper_fill(**(common | {"price": 100.5}))
        registry.record_manual_fill(
            **(common | {"created_at_utc": datetime(2026, 10, 17, tzinfo=UTC)})
        )
    state = ScheduleState(db)
    state.job("fixture", "2026-10-12", datetime(2026, 10, 12, tzinfo=UTC))
    state.finish(
        "fixture", "DONE", {"status": "DEGRADED", "observed_at_utc": "2026-10-12T21:00:00+00:00"}
    )
    state.close()
    settings = Settings(_env_file=None, data_dir=tmp_path)
    result = build_weekly_review(
        settings, session=date(2026, 10, 16), cutoff=datetime(2026, 10, 16, 21, tzinfo=UTC)
    )
    report = json.loads(Path(result["report_path"]).read_text())
    assert result["summary"]["candidate_count"] == 2
    assert report["manual"]["fill_count"] == report["paper"]["fill_count"] == 1
    assert round(report["manual"]["slippage"][0]["slippage_bps"]) == 100
    assert round(report["paper"]["slippage"][0]["slippage_bps"]) == 50
    assert result["summary"]["exception_count"] == 1
    assert result["summary"]["pending_manual_research_count"] == 1
    assert report["performance"]["status"] == "NOT_COMPUTED"
    before = Path(result["report_path"]).read_text()
    second = build_weekly_review(
        settings, session=date(2026, 10, 16), cutoff=datetime(2026, 10, 16, 21, tzinfo=UTC)
    )
    assert (
        second["report_path"] != result["report_path"]
        and Path(result["report_path"]).read_text() == before
    )


def test_missing_malformed_artifacts_and_friday_holiday(tmp_path):
    path = tmp_path / "reports/daily/2026-07-02/fixture"
    path.mkdir(parents=True)
    (path / "daily_index.json").write_text("{}")
    result = build_weekly_review(
        Settings(_env_file=None, data_dir=tmp_path),
        session=date(2026, 7, 2),
        cutoff=datetime(2026, 7, 2, 21, tzinfo=UTC),
    )
    assert result["status"] == "DEGRADED" and result["summary"]["session_count"] == 4
    report = json.loads(Path(result["report_path"]).read_text())
    assert len(report["invalid_artifacts"]) == 1
