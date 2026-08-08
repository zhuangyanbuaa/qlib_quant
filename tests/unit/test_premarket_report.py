from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from quant_system.decision.premarket import (
    _candidate_rows,
    _portfolio_posture,
    load_decision_universe,
)
from quant_system.decision.reports import write_premarket_report
from quant_system.domain.enums import MarketRegime
from quant_system.domain.trading import CandidateSignal
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
