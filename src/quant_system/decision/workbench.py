"""One-command daily workbench orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from quant_system.decision.leverage import run_leverage_overlay_universe_workflow
from quant_system.decision.positions import run_position_check_workflow
from quant_system.decision.premarket import run_premarket_workflow
from quant_system.decision.research import run_research_list_workflow
from quant_system.domain.clocks import NyseSessionClock
from quant_system.models.config import RankingBaselineSettings
from quant_system.storage.parquet import ParquetRepository
from quant_system.strategy.config import BuyTheDipConfig
from quant_system.strategy.leverage_overlay import (
    LeverageOverlayConfig,
    LeverageOverlayUniverseConfig,
)


@dataclass(frozen=True)
class DailyWorkbenchArtifacts:
    """Persisted index files for one daily workbench run."""

    directory: Path
    json_path: Path
    markdown_path: Path


def run_daily_workbench_workflow(
    *,
    repository: ParquetRepository,
    database_path: Path,
    operations_database_path: Path,
    report_root: Path,
    signal_session: date,
    universe_paths: tuple[Path, ...],
    strategy_config: BuyTheDipConfig,
    include_news_risk: bool,
    news_lookback_hours: int,
    benchmark_path: Path,
    include_model_ranking: bool,
    model_settings: RankingBaselineSettings | None,
    leverage_config: LeverageOverlayConfig,
    leverage_universe: LeverageOverlayUniverseConfig,
    max_research_symbols: int = 20,
    news_summary_days: int = 7,
    include_positions: bool = True,
    run_id: UUID | None = None,
    generated_at_utc: datetime | None = None,
) -> tuple[dict[str, Any], DailyWorkbenchArtifacts]:
    """Run the daily decision stack and write a concise index."""
    run_id = run_id or uuid4()
    generated_at_utc = _ensure_utc(generated_at_utc or datetime.now(UTC))
    premarket_report, premarket_artifacts = run_premarket_workflow(
        repository=repository,
        database_path=database_path,
        report_root=report_root,
        universe_paths=universe_paths,
        signal_session=signal_session,
        config=strategy_config,
        include_news_risk=include_news_risk,
        news_lookback_hours=news_lookback_hours,
        include_calibration=True,
        benchmark_path=benchmark_path,
        include_model_ranking=include_model_ranking,
        model_settings=model_settings,
        run_id=run_id,
        generated_at_utc=generated_at_utc,
    )
    research_report, research_artifacts = run_research_list_workflow(
        repository=repository,
        database_path=database_path,
        report_root=report_root,
        universe_paths=universe_paths,
        signal_session=signal_session,
        config=strategy_config,
        include_news_risk=include_news_risk,
        news_lookback_hours=news_lookback_hours,
        benchmark_path=benchmark_path,
        include_model_ranking=include_model_ranking,
        model_settings=model_settings,
        max_symbols=max_research_symbols,
        news_summary_days=news_summary_days,
        run_id=run_id,
        generated_at_utc=generated_at_utc,
    )
    leverage_report, leverage_artifacts = run_leverage_overlay_universe_workflow(
        repository=repository,
        database_path=database_path,
        report_root=report_root,
        signal_session=signal_session,
        config=leverage_config,
        universe=leverage_universe,
        catalyst_confirmed=False,
        run_id=run_id,
        generated_at_utc=generated_at_utc,
    )
    positions_report: dict[str, Any] | None = None
    positions_artifacts: Any | None = None
    if include_positions:
        positions_report, positions_artifacts = run_position_check_workflow(
            repository=repository,
            database_path=database_path,
            operations_database_path=operations_database_path,
            report_root=report_root,
            as_of=signal_session,
            config=strategy_config,
            run_id=run_id,
            generated_at_utc=generated_at_utc,
        )

    report = build_daily_workbench_index(
        run_id=run_id,
        generated_at_utc=generated_at_utc,
        signal_session=signal_session,
        premarket_report=premarket_report,
        premarket_artifacts=premarket_artifacts,
        research_report=research_report,
        research_artifacts=research_artifacts,
        leverage_report=leverage_report,
        leverage_artifacts=leverage_artifacts,
        positions_report=positions_report,
        positions_artifacts=positions_artifacts,
    )
    artifacts = write_daily_workbench_index(report=report, report_root=report_root)
    report["artifacts"] = {
        "directory": str(artifacts.directory),
        "json": str(artifacts.json_path),
        "markdown": str(artifacts.markdown_path),
    }
    artifacts = write_daily_workbench_index(report=report, report_root=report_root)
    return report, artifacts


def build_daily_workbench_index(
    *,
    run_id: UUID,
    generated_at_utc: datetime,
    signal_session: date,
    premarket_report: dict[str, Any],
    premarket_artifacts: Any,
    research_report: dict[str, Any],
    research_artifacts: Any,
    leverage_report: dict[str, Any],
    leverage_artifacts: Any,
    positions_report: dict[str, Any] | None = None,
    positions_artifacts: Any | None = None,
) -> dict[str, Any]:
    """Build the in-memory daily index from component reports."""
    clock = NyseSessionClock()
    earliest_order_session = clock.next_session(signal_session)
    components = {
        "premarket": _component_summary(
            status="COMPLETED",
            report=premarket_report,
            artifacts=premarket_artifacts,
        ),
        "research": _component_summary(
            status="COMPLETED",
            report=research_report,
            artifacts=research_artifacts,
        ),
        "leverage_overlay": _component_summary(
            status="COMPLETED",
            report=leverage_report,
            artifacts=leverage_artifacts,
        ),
    }
    if positions_report is not None and positions_artifacts is not None:
        components["positions"] = _component_summary(
            status="COMPLETED",
            report=positions_report,
            artifacts=positions_artifacts,
        )
    else:
        components["positions"] = {"status": "SKIPPED"}

    research_rows = research_report.get("research_candidates", [])
    leverage_rows = leverage_report.get("assessments", [])
    leverage_attention_rows = [
        row
        for row in leverage_rows
        if row.get("attention_status") != "NO_LEVERAGE_ATTENTION"
    ]
    position_rows = (positions_report or {}).get("positions", [])
    position_actions = [
        row
        for row in position_rows
        if row.get("recommended_action") not in {"", "HOLD", None}
    ]
    return {
        "metadata": {
            "run_id": str(run_id),
            "generated_at_utc": _ensure_utc(generated_at_utc).isoformat(),
            "signal_session": signal_session.isoformat(),
            "data_cutoff_utc": clock.session_close_utc(signal_session).isoformat(),
            "earliest_order_session": earliest_order_session.isoformat(),
            "earliest_order_time_utc": clock.session_open_utc(
                earliest_order_session
            ).isoformat(),
            "decision_scope": "DAILY_WORKBENCH_INDEX_ONLY_NOT_TRADE_PERMISSION",
        },
        "summary": {
            "market_regime": premarket_report.get("portfolio_posture", {}).get(
                "market_regime",
                "UNKNOWN",
            ),
            "posture": premarket_report.get("portfolio_posture", {}).get(
                "status",
                "UNKNOWN",
            ),
            "candidate_count": premarket_report.get("counts", {}).get(
                "candidate_count",
                0,
            ),
            "research_candidate_count": research_report.get("counts", {}).get(
                "research_candidate_count",
                0,
            ),
            "leverage_pair_count": leverage_report.get("metadata", {}).get("pair_count", 0),
            "leverage_attention_count": len(leverage_attention_rows),
            "open_position_count": (positions_report or {})
            .get("counts", {})
            .get("open_position_count", 0),
            "position_action_required_count": len(position_actions),
        },
        "components": components,
        "top_research": research_rows[:10],
        "leverage_attention": leverage_attention_rows[:15],
        "position_actions": position_actions,
        "next_steps": _next_steps(
            premarket_report=premarket_report,
            research_rows=research_rows,
            leverage_attention_rows=leverage_attention_rows,
            position_actions=position_actions,
        ),
    }


def write_daily_workbench_index(
    *,
    report: dict[str, Any],
    report_root: Path,
) -> DailyWorkbenchArtifacts:
    """Write daily_index.json and daily_index.md into the shared run directory."""
    metadata = report["metadata"]
    directory = report_root / metadata["signal_session"] / metadata["run_id"]
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "daily_index.json"
    markdown_path = directory / "daily_index.md"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(render_daily_workbench_markdown(report), encoding="utf-8")
    return DailyWorkbenchArtifacts(
        directory=directory,
        json_path=json_path,
        markdown_path=markdown_path,
    )


def render_daily_workbench_markdown(report: dict[str, Any]) -> str:
    """Render the human-facing workbench index."""
    metadata = report["metadata"]
    summary = report["summary"]
    lines = [
        f"# Daily Workbench Index — {metadata['signal_session']}",
        "",
        f"- Generated UTC: `{metadata['generated_at_utc']}`",
        f"- Earliest order session: `{metadata['earliest_order_session']}`",
        f"- Decision scope: `{metadata['decision_scope']}`",
        f"- Market regime: `{summary['market_regime']}`",
        f"- Posture: `{summary['posture']}`",
        f"- Main candidates: `{summary['candidate_count']}`",
        f"- Research candidates: `{summary['research_candidate_count']}`",
        f"- 2x / leverage attention: `{summary['leverage_attention_count']}`",
        f"- Position actions: `{summary['position_action_required_count']}`",
        "",
        "## Reports",
        "",
        "| Component | Status | Markdown | CSV | Prompt | JSON |",
        "|---|---|---|---|---|---|",
    ]
    for name, component in report["components"].items():
        artifacts = component.get("artifacts", {})
        lines.append(
            "| {name} | {status} | {markdown} | {csv} | {prompt} | {json} |".format(
                name=name,
                status=component.get("status", ""),
                markdown=_path_link(artifacts.get("markdown")),
                csv=_path_link(artifacts.get("csv")),
                prompt=_path_link(artifacts.get("prompt")),
                json=_path_link(artifacts.get("json")),
            )
        )
    lines.extend(["", "## Next steps", ""])
    for step in report.get("next_steps", []):
        lines.append(f"- {step}")
    _append_research_table(lines, report.get("top_research", []))
    _append_leverage_table(lines, report.get("leverage_attention", []))
    _append_position_table(lines, report.get("position_actions", []))
    return "\n".join(lines) + "\n"


def _component_summary(*, status: str, report: dict[str, Any], artifacts: Any) -> dict[str, Any]:
    return {
        "status": status,
        "metadata": report.get("metadata", {}),
        "counts": report.get("counts", report.get("metadata", {})),
        "artifacts": _artifact_dict(artifacts),
    }


def _artifact_dict(artifacts: Any) -> dict[str, str | None]:
    return {
        "directory": str(artifacts.directory),
        "json": str(artifacts.json_path),
        "csv": str(getattr(artifacts, "csv_path", "")) or None,
        "markdown": str(artifacts.markdown_path),
        "html": str(getattr(artifacts, "html_path", "")) or None,
        "prompt": str(getattr(artifacts, "prompt_path", "")) or None,
    }


def _next_steps(
    *,
    premarket_report: dict[str, Any],
    research_rows: list[dict[str, Any]],
    leverage_attention_rows: list[dict[str, Any]],
    position_actions: list[dict[str, Any]],
) -> list[str]:
    steps: list[str] = []
    if position_actions:
        steps.append("Review position actions before considering new exposure.")
    if premarket_report.get("candidates"):
        steps.append("Review rules-approved premarket candidates and broker-order drafts.")
    if research_rows:
        steps.append("Copy news_research_prompt.md into a browsing Codex session.")
    if leverage_attention_rows:
        steps.append("Review leverage_overlay_universe_prompt.md for 2x/generic risk-on ideas.")
    if not steps:
        steps.append("No immediate action. Preserve cash discipline and re-check after new data.")
    return steps


def _append_research_table(lines: list[str], rows: list[dict[str, Any]]) -> None:
    lines.extend(["", "## Top research list", ""])
    if not rows:
        lines.extend(["No research candidates selected.", ""])
        return
    lines.extend(
        [
            "| Rank | Symbol | Bucket | Role | Context | Model rank | Reason |",
            "|---:|---|---|---|---|---:|---|",
        ]
    )
    for row in rows:
        lines.append(
            "| {rank} | {symbol} | {bucket} | {role} | {context} | {model_rank} | "
            "{reason} |".format(
                rank=row.get("research_rank", ""),
                symbol=row.get("symbol", ""),
                bucket=row.get("research_bucket", ""),
                role=row.get("universe_role", ""),
                context=row.get("context_tier", ""),
                model_rank=_optional_int(row.get("model_rank")),
                reason=row.get("research_reason", ""),
            )
        )


def _append_leverage_table(lines: list[str], rows: list[dict[str, Any]]) -> None:
    lines.extend(["", "## 2x / leverage attention", ""])
    if not rows:
        lines.extend(["No leverage-specific attention rows.", ""])
        return
    lines.extend(
        [
            "| Attention | Action | Score | Underlying | Expression | Sector | Setup |",
            "|---|---|---:|---|---|---|---|",
        ]
    )
    for row in rows:
        expression = row.get("leveraged_etf_symbol") or "generic_2x_watch"
        lines.append(
            "| {attention} | {action} | {score}/{max_score} | {underlying} | "
            "{expression} | {sector} | {setup} |".format(
                attention=row.get("attention_status", ""),
                action=row.get("action", ""),
                score=row.get("score", ""),
                max_score=row.get("max_score", ""),
                underlying=row.get("underlying_symbol", ""),
                expression=expression,
                sector=row.get("sector_etf", ""),
                setup=row.get("setup_type", ""),
            )
        )


def _append_position_table(lines: list[str], rows: list[dict[str, Any]]) -> None:
    lines.extend(["", "## Position actions", ""])
    if not rows:
        lines.extend(["No position action required.", ""])
        return
    lines.extend(
        [
            "| Symbol | Action | Reason | PnL % | Stop | Target |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    for row in rows:
        lines.append(
            "| {symbol} | {action} | {reason} | {pnl} | {stop} | {target} |".format(
                symbol=row.get("symbol", ""),
                action=row.get("recommended_action", ""),
                reason=row.get("primary_reason", ""),
                pnl=row.get("unrealized_return_pct", ""),
                stop=row.get("stop_price", ""),
                target=row.get("target_price", ""),
            )
        )


def _path_link(value: str | None) -> str:
    if not value:
        return ""
    path = Path(value)
    return f"[{path.name}]({path})"


def _optional_int(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(int(value))


def _ensure_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
