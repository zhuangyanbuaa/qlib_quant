from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from quant_system.decision.premarket import (
    _calibration_candidate_rows,
    _candidate_rows,
    _exclude_context_only_benchmarks,
    _portfolio_posture,
    load_decision_universe,
)
from quant_system.decision.reports import write_premarket_report
from quant_system.domain.enums import MarketRegime
from quant_system.domain.trading import CandidateSignal
from quant_system.strategy.calibration import TieredCandidateSignal
from quant_system.strategy.config import load_buy_the_dip_config


def test_load_decision_universe_tags_ai_and_hedge_roles() -> None:
    symbols, roles = load_decision_universe(
        (
            Path("configs/universe/ai_watchlist.yaml"),
            Path("configs/universe/hedge_overlay.yaml"),
        )
    )

    assert "NVDA" in symbols
    assert "COST" in symbols
    assert "QQQ" in symbols
    assert roles["NVDA"] == "ai_alpha"
    assert roles["COST"] == "hedge_overlay"
    assert roles["QQQ"] == "benchmark"


def test_candidate_rows_include_manual_order_draft() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    signal = CandidateSignal(
        symbol="NVDA",
        signal_session=date(2026, 7, 24),
        data_cutoff_utc=datetime(2026, 7, 24, 20, tzinfo=UTC),
        signal_time_utc=datetime(2026, 7, 24, 20, 30, tzinfo=UTC),
        earliest_order_session=date(2026, 7, 27),
        earliest_order_time_utc=datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
        score=0.12,
        signal_close=100.0,
        atr20=5.0,
        market_regime=MarketRegime.GREEN,
        reasons=("dip_yesterday", "daily_confirmation"),
    )

    rows = _candidate_rows([signal], role_by_symbol={"NVDA": "ai_alpha"}, config=config)

    assert rows[0]["limit_price"] == 100.0
    assert rows[0]["stop_price"] == 90.0
    assert rows[0]["target_price"] == 115.0
    assert rows[0]["max_gap_up_price"] == 103.0
    assert rows[0]["gap_down_cancel_below"] == 95.0
    assert rows[0]["recommended_action"] == "PREPARE_MANUAL_CONDITIONAL_ORDER"


def test_context_only_benchmarks_are_excluded_from_daily_candidates() -> None:
    signal = CandidateSignal(
        symbol="XLP",
        signal_session=date(2026, 7, 24),
        data_cutoff_utc=datetime(2026, 7, 24, 20, tzinfo=UTC),
        signal_time_utc=datetime(2026, 7, 24, 20, 30, tzinfo=UTC),
        earliest_order_session=date(2026, 7, 27),
        earliest_order_time_utc=datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
        score=0.02,
        signal_close=80.0,
        atr20=2.0,
        market_regime=MarketRegime.GREEN,
        reasons=("dip_yesterday", "daily_confirmation"),
    )

    assert _exclude_context_only_benchmarks([signal], {"XLP": "benchmark"}) == []


def test_relaxed_candidate_without_quality_gate_is_watch_only() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    signal = _signal("WEAK")

    rows = _calibration_candidate_rows(
        [
            TieredCandidateSignal(
                signal=signal,
                calibration_tier="RELAXED",
                passed_tiers=("RELAXED",),
            )
        ],
        role_by_symbol={"WEAK": "ai_alpha"},
        config=config,
        strategy_context={
            "candidate_tier_context": "RELAXED_WATCHLIST",
            "ai_vs_hedge_spread_20d": -0.01,
        },
        hierarchy_rows=[
            {
                "symbol": "WEAK",
                "role": "stock",
                "trend_state": "DRIFT_DOWN",
                "theme": "ai_chips",
                "sector": "Information Technology",
            }
        ],
        rotation_rows=[
            {
                "group_type": "theme",
                "group": "ai_chips",
                "rotation_status": "LAGGING",
                "relative_return_20d": -0.02,
            }
        ],
    )

    assert rows[0]["relaxed_quality_pass"] is False
    assert rows[0]["manual_review_allowed"] is False
    assert rows[0]["calibration_action"] == "WATCH_ONLY_RELAXED_QUALITY_GATE"


