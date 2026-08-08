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
    html_path: Path


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
    html_path = directory / "premarket.html"

    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    pd.DataFrame(candidate_rows).to_csv(csv_path, index=False)
    markdown = _render_markdown(report, candidate_rows)
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(_render_html("Premarket Plan", markdown), encoding="utf-8")

    return DailyReportArtifacts(
        directory=directory,
        json_path=json_path,
        csv_path=csv_path,
        markdown_path=markdown_path,
        html_path=html_path,
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
    html_path = directory / "positions.html"

    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    markdown = _render_positions_markdown(report, rows)
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(_render_html("Position Check", markdown), encoding="utf-8")

    return DailyReportArtifacts(
        directory=directory,
        json_path=json_path,
        csv_path=csv_path,
        markdown_path=markdown_path,
        html_path=html_path,
    )


def write_decision_table_report(
    *,
    report: dict[str, Any],
    rows: list[dict[str, Any]],
    report_root: Path,
    as_of: date,
    run_id: UUID,
    stem: str,
    title: str,
) -> DailyReportArtifacts:
    """Write generic JSON, CSV, Markdown, and HTML decision artifacts."""
    directory = report_root / as_of.isoformat() / str(run_id)
    directory.mkdir(parents=True, exist_ok=True)

    json_path = directory / f"{stem}.json"
    csv_path = directory / f"{stem}.csv"
    markdown_path = directory / f"{stem}.md"
    html_path = directory / f"{stem}.html"

    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    markdown = _render_generic_markdown(title, report, rows)
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(_render_html(title, markdown), encoding="utf-8")

    return DailyReportArtifacts(
        directory=directory,
        json_path=json_path,
        csv_path=csv_path,
        markdown_path=markdown_path,
        html_path=html_path,
    )


def _render_markdown(report: dict[str, Any], candidate_rows: list[dict[str, Any]]) -> str:
    metadata = report["metadata"]
    posture = report["portfolio_posture"]
    strategy_context = report.get("strategy_context", {})
    model_rank_context = report.get("model_rank_context", {})
    counts = report["counts"]
    lines = [
        f"# Premarket Plan — {metadata['signal_session']}",
        "",
        f"- Generated UTC: `{metadata['generated_at_utc']}`",
        f"- Earliest order session: `{metadata['earliest_order_session']}`",
        f"- Market regime: `{posture['market_regime']}`",
        f"- Posture: `{posture['status']}`",
        f"- Strategy context: `{strategy_context.get('candidate_tier_context', 'BASELINE')}`",
        f"- Candidate count: `{counts['candidate_count']}`",
        f"- AI alpha candidates: `{counts['ai_candidate_count']}`",
        f"- Hedge overlay candidates: `{counts['hedge_candidate_count']}`",
        "",
        "## Operating note",
        "",
        posture["message"],
        "",
        "## Strategy calibration",
        "",
        strategy_context.get(
            "message",
            "Calibration context unavailable; use canonical baseline rules.",
        ),
        "",
    ]
    if model_rank_context:
        lines.extend(
            [
                "## Model rank context",
                "",
                f"- Status: `{model_rank_context.get('status', 'DISABLED')}`",
                f"- Model: `{model_rank_context.get('model', 'n/a')}`",
                "- Decision scope: "
                f"`{model_rank_context.get('decision_scope', 'RANK_CONTEXT_ONLY')}`",
                f"- Training rows: `{model_rank_context.get('training_rows', 0)}`",
                f"- Scored rows: `{model_rank_context.get('scored_rows', 0)}`",
                "",
            ]
        )
    calibration_counts = report.get("calibration_counts")
    if calibration_counts:
        lines.extend(
            [
                f"- Calibration candidates: `{calibration_counts['calibration_candidate_count']}`",
                f"- Strict candidates: `{calibration_counts['strict_candidate_count']}`",
                f"- Baseline candidates: `{calibration_counts['baseline_candidate_count']}`",
                "- Relaxed-only candidates: "
                f"`{calibration_counts['relaxed_only_candidate_count']}`",
                f"- Manual-review allowed: `{calibration_counts['manual_review_allowed_count']}`",
                "",
            ]
        )
    calibration_rows = report.get("calibration_candidate_tiers", [])
    if calibration_rows:
        lines.extend(
            [
                "### Tiered candidates",
                "",
                "| Rank | Symbol | Role | Tier | Passed tiers | Score | "
                "Model rank | Review | Action |",
                "|---:|---|---|---|---|---:|---:|---|---|",
            ]
        )
        for row in calibration_rows:
            lines.append(
                "| {rank} | {symbol} | {role} | {tier} | {passed} | {score:.4f} | "
                "{model_rank} | {review} | {action} |".format(
                    rank=row["calibration_rank"],
                    symbol=row["symbol"],
                    role=row["universe_role"],
                    tier=row["calibration_tier"],
                    passed=row["passed_tiers"],
                    score=row["score"],
                    model_rank=_format_optional_int(row.get("model_rank")),
                    review="yes" if row["manual_review_allowed"] else "no",
                    action=row["calibration_action"],
                )
            )
        lines.append("")
    lines.extend(
        [
        "## Candidates",
        "",
        ]
    )
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
            "Target | Model rank | News | Action |",
            "|---:|---|---|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    for row in candidate_rows:
        lines.append(
            "| {rank} | {symbol} | {universe_role} | {score:.4f} | {signal_close:.2f} | "
            "{limit_price:.2f} | {stop_price:.2f} | {target_price:.2f} | "
            "{model_rank} | {news_risk} | {recommended_action} |".format(
                **{
                    **row,
                    "model_rank": _format_optional_int(row.get("model_rank")),
                }
            )
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


def _format_optional_int(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(int(value))


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


def _render_generic_markdown(
    title: str,
    report: dict[str, Any],
    rows: list[dict[str, Any]],
) -> str:
    metadata = report.get("metadata", {})
    lines = [f"# {title}", ""]
    for key, value in metadata.items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Summary", ""])
    for key, value in report.get("counts", {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Rows", ""])
    if not rows:
        lines.extend(["No rows.", ""])
        return "\n".join(lines)
    lines.extend(_markdown_table(rows))
    lines.append("")
    return "\n".join(lines)


def _markdown_table(rows: list[dict[str, Any]]) -> list[str]:
    columns = list(dict.fromkeys(column for row in rows for column in row))
    rendered = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        rendered.append(
            "| "
            + " | ".join(_format_markdown_cell(row.get(column)) for column in columns)
            + " |"
        )
    return rendered


def _format_markdown_cell(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|")


def _render_html(title: str, markdown: str) -> str:
    body = "\n".join(f"<p>{_escape_html(line)}</p>" for line in markdown.splitlines())
    return (
        "<!doctype html>\n"
        "<html><head>"
        '<meta charset="utf-8">'
        f"<title>{_escape_html(title)}</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "max-width:1100px;margin:32px auto;padding:0 20px;line-height:1.5;}"
        "p{margin:0.35rem 0;} code{background:#f4f4f5;padding:2px 4px;"
        "border-radius:4px;} table{border-collapse:collapse;width:100%;}"
        "th,td{border:1px solid #ddd;padding:6px;text-align:left;}"
        "th{background:#f8fafc;}"
        "</style></head><body>"
        f"{body}"
        "</body></html>\n"
    )


def _escape_html(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _money(value: object) -> str:
    return "-" if value is None else f"{float(value):.2f}"


def _number(value: object) -> str:
    return "-" if value is None else f"{float(value):.2f}"
