"""Decision-support workflow for manual 2x leveraged ETF overlays."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pandas as pd

from quant_system.backtest.workflow import load_feature_history
from quant_system.domain.clocks import NyseSessionClock
from quant_system.storage.parquet import ParquetRepository
from quant_system.strategy.leverage_overlay import (
    LeverageOverlayConfig,
    LeverageOverlayUniverseConfig,
    evaluate_leverage_overlay,
)


@dataclass(frozen=True)
class LeverageOverlayArtifacts:
    """Persisted files for one 2x overlay assessment."""

    directory: Path
    json_path: Path
    markdown_path: Path
    prompt_path: Path
    csv_path: Path | None = None


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


def run_leverage_overlay_universe_workflow(
    *,
    repository: ParquetRepository,
    database_path: Path,
    report_root: Path,
    signal_session: date,
    config: LeverageOverlayConfig,
    universe: LeverageOverlayUniverseConfig,
    catalyst_confirmed: bool = False,
    run_id: UUID | None = None,
    generated_at_utc: datetime | None = None,
) -> tuple[dict[str, Any], LeverageOverlayArtifacts]:
    """Generate batch manual 2x ETF overlay assessments."""
    run_id = run_id or uuid4()
    generated_at_utc = _ensure_utc(generated_at_utc or datetime.now(UTC))
    default_market_symbol = config.strategy.default_market_symbol.upper()
    feature_symbols = _feature_symbols_for_universe(universe, default_market_symbol)
    features = load_feature_history(
        repository=repository,
        database_path=database_path,
        symbols=feature_symbols,
        benchmark_symbol=default_market_symbol,
        start=signal_session,
        end=signal_session,
    )

    rows: list[dict[str, Any]] = []
    for member in universe.pairs:
        market_symbol = (member.market_symbol or default_market_symbol).upper()
        assessment = evaluate_leverage_overlay(
            features=features,
            signal_session=signal_session,
            underlying_symbol=member.underlying_symbol,
            leveraged_etf_symbol=member.leveraged_etf_symbol,
            sector_etf=member.sector_etf,
            market_symbol=market_symbol,
            config=config,
            catalyst_confirmed=catalyst_confirmed,
        )
        row = assessment.as_dict()
        row.update(
            {
                "tier": member.tier,
                "provider": member.provider,
                "product_type": member.product_type,
                "universe_role": member.universe_role,
                "market_symbol": market_symbol,
                "alternative_leveraged_etfs": list(member.alternative_leveraged_etfs),
                "notes": member.notes,
                "source_urls": list(member.source_urls),
            }
        )
        row["attention_status"] = _attention_status(row, config)
        rows.append(row)

    clock = NyseSessionClock()
    earliest_order_session = clock.next_session(signal_session)
    action_counts = {
        str(action): int(count)
        for action, count in pd.Series([row["action"] for row in rows])
        .value_counts()
        .items()
    }
    attention_counts = {
        str(status): int(count)
        for status, count in pd.Series([row["attention_status"] for row in rows])
        .value_counts()
        .items()
    }
    review_rows = [
        row
        for row in rows
        if row["action"] in {"ALLOW_MANUAL_REVIEW", "NEED_CATALYST_REVIEW"}
    ]
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
            "decision_scope": "MANUAL_2X_OVERLAY_BATCH_ONLY_NOT_MAIN_STRATEGY",
            "catalyst_confirmed": catalyst_confirmed,
            "universe_type": universe.universe_type,
            "pair_count": len(rows),
            "review_candidate_count": len(review_rows),
            "action_counts": action_counts,
            "attention_counts": attention_counts,
        },
        "assessments": sorted(
            rows,
            key=lambda row: (
                _action_rank(row["action"]),
                _attention_rank(row["attention_status"]),
                -int(row["score"]),
                row["tier"],
                row["underlying_symbol"],
            ),
        ),
        "discipline": {
            "main_strategy_boundary": (
                "This batch overlay does not modify Buy-the-Dip candidates, "
                "manual_review_allowed, paper fills, or broker orders."
            ),
            "product_due_diligence_required": (
                "The scan evaluates the underlying/security context. Before any "
                "manual trade, re-check the leveraged product's liquidity, spread, "
                "expense ratio, issuer notice, and daily-reset decay risk."
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
        stem="leverage_overlay_universe",
    )
    report["artifacts"] = {
        "directory": str(artifacts.directory),
        "json": str(artifacts.json_path),
        "markdown": str(artifacts.markdown_path),
        "prompt": str(artifacts.prompt_path),
        "csv": str(artifacts.csv_path) if artifacts.csv_path is not None else None,
    }
    artifacts = write_leverage_overlay_artifacts(
        report=report,
        report_root=report_root,
        signal_session=signal_session,
        run_id=run_id,
        stem="leverage_overlay_universe",
    )
    return report, artifacts


def write_leverage_overlay_artifacts(
    *,
    report: dict[str, Any],
    report_root: Path,
    signal_session: date,
    run_id: UUID,
    stem: str = "leverage_overlay",
) -> LeverageOverlayArtifacts:
    """Write JSON, Markdown, and news/catalyst prompt artifacts."""
    directory = report_root / signal_session.isoformat() / str(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{stem}.json"
    markdown_path = directory / f"{stem}.md"
    prompt_path = directory / f"{stem}_prompt.md"
    csv_path = directory / f"{stem}_candidates.csv" if "assessments" in report else None

    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(render_leverage_overlay_markdown(report), encoding="utf-8")
    prompt_path.write_text(render_leverage_overlay_prompt(report), encoding="utf-8")
    if csv_path is not None:
        _write_leverage_overlay_csv(report, csv_path)
    return LeverageOverlayArtifacts(
        directory=directory,
        json_path=json_path,
        markdown_path=markdown_path,
        prompt_path=prompt_path,
        csv_path=csv_path,
    )


def render_leverage_overlay_markdown(report: dict[str, Any]) -> str:
    """Render the human-facing 2x overlay report."""
    if "assessments" in report:
        return _render_leverage_overlay_universe_markdown(report)
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
    if "assessments" in report:
        return _render_leverage_overlay_universe_prompt(report)
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


def _render_leverage_overlay_universe_markdown(report: dict[str, Any]) -> str:
    metadata = report["metadata"]
    rows = report["assessments"]
    lines = [
        f"# 2x Leverage Overlay Batch — {metadata['signal_session']}",
        "",
        f"- Generated UTC: `{metadata['generated_at_utc']}`",
        f"- Signal session: `{metadata['signal_session']}`",
        f"- Earliest order session: `{metadata['earliest_order_session']}`",
        f"- Decision scope: `{metadata['decision_scope']}`",
        f"- Pair count: `{metadata['pair_count']}`",
        f"- Review candidates: `{metadata['review_candidate_count']}`",
        f"- Action counts: `{metadata['action_counts']}`",
        f"- Attention counts: `{metadata['attention_counts']}`",
        "",
        "## Candidate radar",
        "",
        (
            "| Action | Attention | Score | Tier | Underlying | 2x product | "
            "Sector | Setup | Stop % | Notes |"
        ),
        "|---|---|---:|---|---|---|---|---|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| `{row['action']}` | `{row['attention_status']}` | "
            f"{row['score']}/{row['max_score']} | "
            f"`{row['tier']}` | `{row['underlying_symbol']}` | "
            f"`{_leveraged_product_label(row)}` | `{row['sector_etf']}` | "
            f"`{row['setup_type']}` | `{_percent(row.get('stop_pct'))}` | "
            f"{row['notes']} |"
        )
    lines.extend(
        [
            "",
            "## Manual-only boundary",
            "",
            "This batch radar evaluates whether the underlying setup deserves manual",
            "2x product review. Generic rows intentionally do not name a product;",
            "they mean the underlying may be attractive for a separately chosen 2x",
            "expression. The scan does not evaluate live product spreads, liquidity,",
            "borrow/creation issues, tax suitability, or broker availability, and it",
            "must not change main-strategy candidates or paper fills.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_leverage_overlay_universe_prompt(report: dict[str, Any]) -> str:
    metadata = report["metadata"]
    rows = [
        row
        for row in report["assessments"]
        if row["action"] in {"ALLOW_MANUAL_REVIEW", "NEED_CATALYST_REVIEW"}
    ]
    if not rows:
        rows = [
            row
            for row in report["assessments"]
            if row["attention_status"] != "NO_LEVERAGE_ATTENTION"
        ][:12]
    if not rows:
        rows = report["assessments"][:8]
    lines = [
        "# Codex 2x ETF batch catalyst review prompt",
        "",
        "请联网研究下面的 2x 附属策略候选。不要给自动下单建议；只判断是否值得人工复核。",
        "",
        "边界：",
        "",
        "- 这是附属策略，不是主策略放行。",
        "- 每条关键新闻必须包含来源链接和发布日期。",
        "- 必须检查 2x 产品本身的流动性、点差、费用、发行方公告和 daily reset/decay 风险。",
        "- 结论只允许：`支持2x人工复核`、`只适合正股观察`、`不适合2x`。",
        "",
        "量化上下文：",
        "",
        f"- Signal session: `{metadata['signal_session']}`",
        f"- Earliest order session: `{metadata['earliest_order_session']}`",
        f"- Catalyst confirmed flag in scan: `{metadata['catalyst_confirmed']}`",
        "",
        "候选：",
        "",
        (
            "| Action | Attention | Score | Underlying | 2x ETF | Alternatives | "
            "Sector | Setup | Stop % |"
        ),
        "|---|---|---:|---|---|---|---|---|---:|",
    ]
    for row in rows:
        alternatives = ", ".join(row.get("alternative_leveraged_etfs", []))
        lines.append(
            f"| `{row['action']}` | `{row['attention_status']}` | "
            f"{row['score']}/{row['max_score']} | "
            f"`{row['underlying_symbol']}` | `{_leveraged_product_label(row)}` | "
            f"`{alternatives}` | `{row['sector_etf']}` | "
            f"`{row['setup_type']}` | `{_percent(row.get('stop_pct'))}` |"
        )
    lines.extend(
        [
            "",
            "请逐个输出：",
            "",
            "1. 最近 3-7 天重要新闻和来源。",
            "2. 是否存在 earnings beat、guidance raise、订单、产品周期、",
            "   行业价格、AI capex、政策或供应链催化。",
            "3. 是否有负面新闻、估值、监管、流动性、财报日/高波动事件风险。",
            "4. 2x 产品本身是否适合手动交易，尤其是成交量、点差、费用和 daily reset 风险。",
            "5. 最终结论：`支持2x人工复核` / `只适合正股观察` / `不适合2x`。",
            "",
        ]
    )
    return "\n".join(lines)


def _write_leverage_overlay_csv(report: dict[str, Any], csv_path: Path) -> None:
    rows = []
    for row in report.get("assessments", []):
        rows.append(
            {
                "signal_session": row["signal_session"],
                "action": row["action"],
                "attention_status": row["attention_status"],
                "score": row["score"],
                "max_score": row["max_score"],
                "tier": row["tier"],
                "universe_role": row["universe_role"],
                "product_type": row["product_type"],
                "provider": row["provider"],
                "underlying_symbol": row["underlying_symbol"],
                "leveraged_etf_symbol": row["leveraged_etf_symbol"] or "",
                "leverage_expression": _leveraged_product_label(row),
                "alternative_leveraged_etfs": ",".join(
                    row.get("alternative_leveraged_etfs", [])
                ),
                "market_symbol": row["market_symbol"],
                "sector_etf": row["sector_etf"],
                "setup_type": row["setup_type"],
                "entry_reference": row["entry_reference"],
                "stop_reference": row["stop_reference"],
                "target_reference": row["target_reference"],
                "stop_pct": row["stop_pct"],
                "risk_reward_estimate": row["risk_reward_estimate"],
                "failed_items": ",".join(
                    item["name"] for item in row.get("checklist", []) if not item["passed"]
                ),
                "reasons": " | ".join(row.get("reasons", [])),
                "notes": row["notes"],
            }
        )
    pd.DataFrame(rows).to_csv(csv_path, index=False)


def _feature_symbols_for_universe(
    universe: LeverageOverlayUniverseConfig,
    default_market_symbol: str,
) -> tuple[str, ...]:
    symbols: set[str] = {default_market_symbol.upper()}
    for member in universe.pairs:
        symbols.add(member.underlying_symbol)
        symbols.add(member.sector_etf)
        symbols.add((member.market_symbol or default_market_symbol).upper())
    return tuple(sorted(symbols))


def _leveraged_product_label(row: dict[str, Any]) -> str:
    if row.get("leveraged_etf_symbol"):
        return str(row["leveraged_etf_symbol"])
    if row.get("product_type") == "generic_2x_watch":
        return "generic_2x_watch"
    return "not_provided"


def _attention_status(row: dict[str, Any], config: LeverageOverlayConfig) -> str:
    if row["action"] in {"ALLOW_MANUAL_REVIEW", "NEED_CATALYST_REVIEW"}:
        return "FORMAL_2X_REVIEW"
    if (
        row.get("product_type") == "generic_2x_watch"
        and int(row["score"]) >= config.strategy.common_stock_preferred_score
    ):
        return "GENERIC_2X_RISKON_WATCH"
    if int(row["score"]) >= config.strategy.common_stock_preferred_score:
        return "UNDERLYING_RISKON_WATCH"
    return "NO_LEVERAGE_ATTENTION"


def _attention_rank(status: str) -> int:
    return {
        "FORMAL_2X_REVIEW": 0,
        "GENERIC_2X_RISKON_WATCH": 1,
        "UNDERLYING_RISKON_WATCH": 2,
        "NO_LEVERAGE_ATTENTION": 3,
    }.get(status, 9)


def _action_rank(action: str) -> int:
    return {
        "ALLOW_MANUAL_REVIEW": 0,
        "NEED_CATALYST_REVIEW": 1,
        "COMMON_STOCK_PREFERRED": 2,
        "NO_2X_TRADE": 3,
    }.get(action, 9)


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
