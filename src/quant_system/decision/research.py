"""Manual research-list workflow built on premarket calibration output."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pandas as pd

from quant_system.decision.premarket import run_premarket_workflow
from quant_system.models.config import RankingBaselineSettings
from quant_system.storage.parquet import ParquetRepository
from quant_system.strategy.config import BuyTheDipConfig

ACTIONABLE_ACTIONS = {
    "PREPARE_MANUAL_CONDITIONAL_ORDER",
    "RELAXED_WATCHLIST_REVIEW_ONLY",
    "REVIEW_AS_DEFENSIVE_OVERLAY",
}
REPAIR_PHASES = {"REPAIR_ATTEMPT", "CONFIRMED_REPAIR"}
BLOCK_ACTIONS = {
    "DEFER_AI_ALPHA_DEFENSIVE_CONTEXT",
    "WATCH_ONLY_SECTOR_CONFIRMATION_GATE",
    "WATCH_ONLY_RELAXED_QUALITY_GATE",
    "WATCH_ONLY_STRICT_CONTEXT",
}


@dataclass(frozen=True)
class ResearchListArtifacts:
    """Persisted research-list files for one signal session."""

    directory: Path
    json_path: Path
    csv_path: Path
    markdown_path: Path
    prompt_path: Path


def run_research_list_workflow(
    *,
    repository: ParquetRepository,
    database_path: Path,
    report_root: Path,
    universe_paths: tuple[Path, ...],
    signal_session: date,
    config: BuyTheDipConfig,
    include_news_risk: bool,
    news_lookback_hours: int,
    benchmark_path: Path,
    include_model_ranking: bool,
    model_settings: RankingBaselineSettings | None,
    max_symbols: int = 20,
    news_summary_days: int = 7,
    run_id: UUID | None = None,
    generated_at_utc: datetime | None = None,
) -> tuple[dict[str, Any], ResearchListArtifacts]:
    """Generate a wider manual research list and a news-summary prompt.

    The workflow does not change trading gates. It starts from the existing
    premarket calibration output and selects rows worth manual/news review.
    """
    run_id = run_id or uuid4()
    generated_at_utc = _ensure_utc(generated_at_utc or datetime.now(UTC))
    source_report, source_artifacts = run_premarket_workflow(
        repository=repository,
        database_path=database_path,
        report_root=report_root,
        universe_paths=universe_paths,
        signal_session=signal_session,
        config=config,
        include_news_risk=include_news_risk,
        news_lookback_hours=news_lookback_hours,
        include_calibration=True,
        benchmark_path=benchmark_path,
        include_model_ranking=include_model_ranking,
        model_settings=model_settings,
        report_stem="research_source",
        report_title="Research Source Premarket Plan",
        run_id=run_id,
        generated_at_utc=generated_at_utc,
    )
    rows = build_research_rows(source_report, max_symbols=max_symbols)
    report = _research_report(
        source_report=source_report,
        rows=rows,
        generated_at_utc=generated_at_utc,
        max_symbols=max_symbols,
        news_summary_days=news_summary_days,
    )
    report["source_artifacts"] = {
        "directory": str(source_artifacts.directory),
        "json": str(source_artifacts.json_path),
        "csv": str(source_artifacts.csv_path),
        "markdown": str(source_artifacts.markdown_path),
        "html": str(source_artifacts.html_path),
    }
    artifacts = write_research_list_artifacts(
        report=report,
        rows=rows,
        directory=source_artifacts.directory,
        news_summary_days=news_summary_days,
    )
    report["artifacts"] = {
        "directory": str(artifacts.directory),
        "json": str(artifacts.json_path),
        "csv": str(artifacts.csv_path),
        "markdown": str(artifacts.markdown_path),
        "prompt": str(artifacts.prompt_path),
    }
    artifacts = write_research_list_artifacts(
        report=report,
        rows=rows,
        directory=source_artifacts.directory,
        news_summary_days=news_summary_days,
    )
    return report, artifacts


def build_research_rows(
    source_report: dict[str, Any],
    *,
    max_symbols: int = 20,
) -> list[dict[str, Any]]:
    """Select a wider but capped research list from calibration rows."""
    calibration_rows = source_report.get("calibration_candidate_tiers", [])
    candidate_rows = source_report.get("candidates", [])
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in calibration_rows:
        research_row = _research_row(row, source="calibration")
        if research_row is None:
            continue
        _keep_best(by_symbol, research_row)
    for row in candidate_rows:
        research_row = _research_row(row, source="canonical")
        if research_row is None:
            continue
        _keep_best(by_symbol, research_row)

    rows = sorted(
        by_symbol.values(),
        key=lambda row: (
            _priority_rank(row["research_bucket"]),
            _model_rank_sort(row.get("model_rank")),
            -float(row.get("reversal_score") or 0.0),
            -float(row.get("score") or 0.0),
            str(row["symbol"]),
        ),
    )
    for rank, row in enumerate(rows[:max_symbols], start=1):
        row["research_rank"] = rank
    return rows[:max_symbols]


def write_research_list_artifacts(
    *,
    report: dict[str, Any],
    rows: list[dict[str, Any]],
    directory: Path,
    news_summary_days: int,
) -> ResearchListArtifacts:
    """Write research JSON, CSV, Markdown, and copy/paste prompt files."""
    json_path = directory / "research.json"
    csv_path = directory / "research_candidates.csv"
    markdown_path = directory / "research.md"
    prompt_path = directory / "news_research_prompt.md"

    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    markdown_path.write_text(_render_research_markdown(report, rows), encoding="utf-8")
    prompt_path.write_text(
        render_news_research_prompt(
            report=report,
            rows=rows,
            news_summary_days=news_summary_days,
        ),
        encoding="utf-8",
    )
    return ResearchListArtifacts(
        directory=directory,
        json_path=json_path,
        csv_path=csv_path,
        markdown_path=markdown_path,
        prompt_path=prompt_path,
    )


def render_news_research_prompt(
    *,
    report: dict[str, Any],
    rows: list[dict[str, Any]],
    news_summary_days: int = 7,
) -> str:
    """Render a prompt for a browsing/news-capable Codex session."""
    metadata = report["metadata"]
    context = report.get("strategy_context", {})
    reversal = context.get("reversal_context", {})
    symbols = ", ".join(row["symbol"] for row in rows) or "(no symbols)"
    lines = [
        "# Codex news research prompt",
        "",
        "请联网研究下面这组股票最近几天的新闻，并结合量化系统给出的",
        "技术/板块/轮动上下文，做手动交易辅助判断。",
        "",
        "重要边界：",
        "",
        "- 不要给自动下单建议。",
        "- 不要把新闻总结写成确定性预测。",
        "- 每条关键新闻都标注来源链接和发布日期。",
        "- 如果没有可靠新闻或催化，明确写“未发现可靠催化”。",
        "- 结论只分为：`重点研究`、`继续观察`、`暂时跳过`。",
        "",
        "运行上下文：",
        "",
        f"- Signal session: `{metadata['signal_session']}`",
        f"- Earliest order session: `{metadata['earliest_order_session']}`",
        f"- Research lookback: 最近 `{news_summary_days}` 天",
        f"- Strategy context: `{context.get('candidate_tier_context', 'UNKNOWN')}`",
        f"- Reversal status: `{reversal.get('status', 'UNKNOWN')}`",
        f"- Reversal action hint: `{reversal.get('action_hint', 'UNKNOWN')}`",
        f"- Symbols: {symbols}",
        "",
        "请按下面格式逐个股票输出：",
        "",
        "```markdown",
        "## <SYMBOL>",
        "",
        "- Research bucket: <系统给出的 bucket>",
        "- Quant context: <用 2-4 句话解释技术形态、板块确认、reversal、model rank>",
        "- News summary:",
        "  - <新闻 1，含来源和日期>",
        "  - <新闻 2，含来源和日期>",
        "- Catalyst check: <财报/指引/订单/监管/分析师评级/供应链/客户变化>",
        "- Risk check: <负面新闻、估值、流动性、板块拖累、宏观风险>",
        "- Integrated view: <新闻是否支持技术修复信号>",
        "- Manual conclusion: `重点研究` / `继续观察` / `暂时跳过`",
        "```",
        "",
        "待研究股票列表：",
        "",
        _prompt_table(rows),
    ]
    return "\n".join(lines) + "\n"


def _research_row(row: dict[str, Any], *, source: str) -> dict[str, Any] | None:
    bucket = _research_bucket(row, source=source)
    if bucket is None:
        return None
    return {
        "research_rank": None,
        "research_bucket": bucket,
        "symbol": row["symbol"],
        "universe_role": row.get("universe_role", ""),
        "source": source,
        "calibration_tier": row.get("calibration_tier", "BASELINE"),
        "context_tier": row.get("context_tier", ""),
        "calibration_action": row.get("calibration_action", row.get("recommended_action", "")),
        "manual_review_allowed": bool(row.get("manual_review_allowed", False)),
        "sector_confirmation_pass": bool(row.get("sector_confirmation_pass", True)),
        "sector_confirmation_reasons": row.get("sector_confirmation_reasons", ""),
        "reversal_phase": row.get("reversal_phase", ""),
        "reversal_score": row.get("reversal_score"),
        "reversal_reasons": row.get("reversal_reasons", ""),
        "model_rank": row.get("model_rank"),
        "model_score": row.get("model_score"),
        "model_rank_status": row.get("model_rank_status", ""),
        "benchmark_etf": row.get("benchmark_etf", ""),
        "score": row.get("score"),
        "signal_close": row.get("signal_close"),
        "news_risk": row.get("news_risk", ""),
        "research_reason": _research_reason(row, bucket),
    }


def _research_bucket(row: dict[str, Any], *, source: str) -> str | None:
    action = str(row.get("calibration_action") or row.get("recommended_action") or "")
    universe_role = str(row.get("universe_role") or "")
    reversal_phase = str(row.get("reversal_phase") or "")
    sector_ok = bool(row.get("sector_confirmation_pass", True))
    manual_review_allowed = bool(row.get("manual_review_allowed", False))

    if manual_review_allowed or action in ACTIONABLE_ACTIONS:
        return "ACTIONABLE_CANDIDATE"
    if source == "canonical" and action:
        return "ACTIONABLE_CANDIDATE"
    if reversal_phase in REPAIR_PHASES and sector_ok:
        return "RESEARCH_WATCHLIST"
    if universe_role in {"ai_alpha", "ai_satellite"} and (
        action in BLOCK_ACTIONS or not sector_ok or reversal_phase in REPAIR_PHASES
    ):
        return "BLOCKED_BUT_INTERESTING"
    if universe_role == "hedge_overlay" and reversal_phase in REPAIR_PHASES:
        return "DEFENSIVE_RESEARCH"
    return None


def _research_reason(row: dict[str, Any], bucket: str) -> str:
    reasons = [bucket]
    if row.get("manual_review_allowed"):
        reasons.append("manual_review_allowed")
    if row.get("reversal_phase"):
        reasons.append(f"reversal:{row['reversal_phase']}")
    if row.get("sector_confirmation_pass") is False:
        reasons.append("sector_not_confirmed")
    if row.get("model_rank") not in {None, ""}:
        reasons.append(f"model_rank:{row['model_rank']}")
    action = row.get("calibration_action") or row.get("recommended_action")
    if action:
        reasons.append(f"action:{action}")
    return ";".join(str(reason) for reason in reasons)


def _keep_best(rows: dict[str, dict[str, Any]], candidate: dict[str, Any]) -> None:
    symbol = str(candidate["symbol"])
    current = rows.get(symbol)
    if current is None or _row_sort_key(candidate) < _row_sort_key(current):
        rows[symbol] = candidate


def _row_sort_key(row: dict[str, Any]) -> tuple[int, int, float, float, str]:
    return (
        _priority_rank(row["research_bucket"]),
        _model_rank_sort(row.get("model_rank")),
        -float(row.get("reversal_score") or 0.0),
        -float(row.get("score") or 0.0),
        str(row["symbol"]),
    )


def _research_report(
    *,
    source_report: dict[str, Any],
    rows: list[dict[str, Any]],
    generated_at_utc: datetime,
    max_symbols: int,
    news_summary_days: int,
) -> dict[str, Any]:
    source_metadata = source_report["metadata"]
    return {
        "metadata": {
            "run_id": source_metadata["run_id"],
            "generated_at_utc": generated_at_utc.isoformat(),
            "signal_session": source_metadata["signal_session"],
            "data_cutoff_utc": source_metadata["data_cutoff_utc"],
            "earliest_order_session": source_metadata["earliest_order_session"],
            "earliest_order_time_utc": source_metadata["earliest_order_time_utc"],
            "decision_scope": "MANUAL_RESEARCH_ONLY_NOT_TRADE_PERMISSION",
            "max_symbols": max_symbols,
            "news_summary_days": news_summary_days,
        },
        "counts": {
            "research_candidate_count": len(rows),
            "actionable_count": sum(
                row["research_bucket"] == "ACTIONABLE_CANDIDATE" for row in rows
            ),
            "research_watchlist_count": sum(
                row["research_bucket"] == "RESEARCH_WATCHLIST" for row in rows
            ),
            "blocked_but_interesting_count": sum(
                row["research_bucket"] == "BLOCKED_BUT_INTERESTING" for row in rows
            ),
            "defensive_research_count": sum(
                row["research_bucket"] == "DEFENSIVE_RESEARCH" for row in rows
            ),
        },
        "portfolio_posture": source_report.get("portfolio_posture", {}),
        "strategy_context": source_report.get("strategy_context", {}),
        "model_rank_context": source_report.get("model_rank_context", {}),
        "research_candidates": rows,
    }


def _render_research_markdown(report: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    metadata = report["metadata"]
    counts = report["counts"]
    context = report.get("strategy_context", {})
    reversal = context.get("reversal_context", {})
    lines = [
        f"# Manual Research List — {metadata['signal_session']}",
        "",
        f"- Generated UTC: `{metadata['generated_at_utc']}`",
        f"- Decision scope: `{metadata['decision_scope']}`",
        f"- Strategy context: `{context.get('candidate_tier_context', 'UNKNOWN')}`",
        f"- Reversal status: `{reversal.get('status', 'UNKNOWN')}`",
        f"- Reversal action hint: `{reversal.get('action_hint', 'UNKNOWN')}`",
        f"- Research candidates: `{counts['research_candidate_count']}`",
        f"- Actionable: `{counts.get('actionable_count', 0)}`",
        f"- Research watchlist: `{counts.get('research_watchlist_count', 0)}`",
        f"- Blocked but interesting: `{counts.get('blocked_but_interesting_count', 0)}`",
        "",
        "This list is broader than the trade-action list. It is for manual news",
        "review and chart/context review only; it does not authorize automated or",
        "unconditional orders.",
        "",
    ]
    if not rows:
        lines.extend(["No research candidates for this session.", ""])
        return "\n".join(lines)
    lines.extend(
        [
            "## Candidates",
            "",
            "| Rank | Symbol | Bucket | Role | Tier | Context | Reversal | "
            "Repair score | Sector OK | Model rank | News | Reason |",
            "|---:|---|---|---|---|---|---|---:|---|---:|---|---|",
        ]
    )
    for row in rows:
        lines.append(
            "| {rank} | {symbol} | {bucket} | {role} | {tier} | {context} | "
            "{reversal} | {repair_score} | {sector_ok} | {model_rank} | "
            "{news} | {reason} |".format(
                rank=row["research_rank"],
                symbol=row["symbol"],
                bucket=row["research_bucket"],
                role=row["universe_role"],
                tier=row["calibration_tier"],
                context=row["context_tier"],
                reversal=row["reversal_phase"],
                repair_score=_format_optional_float(row.get("reversal_score")),
                sector_ok=_format_bool(row.get("sector_confirmation_pass")),
                model_rank=_format_optional_int(row.get("model_rank")),
                news=row.get("news_risk", ""),
                reason=row["research_reason"],
            )
        )
    lines.extend(
        [
            "",
            "Next step: copy `news_research_prompt.md` into a browsing-capable Codex",
            "session to summarize recent news and decide what deserves manual focus.",
            "",
        ]
    )
    return "\n".join(lines)


def _prompt_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No symbols selected._"
    lines = [
        "| Symbol | Bucket | Role | Context | Reversal | Sector OK | Model rank | Reason |",
        "|---|---|---|---|---|---|---:|---|",
    ]
    for row in rows:
        lines.append(
            "| {symbol} | {bucket} | {role} | {context} | {reversal} | {sector_ok} | "
            "{model_rank} | {reason} |".format(
                symbol=row["symbol"],
                bucket=row["research_bucket"],
                role=row["universe_role"],
                context=row["context_tier"],
                reversal=row["reversal_phase"],
                sector_ok=_format_bool(row.get("sector_confirmation_pass")),
                model_rank=_format_optional_int(row.get("model_rank")),
                reason=row["research_reason"],
            )
        )
    return "\n".join(lines)


def _priority_rank(bucket: str) -> int:
    return {
        "ACTIONABLE_CANDIDATE": 0,
        "RESEARCH_WATCHLIST": 1,
        "BLOCKED_BUT_INTERESTING": 2,
        "DEFENSIVE_RESEARCH": 3,
    }.get(bucket, 99)


def _model_rank_sort(value: Any) -> int:
    if value is None or value == "":
        return 999_999
    return int(value)


def _format_optional_int(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(int(value))


def _format_optional_float(value: Any) -> str:
    if value is None or value == "":
        return ""
    return f"{float(value):.4f}"


def _format_bool(value: Any) -> str:
    if value is None or value == "":
        return ""
    return "yes" if bool(value) else "no"


def _ensure_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
