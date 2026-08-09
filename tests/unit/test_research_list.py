from datetime import UTC, datetime
from pathlib import Path

from quant_system.decision.research import (
    build_research_rows,
    render_news_research_prompt,
    write_research_list_artifacts,
)


def _source_report() -> dict:
    return {
        "metadata": {
            "run_id": "research-run",
            "generated_at_utc": datetime(2026, 8, 9, 12, tzinfo=UTC).isoformat(),
            "signal_session": "2026-08-07",
            "data_cutoff_utc": "2026-08-07T20:00:00+00:00",
            "earliest_order_session": "2026-08-10",
            "earliest_order_time_utc": "2026-08-10T13:30:00+00:00",
        },
        "portfolio_posture": {"status": "DEFENSIVE_OVERLAY_AVAILABLE"},
        "strategy_context": {
            "candidate_tier_context": "DEFENSIVE",
            "reversal_context": {
                "status": "DOWNTREND_OR_WASHOUT",
                "action_hint": "DEFENSE_FIRST",
            },
        },
        "model_rank_context": {"status": "READY"},
        "candidates": [
            {
                "symbol": "ABBV",
                "universe_role": "hedge_overlay",
                "recommended_action": "REVIEW_AS_DEFENSIVE_OVERLAY",
                "score": 0.16,
                "signal_close": 246.0,
                "news_risk": "LOW",
            }
        ],
        "calibration_candidate_tiers": [
            {
                "symbol": "NVDA",
                "universe_role": "ai_alpha",
                "calibration_tier": "RELAXED",
                "context_tier": "RELAXED_WATCHLIST",
                "calibration_action": "RELAXED_WATCHLIST_REVIEW_ONLY",
                "manual_review_allowed": True,
                "sector_confirmation_pass": True,
                "reversal_phase": "CONFIRMED_REPAIR",
                "reversal_score": 7.5,
                "reversal_reasons": "trend_confirmed_reversal;above_ma20",
                "model_rank": 2,
                "model_score": 0.03,
                "model_rank_status": "READY",
                "benchmark_etf": "SMH",
                "score": 0.25,
                "signal_close": 190.0,
                "news_risk": "LOW",
            },
            {
                "symbol": "ASML",
                "universe_role": "ai_alpha",
                "calibration_tier": "RELAXED",
                "context_tier": "DEFENSIVE",
                "calibration_action": "WATCH_ONLY_SECTOR_CONFIRMATION_GATE",
                "manual_review_allowed": False,
                "sector_confirmation_pass": False,
                "sector_confirmation_reasons": "benchmark_etf:SMH;benchmark_trend:WEAKENING",
                "reversal_phase": "REPAIR_ATTEMPT",
                "reversal_score": 5.0,
                "model_rank": 1,
                "score": 0.12,
                "signal_close": 1741.0,
                "news_risk": "LOW",
            },
            {
                "symbol": "WEAK",
                "universe_role": "ai_alpha",
                "calibration_tier": "RELAXED",
                "context_tier": "DEFENSIVE",
                "calibration_action": "WATCH_ONLY_RELAXED_QUALITY_GATE",
                "manual_review_allowed": False,
                "sector_confirmation_pass": True,
                "reversal_phase": "DRIFTING_LOWER",
                "reversal_score": 0.5,
                "score": 0.01,
                "signal_close": 10.0,
                "news_risk": "LOW",
            },
        ],
    }


def test_build_research_rows_separates_actionable_and_blocked_interest() -> None:
    rows = build_research_rows(_source_report(), max_symbols=10)
    by_symbol = {row["symbol"]: row for row in rows}

    assert [row["symbol"] for row in rows][:3] == ["NVDA", "ABBV", "ASML"]
    assert by_symbol["NVDA"]["research_bucket"] == "ACTIONABLE_CANDIDATE"
    assert by_symbol["ASML"]["research_bucket"] == "BLOCKED_BUT_INTERESTING"
    assert "WEAK" in by_symbol
    assert by_symbol["WEAK"]["research_bucket"] == "BLOCKED_BUT_INTERESTING"
    assert by_symbol["ASML"]["research_reason"].startswith("BLOCKED_BUT_INTERESTING")


def test_render_news_research_prompt_contains_boundaries_and_symbols() -> None:
    rows = build_research_rows(_source_report(), max_symbols=3)
    prompt = render_news_research_prompt(
        report={
            "metadata": _source_report()["metadata"],
            "strategy_context": _source_report()["strategy_context"],
        },
        rows=rows,
        news_summary_days=7,
    )

    assert "不要给自动下单建议" in prompt
    assert "NVDA" in prompt
    assert "ASML" in prompt
    assert "重点研究" in prompt
    assert "DOWNTREND_OR_WASHOUT" in prompt


def test_write_research_list_artifacts_outputs_prompt(tmp_path: Path) -> None:
    rows = build_research_rows(_source_report(), max_symbols=3)
    report = {
        "metadata": {
            **_source_report()["metadata"],
            "decision_scope": "MANUAL_RESEARCH_ONLY_NOT_TRADE_PERMISSION",
        },
        "counts": {"research_candidate_count": len(rows)},
        "strategy_context": _source_report()["strategy_context"],
        "research_candidates": rows,
    }

    artifacts = write_research_list_artifacts(
        report=report,
        rows=rows,
        directory=tmp_path,
        news_summary_days=5,
    )

    assert artifacts.json_path.exists()
    assert artifacts.csv_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.prompt_path.exists()
    assert "Research lookback: 最近 `5` 天" in artifacts.prompt_path.read_text()