def test_relaxed_leader_reversal_passes_quality_gate() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    signal = _signal("NVDA")

    rows = _calibration_candidate_rows(
        [
            TieredCandidateSignal(
                signal=signal,
                calibration_tier="RELAXED",
                passed_tiers=("RELAXED",),
            )
        ],
        role_by_symbol={"NVDA": "ai_alpha"},
        config=config,
        strategy_context={
            "candidate_tier_context": "RELAXED_WATCHLIST",
            "ai_vs_hedge_spread_20d": -0.01,
        },
        hierarchy_rows=[
            {
                "symbol": "NVDA",
                "role": "leader_stock",
                "trend_state": "REVERSAL_ATTEMPT",
                "theme": "ai_chips",
                "sector": "Information Technology",
            }
        ],
        rotation_rows=[],
    )

    assert rows[0]["relaxed_quality_pass"] is True
    assert rows[0]["relaxed_quality_reasons"] == "leader_stock_REVERSAL_ATTEMPT"
    assert rows[0]["manual_review_allowed"] is True
    assert rows[0]["calibration_action"] == "RELAXED_WATCHLIST_REVIEW_ONLY"


def test_relaxed_theme_strength_without_leader_reversal_is_watch_only() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    signal = _signal("MID")

    rows = _calibration_candidate_rows(
        [
            TieredCandidateSignal(
                signal=signal,
                calibration_tier="RELAXED",
                passed_tiers=("RELAXED",),
            )
        ],
        role_by_symbol={"MID": "ai_alpha"},
        config=config,
        strategy_context={
            "candidate_tier_context": "RELAXED_WATCHLIST",
            "ai_vs_hedge_spread_20d": 0.05,
        },
        hierarchy_rows=[
            {
                "symbol": "MID",
                "role": "stock",
                "trend_state": "REVERSAL_ATTEMPT",
                "theme": "ai_chips",
                "sector": "Information Technology",
            }
        ],
        rotation_rows=[
            {
                "group_type": "theme",
                "group": "ai_chips",
                "rotation_status": "LEADING",
                "relative_return_20d": 0.03,
            }
        ],
    )

    assert rows[0]["relaxed_quality_pass"] is False
    assert rows[0]["manual_review_allowed"] is False
    assert rows[0]["calibration_action"] == "WATCH_ONLY_RELAXED_QUALITY_GATE"
    assert rows[0]["relaxed_quality_reasons"] == ("theme_LEADING;ai_vs_hedge_spread_20d_positive")


def test_defensive_relaxed_overlay_requires_quality_gate() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    signal = _signal("CL")

    rows = _calibration_candidate_rows(
        [
            TieredCandidateSignal(
                signal=signal,
                calibration_tier="RELAXED",
                passed_tiers=("RELAXED",),
            )
        ],
        role_by_symbol={"CL": "hedge_overlay"},
        config=config,
        strategy_context={
            "candidate_tier_context": "DEFENSIVE",
            "ai_vs_hedge_spread_20d": -0.10,
        },
        hierarchy_rows=[
            {
                "symbol": "CL",
                "role": "stock",
                "trend_state": "LAGGING",
                "theme": "defensive_staples",
                "sector": "Consumer Staples",
            }
        ],
        rotation_rows=[
            {
                "group_type": "theme",
                "group": "defensive_staples",
                "rotation_status": "LAGGING",
                "relative_return_20d": -0.01,
            }
        ],
    )

    assert rows[0]["relaxed_quality_pass"] is False
    assert rows[0]["defensive_overlay_quality_pass"] is False
    assert rows[0]["manual_review_allowed"] is False
    assert rows[0]["calibration_action"] == "WATCH_ONLY_DEFENSIVE_OVERLAY_QUALITY_GATE"


