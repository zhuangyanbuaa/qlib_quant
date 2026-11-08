from datetime import UTC, datetime, timedelta
from pathlib import Path

from quant_system.decision.digest import CoverageRow, DigestUniverse, build_daily_digest
from quant_system.decision.hierarchy import classify_reversal_context
from quant_system.universe.config import load_daily_digest_config


def inputs():
    metadata = {
        "run_id": "11111111-1111-4111-8111-111111111111",
        "signal_session": "2026-06-30",
        "earliest_order_session": "2026-07-01",
        "price_cutoff_utc": "2026-06-30T20:30:00+00:00",
        "generated_at_utc": "2026-07-01T12:00:00+00:00",
    }
    scope = DigestUniverse(
        (Path("fixture"),),
        ("AAA",),
        (),
        (
            CoverageRow(
                symbol="AAA",
                resolved_symbol="AAA",
                company_name="Fixture",
                source_lists=("fixture",),
                themes=("custom_theme",),
                status="IN_SCOPE",
                reason="selected_for_canonical_scan",
            ),
        ),
    )
    metrics = {
        "return_5d": 0.02,
        "return_20d": 0.03,
        "relative_return_20d": 0.01,
        "above_ma20": True,
        "above_ma50": True,
        "rsi14": 50,
    }
    state = {
        "trend_state": "UPTREND",
        "price_status": "AVAILABLE",
        "price_provenance": {},
        **classify_reversal_context(metrics, "UPTREND"),
    }
    states = {s: dict(state, symbol=s) for s in ("AAA", "SPY", "QQQ", "XLK")}
    candidate = {
        "symbol": "AAA",
        "signal_id": "signal",
        "score": 0.12,
        "news_risk": "UNKNOWN",
        "execution_eligible": False,
        "recommended_action": "REVIEW_NEWS_BEFORE_ORDER",
        "signal_session": "2026-06-30",
        "earliest_order_session": "2026-07-01",
    }
    premarket = {
        "metadata": metadata,
        "candidates": [candidate],
        "symbol_assessments": [
            {
                "symbol": "AAA",
                "price_status": "AVAILABLE",
                "news_risk": "UNKNOWN",
                "rule_checks": {"is_candidate": True, "feature_ready": True},
                "strategy_score": 0.12,
            }
        ],
        "portfolio_posture": {"market_regime": "GREEN"},
        "model_rank_context": {"status": "DISABLED"},
    }
    return dict(
        config=load_daily_digest_config(),
        scope=scope,
        metadata=metadata,
        premarket=premarket,
        states=states,
        now=datetime(2026, 7, 1, 12, tzinfo=UTC),
    )


def test_scores_are_explained_and_news_pending_is_never_entry_permission():
    digest = build_daily_digest(**inputs())
    assert digest.market.technical_score.value == 65
    assert digest.pending_candidates[0].technical_score.value == 65
    assert digest.pending_candidates[0].strategy_score == 0.12
    assert digest.entry_candidates == ()
    assert digest.pending_candidates[0].entry_status == "PENDING_NEWS"
    assert digest.stocks[0].score_details["score_raw"] == 6.5
    assert digest.market.benchmark_details["SPY"]["score_raw"] == 6.5
    assert len(digest.sectors) >= 15
    assert any(s.symbol == "XLE" and s.technical_score.value is None for s in digest.sectors)


def test_market_missing_input_does_not_reweight_and_high_score_does_not_create_candidate():
    args = inputs()
    args["states"].pop("SPY")
    args["premarket"]["candidates"] = []
    args["premarket"]["symbol_assessments"][0]["rule_checks"]["is_candidate"] = False
    digest = build_daily_digest(**args)
    assert digest.market.technical_score.value is None
    assert not digest.entry_candidates and not digest.pending_candidates
    assert digest.stocks[0].entry_status == "NOT_CANDIDATE"


def test_incomplete_theme_is_not_silently_reweighted():
    args = inputs()
    missing = (
        args["scope"].coverage[0].model_copy(update={"symbol": "BBB", "resolved_symbol": "BBB"})
    )
    args["scope"] = DigestUniverse((), ("AAA", "BBB"), (), (*args["scope"].coverage, missing))
    digest = build_daily_digest(**args)
    theme = next(s for s in digest.sectors if s.symbol == "theme:custom_theme")
    assert theme.valid_count == 1 and theme.total_count == 2
    assert theme.technical_score.value is None


def test_missing_prices_high_news_and_expired_session_cannot_be_recommended():
    for fault, expected in (("price", "UNAVAILABLE"), ("news", "VETOED"), ("time", "EXPIRED")):
        args = inputs()
        if fault == "price":
            args["premarket"]["symbol_assessments"][0]["price_status"] = "UNAVAILABLE"
        elif fault == "news":
            args["premarket"]["symbol_assessments"][0]["news_risk"] = "HIGH"
        else:
            args["now"] += timedelta(days=1)
        digest = build_daily_digest(**args)
        assert not digest.entry_candidates and not digest.pending_candidates
        assert digest.stocks[0].entry_status == expected
