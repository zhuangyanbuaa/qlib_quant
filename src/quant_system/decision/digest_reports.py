"""Versioned digest artifacts and atomic, ordered daily reading entries."""

# ruff: noqa: RUF001

import fcntl
import hashlib
import html
import json
import os
import re
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from quant_system.decision.digest import DailyDigest
from quant_system.domain.clocks import NyseSessionClock

ENTRY_LABELS = {
    "GATE_PASSED": "本次门禁通过",
    "PENDING_NEWS": "待新闻审核",
    "PENDING_GATE": "待盘中确认",
    "PENDING_REVIEW": "待人工核验",
    "NOT_CANDIDATE": "规则未通过",
    "VETOED": "风险否决",
    "EXPIRED": "订单日已过期",
    "UNAVAILABLE": "无法评估",
    "INACTIVE": "不在有效范围",
    "PENDING_VERIFICATION": "标识待核验",
}
METRIC_LABELS = {
    "trend_state": "趋势状态",
    "return_5d": "5日动量",
    "return_20d": "20日动量",
    "relative_return_20d": "20日相对强弱",
    "above_ma20": "站上20日均线",
    "above_ma50": "站上50日均线",
    "ma20_slope_5d": "20日均线斜率",
    "reclaimed_previous_high": "突破前日高点",
    "rsi14": "RSI恢复",
    "drawdown_20d": "20日回撤惩罚",
    "drawdown_60d": "60日回撤惩罚",
    "down_day_ratio_20d": "连续下跌惩罚",
}

# Display names for the repository's internal watchlist theme identifiers.
THEME_LABELS = {
    "ai_hardware": "AI 硬件",
    "ai_software": "AI 软件",
    "ai_infrastructure": "AI 基础设施",
    "speculative_satellite": "高风险卫星关注池",
    "exclude_or_review": "排除或核验观察池",
    "ai_chips": "AI 芯片",
    "ai_platform_software": "AI 平台软件",
    "consumer_hardware_auto_platforms": "消费硬件与汽车",
    "crypto_beta": "加密资产相关",
    "crypto_compute_power": "加密算力与能源",
    "data_center_buildout": "数据中心建设",
    "data_center_compute": "数据中心计算",
    "data_center_power_cooling": "数据中心供电与散热",
    "defensive_financials": "防御金融",
    "defensive_healthcare": "防御医疗",
    "defensive_staples": "防御消费",
    "defensive_utilities": "防御公用事业",
    "edge_connectivity_semiconductors": "边缘连接半导体",
    "electrical_infrastructure_adr_review": "电气基建 ADR 核验",
    "high_beta_fintech": "高波动金融科技",
    "internet_consumer_platforms": "互联网消费平台",
    "internet_software_platforms": "互联网软件平台",
    "memory_storage": "存储芯片",
    "networking_optical": "网络与光通信",
    "power_analog_semiconductors": "功率与模拟芯片",
    "semiconductor_design_eda": "芯片设计与 EDA",
    "semiconductor_supply_chain": "半导体供应链",
    "space_satellite_technology": "太空与卫星",
    "uranium_nuclear_power": "铀与核电",
}


def sector_name(sector):
    return THEME_LABELS.get(sector.name, sector.name)


def _cell(value):
    if value is None:
        return "—"
    return (
        html.escape(str(value), quote=False)
        .replace("|", "&#124;")
        .replace("\n", " ")
        .replace("`", "&#96;")
    )


def _number(value):
    return "—" if value is None else f"{value:g}"


def _score(value):
    if value.value is None:
        return "暂不可用"
    return f"{value.value:g}" + ("（旧值）" if value.status == "STALE" else "")


def _points(value):
    return _score(value) + ("/100" if value.value is not None else "")


def _provenance(value):
    if not value.provenance:
        return []
    return [
        "",
        "输入来源与时点：",
        "```json",
        json.dumps(value.provenance, ensure_ascii=False, indent=2),
        "```",
        "",
    ]


def _link(path, base_dir, label):
    relative = os.path.relpath(Path(path).resolve(), Path(base_dir).resolve())
    return f"[{_cell(label)}]({quote(relative, safe='/.-_')})"