def test_defensive_overlay_healthcare_leader_with_theme_strength_is_reviewable() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    signal = _signal("JNJ")

    rows = _calibration_candidate_rows(
        [
            TieredCandidateSignal(
                signal=signal,
                calibration_tier="RELAXED",
                passed_tiers=("RELAXED",),
            )
        ],
        role_by_symbol={"JNJ": "hedge_overlay"},
        config=config,
        strategy_context={
            "candidate_tier_context": "DEFENSIVE",
            "ai_vs_hedge_spread_20d": -0.10,
        },
        hierarchy_rows=[
            {
                "symbol": "JNJ",
                "role": "leader_stock",
                "trend_state": "CONFIRMED_REVERSAL",
                "theme": "defensive_healthcare",
                "sector": "Health Care",
            }
        ],
        rotation_rows=[
            {
                "group_type": "theme",
                "group": "defensive_healthcare",
                "rotation_status": "IMPROVING",
                "relative_return_20d": 0.02,
            }
        ],
    )

    assert rows[0]["defensive_overlay_quality_pass"] is True
    assert rows[0]["manual_review_allowed"] is True
    assert rows[0]["calibration_action"] == "REVIEW_AS_DEFENSIVE_OVERLAY"


def test_defensive_overlay_financial_leader_is_context_only() -> None:
    config = load_buy_the_dip_config(Path("configs/strategy/buy_the_dip.yaml"))
    signal = _signal("V")

    rows = _calibration_candidate_rows(
        [
            TieredCandidateSignal(
                signal=signal,
                calibration_tier="RELAXED",
                passed_tiers=("RELAXED",),
            )
        ],
        role_by_symbol={"V": "hedge_overlay"},
        config=config,
        strategy_context={
            "candidate_tier_context": "DEFENSIVE",
            "ai_vs_hedge_spread_20d": -0.10,
        },
        hierarchy_rows=[
            {
                "symbol": "V",
                "role": "leader_stock",
                "trend_state": "CONFIRMED_REVERSAL",
                "theme": "defensive_financials",
                "sector": "Financials",
            }
        ],
        rotation_rows=[
            {
                "group_type": "theme",
                "group": "defensive_financials",
                "rotation_status": "LEADING",
                "relative_return_20d": 0.03,
            }
        ],
    )

    assert rows[0]["defensive_overlay_quality_pass"] is False
    assert rows[0]["manual_review_allowed"] is False
    assert rows[0]["calibration_action"] == "WATCH_ONLY_DEFENSIVE_OVERLAY_QUALITY_GATE"


def test_portfolio_posture_mentions_defensive_overlay_when_only_hedges_pass() -> None:
    posture = _portfolio_posture(
        market_regime="GREEN",
        candidate_rows=[{"universe_role": "hedge_overlay"}],
    )

    assert posture["status"] == "DEFENSIVE_OVERLAY_AVAILABLE"
    assert "hedge overlay candidates exist" in posture["message"]


def test_write_premarket_report_outputs_json_csv_and_markdown(tmp_path) -> None:
    run_id = uuid4()
    report = {
        "metadata": {
            "run_id": str(run_id),
            "generated_at_utc": datetime(2026, 7, 26, 12, tzinfo=UTC).isoformat(),
            "signal_session": "2026-07-24",
            "earliest_order_session": "2026-07-27",
        },
        "counts": {
            "candidate_count": 1,
            "ai_candidate_count": 1,
            "hedge_candidate_count": 0,
            "medium_news_risk_count": 0,
        },
        "portfolio_posture": {
            "status": "RISK_ON_SELECTIVE",
            "market_regime": "GREEN",
            "message": "Rules-approved candidates exist.",
        },
    }
    rows = [
        {
            "rank": 1,
            "symbol": "NVDA",
            "universe_role": "ai_alpha",
            "score": 0.12,
            "signal_close": 100.0,
            "limit_price": 100.0,
            "stop_price": 90.0,
            "target_price": 115.0,
            "news_risk": "LOW",
            "recommended_action": "PREPARE_MANUAL_CONDITIONAL_ORDER",
        }
    ]

    artifacts = write_premarket_report(
        report=report,
        candidate_rows=rows,
        report_root=tmp_path,
        signal_session=date(2026, 7, 24),
        run_id=run_id,
    )

    assert artifacts.json_path.exists()
    assert artifacts.csv_path.exists()
    assert artifacts.markdown_path.exists()
    assert "NVDA" in artifacts.markdown_path.read_text(encoding="utf-8")


