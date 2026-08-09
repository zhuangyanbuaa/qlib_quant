import pandas as pd
from apps.decision_dashboard import (
    _join_list_value,
    _prepare_news_frame,
    _symbols_for_news,
)


def test_dashboard_news_symbols_collect_from_daily_reports() -> None:
    symbols = _symbols_for_news(
        index_report={
            "top_research": [{"symbol": "asml"}],
            "leverage_attention": [{"underlying_symbol": "net"}],
            "position_actions": [{"symbol": "mu"}],
        },
        premarket={
            "candidates": [{"symbol": "NVDA"}],
            "calibration_candidate_tiers": [{"symbol": "AMD"}],
        },
        research={"research_candidates": [{"symbol": "ORCL"}]},
        leverage={
            "assessments": [
                {
                    "underlying_symbol": "SNOW",
                    "attention_status": "GENERIC_2X_RISKON_WATCH",
                },
                {
                    "underlying_symbol": "QQQ",
                    "attention_status": "NO_LEVERAGE_ATTENTION",
                },
            ]
        },
        positions={"positions": [{"symbol": "DELL"}]},
    )

    assert symbols == ["AMD", "ASML", "DELL", "MU", "NET", "NVDA", "ORCL", "SNOW"]


def test_dashboard_news_frame_formats_list_columns() -> None:
    frame = pd.DataFrame(
        [
            {
                "published_at_utc": "2026-08-07T12:00:00+00:00",
                "title": "Nvidia headline",
                "matched_symbols": ["NVDA", "AMD"],
                "raw_topics": ["technology"],
            }
        ]
    )

    prepared = _prepare_news_frame(frame)

    assert prepared.loc[0, "matched_symbols"] == "NVDA,AMD"
    assert prepared.loc[0, "raw_topics"] == "technology"
    assert _join_list_value(None) == ""
