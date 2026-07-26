"""Human-facing daily decision report artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import pandas as pd


@dataclass(frozen=True)
class DailyReportArtifacts:
    """Persisted premarket report files for one signal session."""

    directory: Path
    json_path: Path
    csv_path: Path
    markdown_path: Path


def write_premarket_report(
    *,
    report: dict[str, Any],
    candidate_rows: list[dict[str, Any]],
    report_root: Path,
    signal_session: date,
    run_id: UUID,
) -> DailyReportArtifacts:
    """Write JSON, CSV, and Markdown artifacts for the daily workbench."""
    directory = report_root / signal_session.isoformat() / str(run_id)
    directory.mkdir(parents=True, exist_ok=True)

    json_path = directory / "premarket.json"
    csv_path = directory / "candidates.csv"
    markdown_path = directory / "premarket.md"

    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    pd.DataFrame(candidate_rows).to_csv(csv_path, index=False)
    markdown_path.write_text(_render_markdown(report, candidate_rows), encoding="utf-8")

    return DailyReportArtifacts(
        directory=directory,
        json_path=json_path,
        csv_path=csv_path,
        markdown_path=markdown_path,
    )


def write_position_check_report(
    *,
    report: dict[str, Any],
    rows: list[dict[str, Any]],
    report_root: Path,
    as_of: date,
    run_id: UUID,
) -> DailyReportArtifacts:
    """Write JSON, CSV, and Markdown artifacts for a position exit check."""
    directory = report_root / as_of.isoformat() / str(run_id)
    directory.mkdir(parents=True, exist_ok=True)

    json_path = directory / "positions.json"
    csv_path = directory / "positions.csv"
    markdown_path = directory / "positions.md"

    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    markdown_path.write_text(_render_positions_markdown(report, rows), encoding="utf-8")

    return DailyReportArtifacts(
        directory=directory,
        json_path=json_path,
        csv_path=csv_path,
        markdown_path=markdown_path,
    )


def _render_markdown(report: dict[str, Any], candidate_rows: list[dict[str, Any]]) -> str:
    metadata = report["metadata"]
    posture = report["portfolio_posture"]
    counts = report["counts"]
    lines = [
        f"# Premarket Plan — {metadata['signal_session']}",
        "",
        f"- Generated UTC: `{metadata['generated_at_utc']}`",
        f"- Earliest order session: `{metadata['earliest_order_session']}`",
        f"- Market regime: `{posture['market_regime']}`",
        f"- Posture: `{posture['status']}`",
        f"- Candidate count: `{counts['candidate_count']}`",
        f"- AI alpha candidates: `{counts['ai_candidate_count']}`",
        f"- Hedge overlay candidates: `{counts['hedge_candidate_count']}`",
        "",
        "## Operating note",
        "",
        posture["message"],
        "",
        "## Candidates",
        "",
    ]
    if not candidate_rows:
        lines.extend(
            [
                "No rules-approved candidates for this signal session.",
                "",
                "Keep cash discipline and review hedge overlay only if the broader regime or",
                "manual portfolio context calls for defense.",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "| Rank | Symbol | Source | Score | Close | Limit draft | Stop | "
            "Target | News | Action |",
            "|---:|---|---|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    for row in candidate_rows:
        lines.append(
            "| {rank} | {symbol} | {universe_role} | {score:.4f} | {signal_close:.2f} | "
            "{limit_price:.2f} | {stop_price:.2f} | {target_price:.2f} | "
            "{news_risk} | {recommended_action} |".format(**row)
        )
    lines.extend(
        [
            "",
            "## Broker order draft",
            "",
            "These are manual-entry drafts only. The system does not place orders.",
            "Cancel or defer any draft if pre-open/intraday gates show abnormal liquidity,",
            "large adverse gap, or high-severity news.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_positions_markdown(report: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    metadata = report["metadata"]
    counts = report["counts"]
    lines = [
        f"# Position Check — {metadata['as_of']}",
        "",
        f"- Generated UTC: `{metadata['generated_at_utc']}`",
        f"- Market regime: `{metadata['market_regime']}`",
        f"- Open positions: `{counts['open_position_count']}`",
        f"- Exit / reduce / review count: `{counts['action_required_count']}`",
        "",
        "## Positions",
        "",
    ]
    if not rows:
        lines.extend(["No open manual positions are recorded.", ""])
        return "\n".join(lines)

    lines.extend(
        [
            "| Symbol | Qty | Entry | Close | PnL % | Stop | Target | Held | Action | Reason |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    for row in rows:
        lines.append(
            "| {symbol} | {quantity} | {entry} | {close} | {pnl} | {stop} | "
            "{target} | {held} | {action} | {reason} |".format(
                symbol=row["symbol"],
                quantity=row["quantity"],
                entry=_money(row.get("average_entry_price")),
                close=_money(row.get("current_close")),
                pnl=_number(row.get("unrealized_return_pct")),
                stop=_money(row.get("stop_price")),
                target=_money(row.get("target_price")),
                held=row["sessions_held"],
                action=row["recommended_action"],
                reason=row["primary_reason"],
            )
        )
    lines.extend(
        [
            "",
            "These are decision-support checks only. Confirm orders manually in your broker.",
            "",
        ]
    )
    return "\n".join(lines)


def _money(value: object) -> str:
    return "-" if value is None else f"{float(value):.2f}"


def _number(value: object) -> str:
    return "-" if value is None else f"{float(value):.2f}"
