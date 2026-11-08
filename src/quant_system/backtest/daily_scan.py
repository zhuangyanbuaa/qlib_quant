"""Daily rule replay and ex-post signal research, never synthetic intraday fills."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pandas as pd

from quant_system.decision.digest import _technical_value, resolve_digest_universe
from quant_system.decision.hierarchy import HierarchyProxy, compute_symbol_states
from quant_system.domain.clocks import NyseSessionClock
from quant_system.features.point_in_time import build_point_in_time_features
from quant_system.quality.prices import (
    PriceDataBlocked,
    price_quality_mask,
    require_critical_prices,
)
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.strategy.buy_the_dip import BuyTheDipStrategy
from quant_system.universe.lifecycle import LifecycleResolver, apply_lifecycle_quality

HORIZONS = (1, 5, 10)
LIMITATIONS = [
    "当前关注池回溯全年存在选股和幸存者偏差；按配置生效日期筛选的结果另列。",
    "历史 Yahoo/legacy 数据的可用时间可能为推定值，且复权历史可能修订；不是当时实采数据证明。",
    "T+30/T+60、历史新闻及人工审核未重放；本日线研究不批准任何成交。",
    "未来收益为次日开盘至第 N 个交易日收盘的标签，不包含止损、止盈、组合仓位或真实成交。",
    "重叠信号不独立；上涨比例不是策略交易胜率。期末未成熟或缺失价格的标签为空。",
    "本次仅检验指定年内区间，没有调参，也不构成样本外有效性的证明。",
]


def visible_snapshot(versions: pd.DataFrame, session: date, clock=None) -> pd.DataFrame:
    """Filter availability BEFORE version selection; keep source metadata intact."""
    clock = clock or NyseSessionClock()
    visible = versions.loc[
        (pd.to_datetime(versions["available_at_utc"], utc=True) <= clock.available_at_utc(session))
        & (pd.to_datetime(versions["session_date_ny"]).dt.date <= session)
    ]
    order = ["fetched_at_utc", *(["filename"] if "filename" in visible else [])]
    return (
        visible.sort_values(order, kind="stable")
        .drop_duplicates(["symbol", "session_date_ny"], keep="last")
        .copy()
    )


def forward_outcomes(
    features: pd.DataFrame, *, symbol: str, session: date, end: date, clock=None
) -> dict:
    """Ex-post labels on exact exchange sessions, separate from decision inputs."""
    clock = clock or NyseSessionClock()
    entry = clock.next_session(session)
    dates = [entry]
    for _ in range(max(HORIZONS) - 1):
        dates.append(clock.next_session(dates[-1]))
    bars = features.loc[features["symbol"] == symbol].copy()
    bars.index = pd.to_datetime(bars["session_date_ny"]).dt.date
    result = {"outcome_reference_open_session": entry.isoformat()}
    for horizon in HORIZONS:
        target = dates[horizon - 1]
        needed = dates[:horizon]
        status, value = "AVAILABLE", None
        if target > end:
            status = "UNMATURED"
        elif not set(needed) <= set(bars.index):
            status = "MISSING_PRICE"
        elif not price_quality_mask(bars.loc[needed]).all():
            status = "UNRELIABLE_PRICE"
        else:
            value = float(bars.loc[target, "close"] / bars.loc[entry, "open"] - 1)
        result.update(
            {
                f"outcome_{horizon}d_status": status,
                f"outcome_{horizon}d_end": target.isoformat(),
                f"forward_return_{horizon}d": value,
            }
        )
    return result


def research_statistics(signals: pd.DataFrame, scope: str) -> dict:
    selected = signals.loc[signals[scope]] if len(signals) else signals
    result = {"signal_count": len(selected), "unique_symbols": 0, "horizons": {}}
    if len(selected):
        result["unique_symbols"] = selected["symbol"].nunique()
    for horizon in HORIZONS:
        values = selected.get(f"forward_return_{horizon}d", pd.Series(dtype=float)).dropna()
        result["horizons"][str(horizon)] = {
            "observations": len(values),
            "mean_return": float(values.mean()) if len(values) else None,
            "median_return": float(values.median()) if len(values) else None,
            "positive_fraction": float((values > 0).mean()) if len(values) else None,
        }
    return result


def run_daily_scan_research(
    *,
    repository,
    database_path: Path,
    report_root: Path,
    start: date,
    end: date,
    strategy_config,
    digest_config,
    progress=None,
) -> dict:
    """Replay local data without provider calls, parameter changes, or notifications."""
    clock = NyseSessionClock()
    if start > end:
        raise ValueError("start must not follow end")
    sessions = [s.date() for s in clock.calendar.sessions_in_range(start, end)]
    if not sessions:
        raise ValueError("requested range contains no NYSE sessions")
    if clock.available_at_utc(sessions[-1]) > datetime.now(UTC):
        raise ValueError("end must be a completed session")
    resolver = LifecycleResolver.from_repository(repository)
    scopes = {
        session: resolve_digest_universe(digest_config, as_of=session, resolver=resolver)
        for session in sessions
    }
    current = set(scopes[sessions[-1]].scan_symbols)
    members = current | {symbol for scope in scopes.values() for symbol in scope.scan_symbols}
    context = {proxy.symbol for proxy in digest_config.proxies} | {"^VIX", "SPY", "QQQ"}
    symbols = tuple(sorted(members | context))
    with DuckDBAnalytics(database_path, repository.daily_prices_root) as db:
        db.refresh_views()
        versions = db.query_price_versions(
            symbols,
            start_date=sessions[0] - timedelta(days=450),
            end_date=sessions[-1],
            cutoff_utc=clock.available_at_utc(sessions[-1]),
        )
    if versions.empty:
        raise ValueError("no historical prices available")
    if progress:
        progress(f"Loaded {len(versions)} immutable price versions; building causal features")
    features = build_point_in_time_features(versions, clock=clock)
    features = apply_lifecycle_quality(features, resolver)
    strategy = BuyTheDipStrategy(strategy_config.strategy)
    annotated = strategy.annotate(features)
    signals = {
        (signal.signal_session, signal.symbol): signal
        for signal in strategy.generate_signals(features)
        if start <= signal.signal_session <= end and signal.symbol in members
    }
    proxies = tuple(
        HierarchyProxy(s, "stock" if s in members else "context", "research", s, False)
        for s in symbols
        if s != "^VIX"
    )
    run_id = str(uuid4())
    directory = report_root / run_id
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "daily").mkdir()
    versions.to_parquet(directory / "price_versions.parquet", index=False)
    annotated.to_parquet(directory / "feature_audit.parquet", index=False)
    snapshots = {
        str(p): p.read_text() for p in (*digest_config.universe_paths, *digest_config.review_paths)
    }
    _write_json(
        directory / "configuration.json",
        {
            "strategy": strategy_config.model_dump(mode="json"),
            "digest": digest_config.model_dump(mode="json"),
            "universe_files": snapshots,
            "lifecycle": [e.model_dump(mode="json") for e in resolver.events],
        },
    )
    scan_rows, signal_rows, daily_rows = [], [], []
    proxy_groups = {p.symbol: p.group for p in digest_config.proxies}
    for index, session in enumerate(sessions):
        snapshot = visible_snapshot(versions, session, clock)
        tradable = {
            s: resolver.is_tradable(s, as_of=session, cutoff_utc=clock.available_at_utc(session))
            for s in symbols
        }
        snapshot["price_quality_pass"] = price_quality_mask(snapshot) & snapshot.symbol.map(
            tradable
        )
        blocked = []
        try:
            require_critical_prices(snapshot, session=session, clock=clock)
        except PriceDataBlocked as error:
            blocked = error.report["blocked_reasons"]
        states = compute_symbol_states(prices=snapshot, proxies=proxies, as_of=session)
        today = snapshot.loc[pd.to_datetime(snapshot.session_date_ny).dt.date == session]
        reliable = set(today.loc[price_quality_mask(today), "symbol"])
        today_features = annotated.loc[annotated.session_date_ny.dt.date == session].set_index(
            "symbol"
        )
        eligible = set(scopes[session].scan_symbols)
        day_rows = []
        for symbol in symbols:
            if symbol == "^VIX":
                continue
            state = states.get(symbol, {})
            state["price_status"] = "AVAILABLE" if symbol in reliable else "UNAVAILABLE"
            score = _technical_value(state)
            feature = today_features.loc[symbol] if symbol in today_features.index else None
            row = {
                "session": session.isoformat(),
                "symbol": symbol,
                "group": "stock" if symbol in members else proxy_groups.get(symbol, "context"),
                "historical_member": symbol in eligible,
                "current_member": symbol in current,
                "price_available": symbol in reliable,
                "score_status": score.status,
                "technical_score": score.value,
                "score_raw": state.get("score_raw"),
                "score_clamp_adjustment": state.get("score_clamp_adjustment"),
                "score_components": state.get("score_components", []),
                "trend_state": state.get("trend_state"),
                "market_regime": str(feature.market_regime) if feature is not None else "UNKNOWN",
                "data_cutoff_utc": clock.available_at_utc(session).isoformat(),
                "feature_ready": bool(feature.feature_ready) if feature is not None else False,
                "dip_condition": bool(feature.dip_condition) if feature is not None else False,
                "dip_yesterday": bool(feature.dip_yesterday) if feature is not None else False,
                "confirmation": bool(feature.confirmation) if feature is not None else False,
                "daily_rule_signal": (session, symbol) in signals and not blocked,
                "execution_status": "NO_FILL_DAILY_RESEARCH",
            }
            if row["daily_rule_signal"]:
                signal_rows.append(
                    {
                        **row,
                        **signals[session, symbol].model_dump(mode="json"),
                        **forward_outcomes(
                            features, symbol=symbol, session=session, end=end, clock=clock
                        ),
                    }
                )
            day_rows.append(row)
        market_scores = [r["technical_score"] for r in day_rows if r["symbol"] in {"SPY", "QQQ"}]
        vix = today.loc[today.symbol == "^VIX"]
        daily = {
            "session": session.isoformat(),
            "status": "BLOCKED" if blocked else "SCANNED",
            "blocked_reasons": blocked,
            "historical_members": len(eligible),
            "current_members": len(current),
            "current_prices_available": len(current & reliable),
            "historical_rule_signals": sum(
                r["daily_rule_signal"] and r["historical_member"] for r in day_rows
            ),
            "retrospective_rule_signals": sum(
                r["daily_rule_signal"] and r["current_member"] for r in day_rows
            ),
            "market_score": sum(market_scores) / 2
            if len(market_scores) == 2 and all(v is not None for v in market_scores)
            else None,
            "vix": float(vix.iloc[0].close) if "^VIX" in reliable else None,
            "cnn_fear_greed": None,
            "cnn_status": "NOT_REPLAYED",
            "execution_status": "NO_FILL_DAILY_RESEARCH",
            "filled_orders": 0,
        }
        _write_json(directory / "daily" / f"{session}.json", {**daily, "rows": day_rows})
        daily_rows.append(daily)
        scan_rows.extend(day_rows)
        if progress and (index % 20 == 0 or index == len(sessions) - 1):
            progress(f"Scanned {index + 1}/{len(sessions)} sessions through {session}")
    scans, signal_frame, daily_frame = map(pd.DataFrame, (scan_rows, signal_rows, daily_rows))
    if signal_frame.empty:
        signal_frame = pd.DataFrame(
            columns=[
                "session",
                "symbol",
                "historical_member",
                "current_member",
                "technical_score",
                "score",
                "execution_status",
                *[f"forward_return_{n}d" for n in HORIZONS],
            ]
        )
    scans.assign(score_components=scans.score_components.map(json.dumps)).to_parquet(
        directory / "daily_scans.parquet", index=False
    )
    scans.drop(columns=["score_components"]).to_csv(directory / "daily_scans.csv", index=False)
    _write_json(directory / "signals.json", signal_rows)
    signal_frame.drop(columns=["score_components"], errors="ignore").to_csv(
        directory / "signals.csv", index=False
    )
    daily_frame.to_csv(directory / "daily_summary.csv", index=False)
    summary = {
        "run_id": run_id,
        "status": "RESEARCH_COMPLETE_EXECUTION_NOT_VALIDATED",
        "start": str(sessions[0]),
        "end": str(sessions[-1]),
        "session_count": len(sessions),
        "current_symbol_count": len(current),
        "raw_version_count": len(versions),
        "blocked_session_count": sum(bool(d["blocked_reasons"]) for d in daily_rows),
        "retrospective": research_statistics(signal_frame, "current_member"),
        "historical_membership": research_statistics(signal_frame, "historical_member"),
        "execution": {
            "status": "NO_FILL_DAILY_RESEARCH",
            "filled_orders": 0,
            "strategy_return": None,
            "reason": "intraday_and_full_gates_not_replayed",
        },
        "limitations": LIMITATIONS,
        "report_directory": str(directory),
        "config_sha256": sha256((directory / "configuration.json").read_bytes()).hexdigest(),
    }
    _write_json(directory / "summary.json", summary)
    _write_markdown(directory, summary)
    return summary


def _write_json(path, payload):
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str, allow_nan=False),
        encoding="utf-8",
    )


def _write_markdown(directory, summary):
    lines = [
        "# 逐日扫描与信号研究",
        "",
        f"区间：{summary['start']} 至 {summary['end']}；共 {summary['session_count']} 个交易日。",
        f"当前关注股：{summary['current_symbol_count']}；"
        f"价格阻断日：{summary['blocked_session_count']}。",
        "",
        "完整交易策略回测：未验证。日线模式按 no-fill 处理，策略收益为 N/A，不能解读为 0% 收益。",
        "",
        "技术分为 Telegram 同一技术修复分（0–100）；"
        "信号中的 score 则是原策略的 60 日相对收益排名值。",
        "",
    ]
    for label, key in [
        ("当前关注池全年回溯（有选股偏差）", "retrospective"),
        ("按配置生效日期限定的关注池", "historical_membership"),
    ]:
        stats = summary[key]
        lines += [
            f"## {label}",
            "",
            f"信号 {stats['signal_count']} 个，涉及 {stats['unique_symbols']} 只股票。",
            "",
            "| 未来交易日 | 有效样本 | 平均收益 | 中位数 | 上涨比例 |",
            "|---|---:|---:|---:|---:|",
        ]
        for horizon, row in stats["horizons"].items():
            values = [
                "N/A" if row[k] is None else f"{row[k]:.2%}"
                for k in ("mean_return", "median_return", "positive_fraction")
            ]
            lines.append(f"| {horizon} | {row['observations']} | {' | '.join(values)} |")
        lines.append("")
    lines += [
        "## 解释与限制",
        "",
        *[f"- {text}" for text in LIMITATIONS],
        "",
        "## 文件",
        "",
        "- [每日汇总](daily_summary.csv)",
        "- [逐股扫描](daily_scans.csv)",
        "- [信号及未来收益标签](signals.csv)",
        "- 每个交易日的完整 JSON 在 `daily/`。",
        "- `price_versions.parquet` 保留完整来源元数据；"
        "`feature_audit.parquet` 保留计算输入及规则。",
        "- `configuration.json` 保存策略、关注池与生命周期事件快照。",
        "",
    ]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
