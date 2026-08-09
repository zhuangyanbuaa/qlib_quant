from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from quant_system.decision.workbench import (
    build_daily_workbench_index,
    render_daily_workbench_markdown,
    write_daily_workbench_index,
)

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
GENERATED_AT = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _artifacts(tmp_path: Path, stem: str) -> SimpleNamespace:
    directory = tmp_path / stem
    directory.mkdir()
    return SimpleNamespace(
        directory=directory,
        json_path=directory / f"{stem}.json",
        csv_path=directory / f"{stem}.csv",
        markdown_path=directory / f"{stem}.md",
        html_path=directory / f"{stem}.html",
        prompt_path=directory / f"{stem}_prompt.md",
    )


def test_daily_workbench_index_summarizes_components(tmp_path: Path) -> None:
    premarket = {
        "metadata": {"run_id": str(RUN_ID), "signal_session": "2026-08-07"},
        "counts": {"candidate_count": 1},
        "portfolio_posture": {"market_regime": "YELLOW", "status": "SELECTIVE"},
        "candidates": [{"symbol": "NVDA"}],
    }
    research = {
        "metadata": {"run_id": str(RUN_ID), "signal_session": "2026-08-07"},
        "counts": {"research_candidate_count": 1},
        "research_candidates": [
            {
                "research_rank": 1,
                "symbol": "NVDA",
                "research_bucket": "ACTIONABLE_CANDIDATE",
                "research_reason": "manual_review_allowed",
            }
        ],
    }
    leverage = {
        "metadata": {"pair_count": 48},
        "assessments": [
            {
                "attention_status": "GENERIC_2X_RISKON_WATCH",
                "action": "NO_2X_TRADE",
                "score": 6,
                "max_score": 8,
                "underlying_symbol": "NET",
                "leveraged_etf_symbol": None,
                "sector_etf": "IGV",
                "setup_type": "BREAKOUT",
            }
        ],
    }
    positions = {
        "metadata": {"run_id": str(RUN_ID)},
        "counts": {"open_position_count": 1},
        "positions": [
            {
                "symbol": "MU",
                "recommended_action": "EXIT_STOP",
                "primary_reason": "stop_touched_daily_bar",
            }
        ],
    }

    report = build_daily_workbench_index(
        run_id=RUN_ID,
        generated_at_utc=GENERATED_AT,
        signal_session=date(2026, 8, 7),
        premarket_report=premarket,
        premarket_artifacts=_artifacts(tmp_path, "premarket"),
        research_report=research,
        research_artifacts=_artifacts(tmp_path, "research"),
        leverage_report=leverage,
        leverage_artifacts=_artifacts(tmp_path, "leverage"),
        positions_report=positions,
        positions_artifacts=_artifacts(tmp_path, "positions"),
    )

    assert report["summary"]["candidate_count"] == 1
    assert report["summary"]["research_candidate_count"] == 1
    assert report["summary"]["leverage_attention_count"] == 1
    assert report["summary"]["position_action_required_count"] == 1
    assert report["leverage_attention"][0]["underlying_symbol"] == "NET"
    markdown = render_daily_workbench_markdown(report)
    assert "Daily Workbench Index" in markdown
    assert "GENERIC_2X_RISKON_WATCH" in markdown
    assert "EXIT_STOP" in markdown


def test_write_daily_workbench_index(tmp_path: Path) -> None:
    report = {
        "metadata": {
            "run_id": str(RUN_ID),
            "generated_at_utc": GENERATED_AT.isoformat(),
            "signal_session": "2026-08-07",
            "earliest_order_session": "2026-08-10",
            "decision_scope": "DAILY_WORKBENCH_INDEX_ONLY_NOT_TRADE_PERMISSION",
        },
        "summary": {
            "market_regime": "YELLOW",
            "posture": "SELECTIVE",
            "candidate_count": 0,
            "research_candidate_count": 0,
            "leverage_attention_count": 0,
            "position_action_required_count": 0,
        },
        "components": {"positions": {"status": "SKIPPED"}},
        "next_steps": ["No immediate action."],
        "top_research": [],
        "leverage_attention": [],
        "position_actions": [],
    }

    artifacts = write_daily_workbench_index(report=report, report_root=tmp_path)

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert "No immediate action" in artifacts.markdown_path.read_text(encoding="utf-8")
