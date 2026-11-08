"""Immutable weekly operational review with separate manual and paper facts."""

# ruff: noqa: RUF001

import json
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from quant_system.domain.clocks import NyseSessionClock
from quant_system.storage.parquet import ParquetRepository


def build_weekly_review(settings, *, session, cutoff):
    if cutoff.utcoffset() is None:
        raise ValueError("weekly cutoff must be aware")
    cutoff = cutoff.astimezone(UTC)
    monday = session - timedelta(days=session.weekday())
    clock = NyseSessionClock()
    sessions = [
        label.date().isoformat() for label in clock.calendar.sessions_in_range(monday, session)
    ]
    root = settings.resolved_data_dir
    reports = {}
    plans = []
    invalid = []
    for day in sessions:
        for path in (root / "reports/daily" / day).glob("*/*.json"):
            if path.stem not in {"daily_index", "premarket"}:
                continue
            try:
                report = json.loads(path.read_text())
                metadata = report["metadata"]
                generated = datetime.fromisoformat(metadata["generated_at_utc"])
                if (
                    generated.utcoffset() is None
                    or generated > cutoff
                    or metadata["signal_session"] != day
                ):
                    continue
                if path.stem == "daily_index":
                    if day not in reports or generated > reports[day][0]:
                        reports[day] = (generated, path, report)
                else:
                    plans.extend((generated, row) for row in report.get("candidates", []))
            except (OSError, ValueError, KeyError, TypeError):
                invalid.append(str(path))
    since = datetime.combine(monday, datetime.min.time(), ZoneInfo("America/New_York")).astimezone(
        UTC
    )
    facts = {"manual": [], "paper": [], "decisions": [], "scheduled": []}
    database = root / "db/operations.sqlite"
    if database.exists():
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            for key, table in [
                ("manual", "fills_manual"),
                ("paper", "paper_fills"),
                ("decisions", "manual_decisions"),
            ]:
                try:
                    for raw in db.execute(f"SELECT * FROM {table}"):
                        row = dict(raw)
                        created = datetime.fromisoformat(row["created_at_utc"])
                        event = datetime.fromisoformat(
                            row.get("fill_time_utc", row["created_at_utc"])
                        )
                        # Human decisions may be recorded on a later day; require both bounds.
                        if key == "decisions" and row["decision_session"] not in sessions:
                            continue
                        if (
                            created.utcoffset() is not None
                            and event.utcoffset() is not None
                            and since <= event <= cutoff
                            and created <= cutoff
                        ):
                            facts[key].append(row)
                except sqlite3.OperationalError:
                    pass
            with suppress(sqlite3.OperationalError):
                facts["scheduled"] = [
                    dict(row)
                    for row in db.execute(
                        "SELECT session,status,attempts,result_json FROM scheduled_daily_runs "
                        "WHERE session>=? AND session<=?",
                        (monday.isoformat(), session.isoformat()),
                    )
                ]

    def fill_summary(rows):
        slippage = []
        for fill in rows:
            matches = [
                (time, row)
                for time, row in plans
                if time <= datetime.fromisoformat(fill["fill_time_utc"])
                and row.get("signal_id") == fill.get("signal_id")
                and row.get("symbol") == fill["symbol"]
                and row.get("limit_price")
            ]
            reference = (
                max(matches, key=lambda item: item[0])[1]["limit_price"]
                if matches and fill["side"] == "BUY"
                else None
            )
            slippage.append(
                {
                    "fill_id": fill["fill_id"],
                    "symbol": fill["symbol"],
                    "side": fill["side"],
                    "plan_limit_price": reference,
                    "slippage_bps": (fill["price"] / reference - 1) * 10000 if reference else None,
                    "basis": "BUY_PRICE_VS_MATCHED_PLAN_LIMIT" if reference else "UNAVAILABLE",
                }
            )
        return {
            "fill_count": len(rows),
            "buy_count": sum(row["side"] == "BUY" for row in rows),
            "sell_count": sum(row["side"] == "SELL" for row in rows),
            "commission": sum(row["commission"] for row in rows),
            "slippage": slippage,
        }

    summaries = [report["summary"] for _, _, report in reports.values()]
    exceptions = []
    unattributed = 0
    for row in facts["scheduled"]:
        try:
            result = json.loads(row["result_json"])
            observed = datetime.fromisoformat(result["observed_at_utc"])
            if observed.utcoffset() is None or observed > cutoff:
                continue
        except (ValueError, KeyError, TypeError):
            unattributed += 1
            continue
        if (
            result.get("status") in {"FAILED", "BLOCKED", "DEGRADED", "UNAVAILABLE"}
            or row["status"] == "FAILED"
        ):
            exceptions.append(
                {
                    "session": row["session"],
                    "status": row["status"],
                    "pipeline_status": result.get("status"),
                    "attempts": row["attempts"],
                    "error_type": result.get("error_type"),
                }
            )
    reviewed = {
        row["package_id"]
        for row in ParquetRepository(root).read_manual_research().to_pylist()
        if max(row["available_at_utc"], row["fetched_at_utc"]) <= cutoff
    }
    packages = [
        report["manual_research"].get("package_id")
        for _, _, report in reports.values()
        if report.get("manual_research")
    ]
    summary = {
        "session_count": len(sessions),
        "daily_report_count": len(reports),
        "missing_daily_sessions": [day for day in sessions if day not in reports],
        "candidate_count": sum(row.get("candidate_count", 0) for row in summaries),
        "research_candidate_count": sum(
            row.get("research_candidate_count", 0) for row in summaries
        ),
        "execution_eligible_candidate_count": sum(
            row.get("execution_eligible_candidate_count", 0) for row in summaries
        ),
        "manual_research_package_count": len(packages),
        "pending_manual_research_count": sum(package not in reviewed for package in packages),
        "imported_manual_research_count": sum(package in reviewed for package in packages),
        "unattributed_operational_record_count": unattributed,
        "exception_count": len(exceptions),
        "manual_decision_count": len(facts["decisions"]),
    }
    report = {
        "metadata": {
            "run_id": str(uuid4()),
            "week_start": monday.isoformat(),
            "week_last_session": session.isoformat(),
            "cutoff_utc": cutoff.isoformat(),
            "scope": "OPERATIONAL_REVIEW_ONLY",
        },
        "status": "DEGRADED" if summary["missing_daily_sessions"] or invalid else "COMPLETED",
        "summary": summary,
        "daily_reports": [str(path) for _, path, _ in reports.values()],
        "manual": fill_summary(facts["manual"]),
        "paper": fill_summary(facts["paper"]),
        "exceptions": exceptions,
        "invalid_artifacts": invalid,
        "performance": {
            "status": "NOT_COMPUTED",
            "reason": "weekly_review_does_not_infer_closed_trade_pnl_or_win_rate",
        },
    }
    directory = root / "reports/weekly" / monday.isoformat() / report["metadata"]["run_id"]
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "weekly.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    markdown = directory / "weekly.md"
    markdown.write_text(
        "\n".join(
            [
                f"# 周度复盘 {monday} — {session}",
                f"状态：{report['status']}",
                f"日报：{len(reports)}/{len(sessions)}；候选合计：{summary['candidate_count']}；"
                f"研究候选合计：{summary['research_candidate_count']}",
                f"人工成交：{report['manual']['fill_count']}；"
                f"paper 成交：{report['paper']['fill_count']}",
                f"运行异常/降级：{len(exceptions)}；"
                f"待回填新闻包：{summary['pending_manual_research_count']}",
                "候选数按每日最新日报合计。回填仅证明导入已审核研究，不认证完整新闻覆盖。",
                "滑点仅比较已关联 BUY 成交价与计划限价；SELL 或无法关联时为 UNAVAILABLE。",
                "本周报未计算收益率、已平仓盈亏或胜率。",
                "",
                "日报路径：",
                *[str(p) for _, p, _ in reports.values()],
            ]
        ),
        encoding="utf-8",
    )
    return {
        "status": report["status"],
        "summary": summary,
        "report_path": str(path),
        "markdown_path": str(markdown),
    }