def render_digest_summary(digest):
    """Local preview text; delivery/pagination remains Phase C."""
    value = DailyDigest.model_validate(digest) if isinstance(digest, dict) else digest
    m = value.metadata
    lines = [
        f"美股日报｜信号日 {m['signal_session']}"
        f" → 计划进场日 {m.get('earliest_order_session', '—')}"
    ]
    if value.status in {"BLOCKED", "FAILED"}:
        return "\n".join([*lines, "数据异常，暂停入场建议", *value.blocked_reasons])
    lines += [
        "",
        "🌐 大盘",
        f"CNN 恐惧与贪婪：{_points(value.market.cnn_fear_greed)}",
        f"VIX：{_score(value.market.vix)}",
        f"系统技术分：{_points(value.market.technical_score)}｜市场状态：{value.market.regime}",
        "",
        "🏭 板块技术分",
    ]
    lines += [
        f"{sector_name(s)} {s.symbol}：{_points(s.technical_score)}"
        for s in value.sectors
        if s.group == "sector"
    ]
    themes = [s for s in value.sectors if s.group == "theme"]
    if themes:
        lines.append("关注主题：")
        lines += [
            f"{sector_name(s)} {s.symbol if s.proxy_kind == 'ETF' else '（关注池代理）'}："
            f"{_points(s.technical_score)}"
            for s in themes
        ]
    lines += ["", "🎯 关注股入场筛选", f"本次门禁通过：{len(value.entry_candidates)}"]
    lines += [
        f"{r.symbol} {_points(r.technical_score)}｜有效至 {r.gate.get('valid_until_utc', '—')}"
        for r in value.entry_candidates
    ]
    lines += [f"技术条件符合、待确认：{len(value.pending_candidates)}"]
    lines += [
        f"{r.symbol} {_points(r.technical_score)}｜"
        f"{ENTRY_LABELS.get(r.entry_status, r.entry_status)}"
        for r in value.pending_candidates
    ]
    if not value.entry_candidates and not value.pending_candidates:
        lines.append("暂无符合条件的入场候选")
    lines.append(
        f"覆盖：关注 {len(value.coverage)} 个；"
        f"完成评估 {sum(s.status == 'ASSESSED' for s in value.stocks)} 个；"
        f"待核验 {sum(r.status == 'PENDING_VERIFICATION' for r in value.coverage)} 个"
    )
    return "\n".join(lines)


def _details(details):
    lines = ["| 维度 | 输入 | 条件 / 阈值 | 原始贡献（0–10分制） |", "|---|---|---|---:|"]
    for part in details.get("score_components") or []:
        lines.append(
            f"| {_cell(METRIC_LABELS.get(part['metric'], part['metric']))} | "
            f"{_cell(part.get('value'))} | {_cell(part.get('condition'))} "
            f"{_cell(part.get('threshold'))} | {_number(part.get('contribution'))} |"
        )
    lines += [
        "",
        f"截断前：{_number(details.get('score_raw'))}；"
        f"截断调整：{_number(details.get('score_clamp_adjustment'))}；"
        "最终技术分 = 截断后原始分 × 10。",
        "",
    ]
    return lines