def test_write_premarket_report_includes_calibration_section(tmp_path) -> None:
    run_id = uuid4()
    report = {
        "metadata": {
            "run_id": str(run_id),
            "generated_at_utc": datetime(2026, 7, 26, 12, tzinfo=UTC).isoformat(),
            "signal_session": "2026-07-24",
            "earliest_order_session": "2026-07-27",
        },
        "counts": {
            "candidate_count": 0,
            "ai_candidate_count": 0,
            "hedge_candidate_count": 0,
            "medium_news_risk_count": 0,
        },
        "calibration_counts": {
            "calibration_candidate_count": 1,
            "strict_candidate_count": 0,
            "baseline_candidate_count": 0,
            "relaxed_only_candidate_count": 1,
            "manual_review_allowed_count": 1,
            "defensive_defer_count": 0,
        },
        "portfolio_posture": {
            "status": "CASH_FIRST",
            "market_regime": "GREEN",
            "message": "No baseline candidates.",
        },
        "strategy_context": {
            "candidate_tier_context": "RELAXED_WATCHLIST",
            "message": "AI leaders show reversal breadth.",
        },
        "calibration_candidate_tiers": [
            {
                "calibration_rank": 1,
                "symbol": "NVDA",
                "universe_role": "ai_alpha",
                "calibration_tier": "RELAXED",
                "passed_tiers": "RELAXED",
                "score": -0.01,
                "manual_review_allowed": True,
                "calibration_action": "RELAXED_WATCHLIST_REVIEW_ONLY",
            }
        ],
    }

    artifacts = write_premarket_report(
        report=report,
        candidate_rows=[],
        report_root=tmp_path,
        signal_session=date(2026, 7, 24),
        run_id=run_id,
    )
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert "Strategy calibration" in markdown
    assert "RELAXED_WATCHLIST" in markdown
    assert "RELAXED_WATCHLIST_REVIEW_ONLY" in markdown


def test_write_premarket_report_includes_model_rank_context(tmp_path) -> None:
    run_id = uuid4()
    report = {
        "metadata": {
            "run_id": str(run_id),
            "generated_at_utc": datetime(2026, 7, 26, 12, tzinfo=UTC).isoformat(),
            "signal_session": "2026-07-24",
            "earliest_order_session": "2026-07-27",
        },
        "counts": {
            "candidate_count": 1,
            "ai_candidate_count": 1,
            "hedge_candidate_count": 0,
            "medium_news_risk_count": 0,
        },
        "portfolio_posture": {
            "status": "RISK_ON_SELECTIVE",
            "market_regime": "GREEN",
            "message": "Rules-approved candidates exist.",
        },
        "model_rank_context": {
            "status": "SCORED",
            "model": "lightgbm_daily_context_v1",
            "decision_scope": "RANK_CONTEXT_ONLY_DOES_NOT_CHANGE_ACTIONS",
            "training_rows": 33,
            "scored_rows": 1,
        },
    }
    rows = [
        {
            "rank": 1,
            "symbol": "NVDA",
            "universe_role": "ai_alpha",
            "score": 0.12,
            "signal_close": 100.0,
            "limit_price": 100.0,
            "stop_price": 90.0,
            "target_price": 115.0,
            "model_rank": 1,
            "model_score": 0.123456,
            "model_rank_status": "SCORED",
            "news_risk": "LOW",
            "recommended_action": "PREPARE_MANUAL_CONDITIONAL_ORDER",
        }
    ]

    artifacts = write_premarket_report(
        report=report,
        candidate_rows=rows,
        report_root=tmp_path,
        signal_session=date(2026, 7, 24),
        run_id=run_id,
    )
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert "Model rank context" in markdown
    assert "lightgbm_daily_context_v1" in markdown
    assert "Training rows: `33`" in markdown
    assert "0.1235" in markdown
    assert "SCORED" in markdown


def _signal(symbol: str) -> CandidateSignal:
    return CandidateSignal(
        symbol=symbol,
        signal_session=date(2026, 7, 24),
        data_cutoff_utc=datetime(2026, 7, 24, 20, tzinfo=UTC),
        signal_time_utc=datetime(2026, 7, 24, 20, 30, tzinfo=UTC),
        earliest_order_session=date(2026, 7, 27),
        earliest_order_time_utc=datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
        score=0.02,
        signal_close=80.0,
        atr20=2.0,
        market_regime=MarketRegime.GREEN,
        reasons=("dip_yesterday", "daily_confirmation"),
    )
