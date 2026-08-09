"""Decision-support workflow for manual 2x leveraged ETF overlays."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from quant_system.backtest.workflow import load_feature_history
from quant_system.domain.clocks import NyseSessionClock
from quant_system.storage.parquet import ParquetRepository
from quant_system.strategy.leverage_overlay import (
    LeverageOverlayConfig,
    evaluate_leverage_overlay,
)


@dataclass(frozen=True)
class LeverageOverlayArtifacts:
    """Persisted files for one 2x overlay assessment."""

    directory: Path
    json_path: Path
    markdown_path: Path
    prompt_path: Path


def run_leverage_overlay_workflow(
    *,
    repository: ParquetRepository,
    database_path: Path,
    report_root: Path,
    signal_session: date,
    underlying_symbol: str,
    leveraged_etf_symbol: str | None,
    sector_etf: str,
    market_symbol: str,
    config: LeverageOverlayConfig,
    catalyst_confirmed: bool = False,
    run_id: UUID | None = None,
    generated_at_utc: datetime | None = None,
) -> tuple[dict[str, Any], LeverageOverlayArtifacts]:
    """Generate a manual 2x ETF overlay assessment."""
    run_id = run_id or uuid4()
    generated_at_utc = _ensure_utc(generated_at_utc or datetime.now(UTC))
    market_symbol = market_symbol.upper()
    sector_etf = sector_etf.upper()
    underlying_symbol = underlying_symbol.upper()
    leveraged_etf_symbol = leveraged_etf_symbol.upper() if leveraged_etf_symbol else None
    requested_symbols = tuple(
        symbol
        for symbol in (underlying_symbol, sector_etf, leveraged_etf_symbol)
        if symbol is not None
    )
    features = load_feature_history(
        repository=repository,
        database_path=database_path,
        symbols=requested_symbols,
        benchmark_symbol=market_symbol,
        start=signal_session,
        end=signal_session,
    )
    assessment = evaluate_leverage_overlay(
        features=features,
        signal_session=signal_session,
        underlying_symbol=underlying_symbol,
        leveraged_etf_symbol=leveraged_etf_symbol,
        sector_etf=sector_etf,
        market_symbol=market_symbol,
        config=config,
        catalyst_confirmed=catalyst_confirmed,
    )
    clock = NyseSessionClock()
    earliest_order_session = clock.next_session(signal_session)
    report = {
        "metadata": {
            "run_id": str(run_id),
            "generated_at_utc": generated_at_utc.isoformat(),
            "signal_session": signal_session.isoformat(),
            "data_cutoff_utc": clock.session_close_utc(signal_session).isoformat(),
            "earliest_order_session": earliest_order_session.isoformat(),
            "earliest_order_time_utc": clock.session_open_utc(
                earliest_order_session
            ).isoformat(),
            "decision_scope": "MANUAL_2X_OVERLAY_ONLY_NOT_MAIN_STRATEGY",
            "market_symbol": market_symbol,
            "sector_etf": sector_etf,
            "catalyst_confirmed": catalyst_confirmed,
        },
        "assessment": assessment.as_dict(),
        "discipline": {
            "main_strategy_boundary": (
                "This overlay does not modify Buy-the-Dip candidates, "
                "manual_review_allowed, paper fills, or broker orders."
            ),
            "forbidden_uses": [
                "bottom_fishing",
                "averaging_down",
                "fomo_reentry",
                "replacing_common_stock_core",
                "holding_after_setup_breaks",
            ],
        },
    }
    artifacts = write_leverage_overlay_artifacts(
        report=report,
        report_root=report_root,
        signal_session=signal_session,
        run_id=run_id,
    )
    report["artifacts"] = {
        "directory": str(artifacts.directory),
        "json": str(artifacts.json_path),
        "markdown": str(artifacts.markdown_path),
        "prompt": str(artifacts.prompt_path),
    }
    artifacts = write_leverage_overlay_artifacts(
        report=report,
        report_root=report_root,
        signal_session=signal_session,
        run_id=run_id,
    )
    return report, artifacts


def write_leverage_overlay_artifacts(
    *,
    report: dict[str, Any],
    report_root: Path,
    signal_session: date,
    run_id: UUID,
) -> LeverageOverlayArtifacts:
    """Write JSON, Markdown, and news/catalyst prompt artifacts."""
    directory = report_root / signal_session.isoformat() / str(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "leverage_overlay.json"
    markdown_path = directory / "leverage_overlay.md"
    prompt_path = directory / "leverage_overlay_prompt.md"

    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(render_leverage_overlay_markdown(report), encoding="utf-8")
    prompt_path.write_text(render_leverage_overlay_prompt(report), encoding="utf-8")
    return LeverageOverlayArtifacts(
        directory=directory,
        json_path=json_path,
        markdown_path=markdown_path,
        prompt_path=prompt_path,
    )


def render_leverage_overlay_markdown(report: dict[str, Any]) -> str:
    """Render the human-facing 2x overlay report."""
    metadata = report["metadata"]
    assessment = report["assessment"]
    lines = [
        f"# 2x Leverage Overlay — {assessment['underlying_symbol']}",
        "",
        f"- Generated UTC: `{metadata['generated_at_utc']}`",
        f"- Signal session: `{metadata['signal_session']}`",
        f"- Earliest order session: `{metadata['earliest_order_session']}`",
        f"- Decision scope: `{metadata['decision_scope']}`",
        f"- Underlying: `{assessment['underlying_symbol']}`",
        f"- 2x ETF: `{assessment.get('leveraged_etf_symbol') or 'not_provided'}`",
        f"- Sector ETF: `{assessment['sector_etf']}`",
        f"- Action: `{assessment['action']}`",
        f"- Score: `{assessment['score']}/{assessment['max_score']}`",
        f"- Setup type: `{assessment['setup_type']}`",
        f"- Entry reference: `{_money(assessment.get('entry_reference'))}`",
        f"- Stop reference: `{_money(assessment.get('stop_reference'))}`",
        f"- Target reference: `{_money(assessment.get('target_reference'))}`",
        f"- Stop pct: `{_percent(assessment.get('stop_pct'))}`",
        "",
        "## Checklist",
        "",
        "| Item | Pass | Message |",
        "|---|---|---|",
    ]
    for item in assessment.get("checklist", []):
        lines.append(
            f"| `{item['name']}` | {'yes' if item['passed'] else 'no'} | "
            f"{item['message']} |"
        )
    lines.extend(
        [
            "",
            "## Discipline",
            "",
            "This is a subsidiary manual overlay, not a main-strategy candidate.",
            "It must not be used for bottom-fishing, averaging down, FOMO re-entry,",
            "or replacing common-stock core exposure.",
            "",
        ]
    )
    return "\n".join(lines)


def render_leverage_overlay_prompt(report: dict[str, Any]) -> str:
    """Render a copy/paste prompt for catalyst/news validation."""
    metadata = report["metadata"]
    assessment = report["assessment"]
    symbol = assessment["underlying_symbol"]
    leveraged = assessment.get("leveraged_etf_symbol") or "未指定"
    lines = [
        "# Codex 2x ETF catalyst review prompt",
        "",
        "请联网研究下面的正股和对应 2x ETF，判断是否满足手动 2x overlay 的",
        "基本面/新闻催化要求。不要给自动下单建议。",
        "",
        "边界：",
        "",
        "- 只输出手动交易辅助判断，不要说“必须买入”。",
        "- 每条关键新闻必须包含来源链接和发布日期。",
        "- 如果没有可靠催化，明确写“未发现可靠催化”。",
        "- 结论只允许：`支持2x人工复核`、`只适合正股观察`、`不适合2x`。",
        "",
        "量化上下文：",
        "",
        f"- Signal session: `{metadata['signal_session']}`",
        f"- Underlying: `{symbol}`",
        f"- 2x ETF: `{leveraged}`",
        f"- Sector ETF: `{assessment['sector_etf']}`",
        f"- Overlay action before news: `{assessment['action']}`",
        f"- Checklist score: `{assessment['score']}/{assessment['max_score']}`",
        f"- Setup type: `{assessment['setup_type']}`",
        f"- Stop reference: `{_money(assessment.get('stop_reference'))}`",
        "",
        "请输出：",
        "",
        "1. 最近 3-7 天重要新闻和来源。",
        "2. 是否存在 earnings beat、guidance raise、订单、产品周期、行业价格、",
        "   AI capex、政策或供应链催化。",
        "3. 是否有负面新闻、估值、监管、流动性、财报日/高波动事件风险。",
        "4. 新闻是否支持当前技术趋势和行业确认。",
        "5. 最终结论：`支持2x人工复核` / `只适合正股观察` / `不适合2x`。",
        "",
    ]
    return "\n".join(lines)


def _money(value: Any) -> str:
    if value is None or value == "":
        return ""
    return f"{float(value):.2f}"


def _percent(value: Any) -> str:
    if value is None or value == "":
        return ""
    return f"{float(value) * 100:.2f}%"


def _ensure_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
