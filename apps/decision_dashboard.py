"""Local Streamlit dashboard for daily decision-support reports.

Run from the project root:

    streamlit run apps/decision_dashboard.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from quant_system.decision.journal import reconstruct_open_positions  # noqa: E402
from quant_system.settings import get_settings  # noqa: E402
from quant_system.storage.sqlite import OperationsRegistry  # noqa: E402


def main() -> None:
    st.set_page_config(page_title="Qlib Quant Daily Workbench", layout="wide")
    _inject_style()
    st.title("Qlib Quant Daily Workbench")
    settings = get_settings()
    data_root = settings.resolved_data_dir
    daily_root = data_root / "reports" / "daily"

    dates = _available_report_dates(daily_root)
    if not dates:
        st.info("No daily reports found yet. Run `quant decision premarket` first.")
        _render_live_journal(data_root)
        return

    selected_date = st.sidebar.selectbox("Report date", dates, index=0)
    date_root = daily_root / selected_date
    index_report = _latest_json(date_root, "daily_index.json")
    premarket = _latest_json(date_root, "premarket.json")
    research = _latest_json(date_root, "research.json")
    leverage = _latest_json(date_root, "leverage_overlay_universe.json")
    positions = _latest_json(date_root, "positions.json")

    tab_today, tab_candidates, tab_research, tab_leverage, tab_portfolio, tab_reports, tab_data = (
        st.tabs(
            [
                "Today",
                "Candidates",
                "Research",
                "2x Overlay",
                "Portfolio",
                "Reports",
                "Data Health",
            ]
        )
    )
    with tab_today:
        _render_today(index_report, premarket, positions, leverage)
    with tab_candidates:
        _render_candidates(premarket)
    with tab_research:
        _render_research(research, index_report)
    with tab_leverage:
        _render_leverage(leverage, index_report)
    with tab_portfolio:
        _render_portfolio(positions, data_root)
    with tab_reports:
        _render_reports(date_root)
    with tab_data:
        _render_data_health(premarket, positions, data_root)


def _available_report_dates(daily_root: Path) -> list[str]:
    if not daily_root.exists():
        return []
    return sorted(
        [path.name for path in daily_root.iterdir() if path.is_dir()],
        reverse=True,
    )


def _latest_json(date_root: Path, filename: str) -> dict[str, Any] | None:
    paths = sorted(date_root.glob(f"*/{filename}"), key=lambda p: p.stat().st_mtime)
    if not paths:
        return None
    return json.loads(paths[-1].read_text(encoding="utf-8"))


def _inject_style() -> None:
    st.markdown(
        """
        <style>
        .block-container {padding-top: 2rem;}
        div[data-testid="stMetric"] {
            background: linear-gradient(135deg, rgba(13, 24, 38, .96), rgba(24, 35, 49, .86));
            border: 1px solid rgba(93, 185, 255, .20);
            border-radius: 18px;
            padding: 14px 16px;
            box-shadow: 0 12px 36px rgba(0,0,0,.22);
        }
        div[data-testid="stMetricLabel"] {color: #9fb4c9;}
        div[data-testid="stMetricValue"] {color: #f5f7fb;}
        .workbench-callout {
            border-left: 4px solid #4cc9f0;
            padding: .8rem 1rem;
            border-radius: 12px;
            background: rgba(76, 201, 240, .08);
            margin: .75rem 0 1rem 0;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_today(
    index_report: dict[str, Any] | None,
    premarket: dict[str, Any] | None,
    positions: dict[str, Any] | None,
    leverage: dict[str, Any] | None,
) -> None:
    st.header("Today")
    if index_report is not None:
        _render_daily_index_summary(index_report)
    if premarket is None:
        st.warning("No premarket report for this date.")
        return
    metadata = premarket["metadata"]
    counts = premarket["counts"]
    posture = premarket["portfolio_posture"]
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Signal Session", metadata["signal_session"])
    col2.metric("Market Regime", posture["market_regime"])
    col3.metric("Posture", posture["status"])
    col4.metric("Candidates", counts["candidate_count"])
    st.info(posture["message"])
    _render_model_rank_context(premarket)
    if leverage is not None:
        attention = leverage.get("metadata", {}).get("attention_counts", {})
        if attention:
            st.markdown(
                '<div class="workbench-callout">2x overlay attention is available. '
                "Open the 2x Overlay tab before considering tactical leverage.</div>",
                unsafe_allow_html=True,
            )
            st.json(attention)
    if positions is not None:
        position_counts = positions["counts"]
        col1, col2, col3 = st.columns(3)
        col1.metric("Open Positions", position_counts["open_position_count"])
        col2.metric("Action Required", position_counts["action_required_count"])
        col3.metric("Hold", position_counts["hold_count"])


def _render_daily_index_summary(index_report: dict[str, Any]) -> None:
    summary = index_report.get("summary", {})
    metadata = index_report.get("metadata", {})
    st.caption(f"Run ID: {metadata.get('run_id', '')}")
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Main", summary.get("candidate_count", 0))
    col2.metric("Research", summary.get("research_candidate_count", 0))
    col3.metric("2x Attention", summary.get("leverage_attention_count", 0))
    col4.metric("Positions", summary.get("open_position_count", 0))
    col5.metric("Actions", summary.get("position_action_required_count", 0))
    next_steps = index_report.get("next_steps", [])
    if next_steps:
        st.subheader("Next steps")
        for step in next_steps:
            st.write(f"- {step}")


def _render_candidates(premarket: dict[str, Any] | None) -> None:
    st.header("Candidates")
    if premarket is None:
        st.warning("No candidate report available.")
        return
    candidates = pd.DataFrame(premarket.get("candidates", []))
    calibration = pd.DataFrame(premarket.get("calibration_candidate_tiers", []))
    _render_model_rank_context(premarket)
    if not calibration.empty:
        st.subheader("Calibration candidates")
        display_columns = [
            column
            for column in (
                "calibration_rank",
                "symbol",
                "universe_role",
                "calibration_tier",
                "context_tier",
                "manual_review_allowed",
                "calibration_action",
                "benchmark_etf",
                "sector_confirmation_pass",
                "sector_confirmation_reasons",
                "model_rank",
                "model_score",
                "model_rank_status",
            )
            if column in calibration.columns
        ]
        st.dataframe(calibration[display_columns], use_container_width=True)
    if candidates.empty:
        st.info("No rules-approved candidates in this report.")
        return
    st.subheader("Rules-approved candidates")
    st.dataframe(candidates, use_container_width=True)
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("By Universe Role")
        st.bar_chart(candidates["universe_role"].value_counts())
    with col2:
        st.subheader("By Recommended Action")
        st.bar_chart(candidates["recommended_action"].value_counts())


def _render_research(
    research: dict[str, Any] | None,
    index_report: dict[str, Any] | None,
) -> None:
    st.header("Research List")
    rows = []
    if research is not None:
        rows = research.get("research_candidates", [])
        counts = research.get("counts", {})
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Research", counts.get("research_candidate_count", 0))
        col2.metric("Actionable", counts.get("actionable_count", 0))
        col3.metric("Watchlist", counts.get("research_watchlist_count", 0))
        col4.metric("Blocked", counts.get("blocked_but_interesting_count", 0))
    elif index_report is not None:
        rows = index_report.get("top_research", [])
        st.info("No research.json found; showing daily_index top_research snapshot.")
    else:
        st.warning("No research report found.")
        return
    frame = pd.DataFrame(rows)
    if frame.empty:
        st.info("No research candidates.")
        return
    display_columns = [
        column
        for column in (
            "research_rank",
            "symbol",
            "research_bucket",
            "universe_role",
            "context_tier",
            "reversal_phase",
            "sector_confirmation_pass",
            "model_rank",
            "model_score",
            "research_reason",
        )
        if column in frame.columns
    ]
    st.dataframe(frame[display_columns], use_container_width=True)
    if "research_bucket" in frame.columns:
        st.subheader("Research buckets")
        st.bar_chart(frame["research_bucket"].value_counts())


def _render_leverage(
    leverage: dict[str, Any] | None,
    index_report: dict[str, Any] | None,
) -> None:
    st.header("2x Overlay")
    rows = []
    if leverage is not None:
        rows = leverage.get("assessments", [])
        metadata = leverage.get("metadata", {})
        col1, col2, col3 = st.columns(3)
        col1.metric("Pairs", metadata.get("pair_count", 0))
        col2.metric("Formal Review", metadata.get("review_candidate_count", 0))
        col3.metric(
            "Attention",
            sum(
                count
                for status, count in metadata.get("attention_counts", {}).items()
                if status != "NO_LEVERAGE_ATTENTION"
            ),
        )
        if metadata.get("attention_counts"):
            st.json(metadata["attention_counts"])
    elif index_report is not None:
        rows = index_report.get("leverage_attention", [])
        st.info("No leverage_overlay_universe.json found; showing daily_index snapshot.")
    else:
        st.warning("No leverage overlay report found.")
        return
    frame = pd.DataFrame(rows)
    if frame.empty:
        st.info("No leverage attention rows.")
        return
    if "attention_status" in frame.columns:
        selected = st.multiselect(
            "Attention status",
            sorted(frame["attention_status"].dropna().unique()),
            default=[
                status
                for status in sorted(frame["attention_status"].dropna().unique())
                if status != "NO_LEVERAGE_ATTENTION"
            ],
        )
        if selected:
            frame = frame.loc[frame["attention_status"].isin(selected)]
    display_columns = [
        column
        for column in (
            "attention_status",
            "action",
            "score",
            "max_score",
            "tier",
            "underlying_symbol",
            "leveraged_etf_symbol",
            "product_type",
            "sector_etf",
            "setup_type",
            "stop_pct",
            "risk_reward_estimate",
            "notes",
        )
        if column in frame.columns
    ]
    st.dataframe(frame[display_columns], use_container_width=True)


def _render_model_rank_context(premarket: dict[str, Any]) -> None:
    context = premarket.get("model_rank_context", {})
    if not context:
        return
    with st.expander("Model rank context", expanded=False):
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Status", context.get("status", "DISABLED"))
        col2.metric("Training rows", context.get("training_rows", 0))
        col3.metric("Minimum rows", context.get("minimum_train_rows", "n/a"))
        col4.metric("Scored rows", context.get("scored_rows", 0))
        st.caption(context.get("decision_scope", "RANK_CONTEXT_ONLY"))
        st.json({key: value for key, value in context.items() if key not in {"feature_columns"}})


def _render_portfolio(positions: dict[str, Any] | None, data_root: Path) -> None:
    st.header("Portfolio")
    if positions is None:
        st.info(
            "No position-check report for this date. Run "
            "`quant decision positions --date YYYY-MM-DD`."
        )
        _render_live_journal(data_root)
        return
    rows = pd.DataFrame(positions.get("positions", []))
    if rows.empty:
        st.success("No open positions are recorded.")
        return
    st.dataframe(rows, use_container_width=True)
    required = rows.loc[rows["recommended_action"] != "HOLD"]
    if not required.empty:
        st.warning("Some positions require manual review.")
        st.dataframe(
            required[["symbol", "recommended_action", "primary_reason"]],
            use_container_width=True,
        )
    st.subheader("Unrealized PnL")
    st.bar_chart(rows.set_index("symbol")["unrealized_pnl"])


def _render_reports(date_root: Path) -> None:
    st.header("Reports")
    report_files = sorted(date_root.glob("*/*.md"), key=lambda p: p.stat().st_mtime)
    if not report_files:
        st.info("No Markdown reports found.")
        return
    selected = st.selectbox("Markdown report", report_files, format_func=lambda p: p.name)
    st.markdown(selected.read_text(encoding="utf-8"))
    st.caption(str(selected))
    html_peer = selected.with_suffix(".html")
    if html_peer.exists():
        st.caption(f"HTML artifact: {html_peer}")


def _render_data_health(
    premarket: dict[str, Any] | None,
    positions: dict[str, Any] | None,
    data_root: Path,
) -> None:
    st.header("Data Health")
    if premarket is not None:
        st.json(premarket.get("metadata", {}))
    if positions is not None:
        st.json(positions.get("metadata", {}))
    quality_root = data_root / "reports" / "quality"
    quality_files = sorted(quality_root.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if quality_files:
        st.subheader("Latest Quality Report")
        st.caption(str(quality_files[-1]))
        st.json(json.loads(quality_files[-1].read_text(encoding="utf-8")))


def _render_live_journal(data_root: Path) -> None:
    database_path = data_root / "db" / "operations.sqlite"
    with OperationsRegistry(database_path) as store:
        open_positions = reconstruct_open_positions(store.manual_fills())
    rows = pd.DataFrame(position.to_dict() for position in open_positions)
    if rows.empty:
        st.info("No open manual positions in the SQLite journal.")
    else:
        st.subheader("Current SQLite Journal Positions")
        st.dataframe(rows, use_container_width=True)


if __name__ == "__main__":
    main()