def render_digest_markdown(digest, *, base_dir, previous_success=None):
    value = DailyDigest.model_validate(digest) if isinstance(digest, dict) else digest
    meta = value.metadata
    lines = [
        f"# 美股日报 · {meta['signal_session']}",
        "",
        f"状态：{_cell(value.status)}",
        "",
        f"报告时间：{_cell(meta.get('generated_at_utc'))}；评估时点：{_cell(meta.get('evaluated_at_utc'))}。",
        f"价格截止：{_cell(meta.get('price_cutoff_utc'))}；市场指标截止：{_cell(meta.get('market_context_cutoff_utc'))}。",
        f"最早订单日：{_cell(meta.get('earliest_order_session'))}；"
        f"阶段：{_cell(meta.get('publication_kind', 'daily'))}。",
        "",
    ]
    if value.status in {"BLOCKED", "FAILED"}:
        lines += [
            "**数据异常，暂停入场建议。**",
            "",
            *[f"- {_cell(reason)}" for reason in value.blocked_reasons],
        ]
        if previous_success:
            lines += [
                "",
                "最近成功报告（旧结果，不是本次建议）："
                + _link(previous_success["markdown"], base_dir, previous_success["signal_session"]),
            ]
        return "\n".join(lines) + "\n"
    lines += [
        "## 今日摘要",
        "",
        "```text",
        render_digest_summary(value),
        "```",
        "",
        "技术分表示趋势修复程度，不是胜率或收益率；进场资格由原有策略与有效门禁决定。",
        "",
        "## 大盘指标与来源",
        "",
        "| 指标 | 数值 | 状态 | 来源 / 观测日 |",
        "|---|---:|---|---|",
    ]
    observations = (
        ("CNN 恐惧与贪婪", value.market.cnn_fear_greed),
        ("VIX", value.market.vix),
    )
    for name, observed in observations:
        provenance = observed.provenance
        lines.append(
            f"| {name} | {_score(observed)} | {_cell(observed.status)} | "
            f"{_cell(provenance.get('source', observed.reason))} / "
            f"{_cell(provenance.get('observation_date', '—'))} |"
        )
    for name, observed in observations:
        if observed.provenance:
            lines += ["", f"### {name} 来源", *_provenance(observed)]
    lines += [
        "",
        "### 系统大盘技术分",
        "",
        f"总分：{_points(value.market.technical_score)}。"
        "SPY、QQQ 等权，各占 50%；缺项不重新分配权重。",
        "",
    ]
    for symbol, score in value.market.benchmark_scores.items():
        lines += [f"#### {symbol}：{_points(score)}", ""]
        if score.value is None:
            lines += ["缺失原因：" + _cell(score.reason), ""]
        details = value.market.benchmark_details.get(symbol, {})
        if details.get("score_components"):
            lines += _details(details)
        lines += _provenance(score)
    lines += ["", "## 板块与主题明细", ""]
    for sector in value.sectors:
        proxy = "关注池等权代理，非全市场板块" if sector.proxy_kind == "WATCHLIST" else "ETF"
        lines += [
            f"### {_cell(sector_name(sector))} · {_cell(sector.symbol)}",
            "",
            f"技术分：{_points(sector.technical_score)}；"
            f"代理：{proxy}；"
            f"覆盖：{sector.valid_count}/{sector.total_count}。",
            "",
        ]
        if sector.members:
            lines += ["成员：" + ", ".join(_cell(s) for s in sector.members), ""]
        if sector.technical_score.value is None:
            lines += ["缺失原因：" + _cell(sector.technical_score.reason), ""]
        if sector.score_details.get("score_components"):
            lines += _details(sector.score_details)
        lines += _provenance(sector.technical_score)
    lines += [
        "## 全部关注个股",
        "",
        "| 代码 | 公司 | 技术分 /100 | 60日相对收益 | 入场状态 | 原因 |",
        "|---|---|---:|---:|---|---|",
    ]
    for stock in value.stocks:
        raw = "—" if stock.strategy_score is None else f"{stock.strategy_score:.2%}"
        lines.append(
            f"| {_cell(stock.symbol)} | {_cell(stock.company_name)} | "
            f"{_score(stock.technical_score)} | {raw} | "
            f"{_cell(ENTRY_LABELS.get(stock.entry_status, stock.entry_status))} | "
            f"{_cell(stock.reason)} |"
        )
    for stock in value.stocks:
        lines += [
            "",
            f"### {_cell(stock.symbol)} 评分与规则",
            "",
            "规则检查：" + _cell(stock.rule_checks),
            "",
            "新闻风险："
            + _cell(stock.news_risk)
            + "；模型状态 / 排名："
            + _cell(stock.model_context),
            "",
        ]
        if stock.score_details.get("score_components"):
            lines += _details(stock.score_details)
        if stock.plan:
            lines += ["计划参考（不是成交记录）：", "", "| 项目 | 数值 |", "|---|---|"]
            for key in (
                "signal_id",
                "signal_session",
                "earliest_order_time_utc",
                "limit_price",
                "max_gap_up_price",
                "gap_down_cancel_below",
                "stop_price",
                "target_price",
            ):
                lines.append(f"| {_cell(key)} | {_cell(stock.plan.get(key, '—'))} |")
        if stock.gate:
            lines += [
                "",
                "本次门禁证据（过期后须重新确认）：",
                "```json",
                json.dumps(stock.gate, ensure_ascii=False, indent=2),
                "```",
            ]
        if stock.technical_score.provenance:
            lines += [
                "",
                "价格输入来源：",
                "```json",
                json.dumps(stock.technical_score.provenance, ensure_ascii=False, indent=2),
                "```",
            ]
    if value.data_health.get("latest_gate"):
        lines += [
            "",
            "## 本次门禁检查（入场资格仍以筛选状态为准）",
            "",
            "```json",
            json.dumps(value.data_health["latest_gate"], ensure_ascii=False, indent=2),
            "```",
        ]
    lines += [
        "",
        "## LLM 联网研究 prompt",
        "",
        "按批复制以下完整内容。未知信息必须保留未知；研究结果不能覆盖原始分数或硬规则。",
        "",
    ]
    for index, prompt in enumerate(value.research.get("prompts", []), 1):
        text = prompt["text"]
        fence = "`" * max(4, max((len(s) + 1 for s in re.findall(r"`+", text)), default=4))
        lines += [
            f"### 第 {index} 批：{_cell(', '.join(prompt['symbols']))}",
            "",
            fence + "text",
            text,
            fence,
            "",
        ]
    if not value.research.get("prompts"):
        lines += ["本报告无待运行的研究 prompt。", ""]
    if value.research.get("import_instructions"):
        lines += ["导入方式：", "```text", value.research["import_instructions"], "```", ""]
    if value.research.get("review"):
        lines += [
            "### 已导入的人工研究（仅研究用途）",
            "",
            "```json",
            json.dumps(value.research["review"], ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    lines += [
        "## 覆盖与审计",
        "",
        "```json",
        json.dumps(meta, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    for row in value.coverage:
        lines.append(
            f"- {_cell(row.symbol)} → {_cell(row.resolved_symbol)}："
            f"{_cell(row.status)} / {_cell(row.reason)}；"
            f"来源：{_cell(', '.join(row.source_lists))}；主题：{_cell(', '.join(row.themes))}"
        )
    for name, path in value.artifacts.items():
        if path:
            lines += ["", _link(path, base_dir, name)]
    return "\n".join(lines) + "\n"


def _instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("digest timestamps must be aware")
    return parsed.astimezone(UTC)


def _validate(digest):
    meta = digest.metadata
    UUID(meta["run_id"])
    signal = date.fromisoformat(meta["signal_session"])
    clock = NyseSessionClock()
    generated = _instant(meta["generated_at_utc"])
    cutoff = _instant(meta["price_cutoff_utc"])
    if cutoff != clock.available_at_utc(signal) or cutoff > generated:
        raise ValueError("digest signal/cutoff/generation mismatch")
    if meta.get("earliest_order_session") != clock.next_session(signal).isoformat():
        raise ValueError("digest order session mismatch")
    for key in ("market_context_cutoff_utc", "evaluated_at_utc", "canonical_generated_at_utc"):
        if meta.get(key) and _instant(meta[key]) > generated:
            raise ValueError("digest contains future context")
    if digest.status in {"BLOCKED", "FAILED"} and (
        digest.entry_candidates or digest.pending_candidates
    ):
        raise ValueError("blocked digest cannot contain recommendations")


def _rank(digest):
    m = digest.metadata
    return (
        m["signal_session"],
        _instant(m.get("canonical_generated_at_utc", m["generated_at_utc"])),
        _instant(m.get("evaluated_at_utc", m["generated_at_utc"])),
        int(digest.status in {"BLOCKED", "FAILED"}),
    )


def _atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".digest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _immutable(path, text):
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise ValueError("digest run artifact conflict")
    else:
        _atomic(path, text)


def publish_digest(digest, *, report_root, mode):
    """Write complete immutable run evidence, then advance ordered reading aliases."""
    if mode not in {"live", "replay"}:
        raise ValueError("unsupported digest publication mode")
    digest = (
        DailyDigest.model_validate(digest)
        if isinstance(digest, dict)
        else digest.model_copy(deep=True)
    )
    digest.metadata["publication_mode"] = mode
    _validate(digest)
    root = Path(report_root).resolve()
    directory = root / digest.metadata["signal_session"] / digest.metadata["run_id"]
    paths = {"json": str(directory / "digest.json"), "markdown": str(directory / "digest.md")}
    digest.artifacts.update(digest_json=paths["json"], digest_markdown=paths["markdown"])
    payload = digest.model_dump_json(indent=2)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".digest-publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Validate all conflicts before changing any run artifact or alias.
        for path, content in (
            (Path(paths["json"]), payload),
            (Path(paths["markdown"]), render_digest_markdown(digest, base_dir=directory)),
        ):
            if path.exists() and path.read_text() != content:
                raise ValueError("digest run artifact conflict")
        _immutable(Path(paths["json"]), payload)
        _immutable(Path(paths["markdown"]), render_digest_markdown(digest, base_dir=directory))
        if mode == "live":
            for stem in (digest.metadata["signal_session"], "latest"):
                manifest_path = root / f"{stem}.json"
                previous = None
                if manifest_path.exists():
                    previous = json.loads(manifest_path.read_text())
                    raw = Path(previous["json"]).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != previous["sha256"]:
                        raise ValueError("published digest checksum mismatch")
                    prior = DailyDigest.model_validate_json(raw)
                    _validate(prior)
                    if _rank(prior) > _rank(digest) or (
                        _rank(prior) == _rank(digest)
                        and prior.metadata["run_id"] != digest.metadata["run_id"]
                    ):
                        _atomic(
                            root / f"{stem}.md",
                            render_digest_markdown(
                                prior, base_dir=root, previous_success=previous.get("last_success")
                            ),
                        )
                        continue
                success = (
                    {**paths, "signal_session": digest.metadata["signal_session"]}
                    if digest.status not in {"BLOCKED", "FAILED"}
                    else (previous or {}).get("last_success")
                )
                manifest = {
                    **paths,
                    "sha256": hashlib.sha256(payload.encode()).hexdigest(),
                    "last_success": success,
                }
                _atomic(manifest_path, json.dumps(manifest, indent=2))
                _atomic(
                    root / f"{stem}.md",
                    render_digest_markdown(digest, base_dir=root, previous_success=success),
                )
    return paths


def refresh_digest_after_event(*, report_root, premarket_path, event, kind):
    """Publish a new reading revision after gate/research evidence; never edit the parent."""
    from copy import deepcopy
    from uuid import uuid5

    from quant_system.backtest.workflow import load_price_snapshot
    from quant_system.decision.digest import classify_digest_entry
    from quant_system.quality.prices import PriceDataBlocked, price_quality_mask
    from quant_system.storage.parquet import ParquetRepository

    path = Path(premarket_path)
    index = path.parent / "daily_index.json"
    if not index.is_file():
        return None  # Legacy/standalone gate without a full daily digest.
    parent = json.loads(index.read_text()).get("digest")
    if not parent or parent.get("schema_version") != "2":
        return None
    digest = DailyDigest.model_validate(parent).model_copy(deep=True)
    meta = digest.metadata
    now = _instant(event["metadata"]["generated_at_utc"])
    if now < _instant(meta["generated_at_utc"]):
        return None
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta.get("premarket_sha256"):
        raise ValueError("digest canonical plan changed")
    premarket = json.loads(raw)
    if premarket["metadata"]["run_id"] != meta["canonical_run_id"]:
        raise ValueError("digest parent run mismatch")
    scope = Path(report_root) / meta["signal_session"]
    gates = []
    for candidate_path in scope.glob("*/open_gate_*.json"):
        try:
            gates.append(json.loads(candidate_path.read_text()))
        except (ValueError, OSError):
            continue
    if kind == "gate":
        gates.append(event)
        digest.data_health["latest_gate"] = {
            "metadata": deepcopy(event["metadata"]),
            "rows": deepcopy(event.get("rows", [])),
        }
    # Select only matching research revisions available at the evaluation instant.
    reviews = []
    for candidate_path in (path.parent / "manual_research").glob("*/combined_report.json"):
        try:
            review = json.loads(candidate_path.read_text())
            if (
                review["metadata"]["parent_run_id"] == meta["canonical_run_id"]
                and _instant(review["metadata"]["generated_at_utc"]) <= now
                and (
                    not review.get("observation")
                    or _instant(review["observation"]["available_at_utc"]) <= now
                )
            ):
                reviews.append(review)
        except (ValueError, KeyError, OSError):
            continue
    manual_risks = {}
    if reviews:
        review = max(reviews, key=lambda r: _instant(r["metadata"]["generated_at_utc"]))
        digest.research["review"] = {
            key: deepcopy(review[key])
            for key in (
                "metadata",
                "status",
                "overview",
                "evidence",
                "observation",
            )
        }
        digest.research["review"]["symbols"] = [
            {
                key: row.get(key)
                for key in (
                    "symbol",
                    "review",
                    "news_risk",
                    "review_status",
                )
            }
            for row in review["symbols"]
        ]
        manual_risks = {row["symbol"]: row["news_risk"] for row in review["symbols"]}
    assessments = {row["symbol"]: deepcopy(row) for row in premarket.get("symbol_assessments", [])}
    plans = {row["symbol"]: deepcopy(row) for row in premarket.get("candidates", [])}
    blocked = ()
    try:
        repository = ParquetRepository(Path(meta["data_root"]))
        session = date.fromisoformat(meta["signal_session"])
        frame = load_price_snapshot(
            repository=repository,
            database_path=repository.data_root / "db/analytics.duckdb",
            symbols=tuple(assessments),
            start=session,
            end=session,
        )
        valid = set(frame.loc[price_quality_mask(frame), "symbol"])
        for symbol, row in assessments.items():
            if symbol not in valid:
                row["price_status"] = "UNAVAILABLE"
    except PriceDataBlocked as error:
        blocked = tuple(error.report["blocked_reasons"])
    stocks = []
    for stock in digest.stocks:
        assessment = assessments.get(stock.resolved_symbol, {})
        candidate = plans.get(stock.resolved_symbol)
        risk = manual_risks.get(stock.resolved_symbol)
        if risk in {"HIGH", "MEDIUM"}:
            assessment = assessment | {"news_risk": risk}
            if candidate:
                candidate = candidate | {"news_risk": risk, "execution_eligible": False}
        entry, reason, gate = classify_digest_entry(
            candidate, assessment, premarket=premarket, now=now, gate_reports=gates
        )
        if stock.status != "ASSESSED":
            entry, reason = stock.entry_status, stock.reason
        if blocked:
            entry, reason, gate = "UNAVAILABLE", "critical_prices_blocked", {}
        stocks.append(
            stock.model_copy(
                update={
                    "entry_status": entry,
                    "reason": reason,
                    "gate": gate,
                    "news_risk": assessment.get("news_risk", stock.news_risk),
                }
            )
        )
    meta.update(
        generated_at_utc=now.isoformat(), evaluated_at_utc=now.isoformat(), publication_kind=kind
    )
    payload = {
        "metadata": meta,
        "stocks": [s.model_dump(mode="json") for s in stocks],
        "event": event,
        "blocked": blocked,
    }
    new_run = uuid5(
        UUID(meta["canonical_run_id"]), json.dumps(payload, sort_keys=True, default=str)
    )
    meta["run_id"] = str(new_run)
    # New immutable revision owns its own paths; retain original canonical sources.
    digest.artifacts.pop("digest_json", None)
    digest.artifacts.pop("digest_markdown", None)
    digest = digest.model_copy(
        update={
            "stocks": tuple(stocks),
            "blocked_reasons": blocked,
            "status": "BLOCKED" if blocked else digest.status,
            "entry_candidates": tuple(s for s in stocks if s.entry_status == "GATE_PASSED"),
            "pending_candidates": tuple(
                s
                for s in stocks
                if s.entry_status.startswith("PENDING_") and s.status == "ASSESSED"
            ),
        }
    )
    return publish_digest(digest, report_root=report_root, mode=meta["publication_mode"])
