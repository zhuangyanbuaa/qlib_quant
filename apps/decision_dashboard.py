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
    premarket = _latest_json(date_root, "premarket.json")
    positions = _latest_json(date_root, "positions.json")

    tab_today, tab_candidates, tab_portfolio, tab_reports, tab_data = st.tabs(
        ["Today", "Candidates", "Portfolio", "Reports", "Data Health"]
    )
    with tab_today:
        _render_today(premarket, positions)
    with tab_candidates:
        _render_candidates(premarket)
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


def _render_today(
    premarket: dict[str, Any] | None,
    positions: dict[str, Any] | None,
) -> None:
    st.header("Today")
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
    if positions is not None:
        position_counts = positions["counts"]
        col1, col2, col3 = st.columns(3)
        col1.metric("Open Positions", position_counts["open_position_count"])
        col2.metric("Action Required", position_counts["action_required_count"])
        col3.metric("Hold", position_counts["hold_count"])


def _render_candidates(premarket: dict[str, Any] | None) -> None:
    st.header("Candidates")
    if premarket is None:
        st.warning("No candidate report available.")
        return
    candidates = pd.DataFrame(premarket.get("candidates", []))
    if candidates.empty:
        st.info("No rules-approved candidates in this report.")
        return
    st.dataframe(candidates, use_container_width=True)
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("By Universe Role")
        st.bar_chart(candidates["universe_role"].value_counts())
    with col2:
        st.subheader("By Recommended Action")
        st.bar_chart(candidates["recommended_action"].value_counts())


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
