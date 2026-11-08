"""One-command daily workbench orchestration."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from quant_system.decision.digest import (
    DailyDigest,
    build_daily_digest,
    load_digest_states,
    resolve_digest_universe,
)
from quant_system.decision.digest_reports import publish_digest
from quant_system.decision.leverage import run_leverage_overlay_universe_workflow
from quant_system.decision.manual_research import export_manual_research_package
from quant_system.decision.positions import run_position_check_workflow
from quant_system.decision.premarket import run_premarket_workflow
from quant_system.decision.research import run_research_list_workflow
from quant_system.domain.clocks import NyseSessionClock
from quant_system.ingestion.market_context import select_market_context
from quant_system.models.config import RankingBaselineSettings
from quant_system.storage.parquet import ParquetRepository
from quant_system.strategy.config import BuyTheDipConfig
from quant_system.strategy.leverage_overlay import (
    LeverageOverlayConfig,
    LeverageOverlayUniverseConfig,
)
from quant_system.universe.config import load_daily_digest_config
from quant_system.universe.lifecycle import LifecycleResolver


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
    news_cutoff_utc: datetime | None = None,
    manual_news: bool = False,
    model_activation: dict[str, Any] | None = None,
    digest_config=None,
    digest_scope=None,
    market_context_values=None,
    context_cutoff_utc=None,
    publication_mode=None,
) -> tuple[dict[str, Any], DailyWorkbenchArtifacts]:
    """Run the daily decision stack and write a concise index."""
    run_id = run_id or uuid4()
    generated_at_utc = _ensure_utc(generated_at_utc or datetime.now(UTC))
    digest_config = digest_config or load_daily_digest_config()
    digest_scope = digest_scope or resolve_digest_universe(
        digest_config,
        as_of=signal_session,
        resolver=LifecycleResolver.from_repository(repository),
        universe_paths=None
        if tuple(universe_paths) == digest_config.universe_paths
        else universe_paths,
    )
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
        news_cutoff_utc=news_cutoff_utc,
        manual_news=manual_news,
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
        news_cutoff_utc=news_cutoff_utc,
        manual_news=manual_news,
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
    if model_activation is not None:
        report["model_activation"] = model_activation
    if manual_news:
        report["metadata"]["news_mode"] = "MANUAL_RESEARCH"
        report["manual_research"] = export_manual_research_package(
            report,
            premarket_artifacts.directory,
            strategy_config,
            lookback_days=news_summary_days,
        )
        report["next_steps"].insert(0, "Run the manual research prompt with live web search.")
    states = load_digest_states(
        repository=repository,
        database_path=database_path,
        config=digest_config,
        scope=digest_scope,
        session=signal_session,
    )
    report["digest"] = build_daily_digest(
        config=digest_config,
        scope=digest_scope,
        metadata=report["metadata"],
        premarket=premarket_report,
        states=states,
        now=generated_at_utc,
    ).model_dump(mode="json")
    context_cutoff_utc = (
        context_cutoff_utc or news_cutoff_utc or NyseSessionClock().available_at_utc(signal_session)
    )
    if context_cutoff_utc > generated_at_utc:
        raise ValueError("market context cutoff cannot exceed report generation time")
    if market_context_values is None:
        market_context_values = select_market_context(
            repository,
            database_path,
            session=signal_session,
            cutoff=context_cutoff_utc,
        )
    report["digest"]["metadata"]["market_context_cutoff_utc"] = context_cutoff_utc.isoformat()
    report["digest"]["market"].update(market_context_values)
    report["digest"]["data_health"]["market_context"] = {
        name: {"status": value["status"], "reason": value["reason"]}
        for name, value in market_context_values.items()
    }
    if any(value["status"] != "AVAILABLE" for value in market_context_values.values()):
        report["status"] = "DEGRADED"
    technical_health = {
        "unavailable_market": report["digest"]["market"]["technical_score"]["status"]
        != "AVAILABLE",
        "unavailable_stocks": sum(s["status"] == "UNAVAILABLE" for s in report["digest"]["stocks"]),
        "unavailable_sectors": sum(
            s["technical_score"]["status"] != "AVAILABLE" for s in report["digest"]["sectors"]
        ),
    }
    report["digest"]["data_health"]["technical"] = technical_health
    if any(technical_health.values()):
        report["status"] = "DEGRADED"
    report["digest"]["status"] = report["status"]
    publication_mode = publication_mode or (
        "live"
        if signal_session == NyseSessionClock().latest_completed_session(generated_at_utc)
        else "replay"
    )
    report["digest"]["metadata"].update(
        canonical_generated_at_utc=generated_at_utc.isoformat(),
        canonical_run_id=str(run_id),
        publication_mode=publication_mode,
        publication_kind="daily",
        data_root=str(repository.data_root),
        premarket_path=str(premarket_artifacts.json_path.resolve()),
        premarket_sha256=hashlib.sha256(premarket_artifacts.json_path.read_bytes()).hexdigest(),
        score_config_sha256=hashlib.sha256(
            (digest_config.model_dump_json() + strategy_config.model_dump_json()).encode()
        ).hexdigest(),
    )
    package = report.get("manual_research")
    if package is None:
        package = export_manual_research_package(
            report, premarket_artifacts.directory, strategy_config, lookback_days=news_summary_days
        )
        report["research_package"] = package
    report["digest"]["research"] = {
        "prompts": package.get("prompt_batches", []),
        "package_path": package["artifacts"]["package"],
        "import_instructions": (
            ".venv/bin/python -m quant_system decision import-manual-research "
            f"--package {shlex.quote(package['artifacts']['package'])} "
            "--result /path/to/batch-result.json --reviewed-by YOUR_NAME"
        ),
    }
    report["digest"]["artifacts"] = {
        name + "_json": str(component["artifacts"]["json"])
        for name, component in report["components"].items()
        if component.get("artifacts", {}).get("json")
    }
    report["digest"]["artifacts"].update(
        research_package=package["artifacts"]["package"],
        research_schema=package["artifacts"]["schema"],
    )
    digest_directory = (report_root / signal_session.isoformat() / str(run_id)).resolve()
    report["digest"]["artifacts"].update(
        digest_json=str(digest_directory / "digest.json"),
        digest_markdown=str(digest_directory / "digest.md"),
    )
    report["digest"] = DailyDigest.model_validate(report["digest"]).model_dump(mode="json")
    digest_artifacts = publish_digest(
        report["digest"], report_root=report_root, mode=publication_mode
    )
    artifacts = write_daily_workbench_index(report=report, report_root=report_root)
    report["artifacts"] = {
        "directory": str(artifacts.directory),
        "json": str(artifacts.json_path),
        "markdown": str(artifacts.markdown_path),
        "digest_json": digest_artifacts["json"],
        "digest_markdown": digest_artifacts["markdown"],
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
        row for row in leverage_rows if row.get("attention_status") != "NO_LEVERAGE_ATTENTION"
    ]
    position_rows = (positions_report or {}).get("positions", [])
    position_actions = [
        row for row in position_rows if row.get("recommended_action") not in {"", "HOLD", None}
    ]
    return {
        "metadata": {
            "run_id": str(run_id),
            "generated_at_utc": _ensure_utc(generated_at_utc).isoformat(),
            "signal_session": signal_session.isoformat(),
            "data_cutoff_utc": clock.available_at_utc(signal_session).isoformat(),
            "price_cutoff_utc": clock.available_at_utc(signal_session).isoformat(),
            "news_cutoff_utc": premarket_report.get("metadata", {}).get(
                "news_cutoff_utc", clock.available_at_utc(signal_session).isoformat()
            ),
            "earliest_order_session": earliest_order_session.isoformat(),
            "earliest_order_time_utc": clock.session_open_utc(earliest_order_session).isoformat(),
            "decision_scope": "DAILY_WORKBENCH_INDEX_ONLY_NOT_TRADE_PERMISSION",
        },
        "status": "DEGRADED"
        if premarket_report.get("data_health", {}).get("news", {}).get("status") == "DEGRADED"
        else "COMPLETED",
        "summary": {
            "execution_eligible_candidate_count": sum(
                row.get("execution_eligible", True)
                and row.get("recommended_action") == "PREPARE_MANUAL_CONDITIONAL_ORDER"
                for row in premarket_report.get("candidates", [])
            ),
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
        "data_health": premarket_report.get("data_health", {}),
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
    news_status = report.get("data_health", {}).get("news", {}).get("status", "UNKNOWN")
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
        f"- News data health: `{news_status}`",
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
    manual = report.get("manual_research")
    if manual:
        lines.extend(["", "## Manual news research", "", f"Status: `{manual['status']}`", ""])
        for name, path in manual["artifacts"].items():
            lines.append(f"- {name}: {_path_link(path)}")
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
        "data_health": report.get("data_health", {}),
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
