"""Calendar anchored premarket, forward snapshot/gate, and weekly jobs."""

# ruff: noqa: RUF001

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from quant_system.backtest.workflow import load_price_snapshot
from quant_system.decision.gates import run_open_gate_workflow, run_preopen_refresh_workflow
from quant_system.domain.clocks import NyseSessionClock
from quant_system.ingestion.intraday import (
    collect_intraday,
    load_intraday_settings,
    select_intraday,
)
from quant_system.operations.digest_notifications import (
    notification_pages,
    paginate_lines,
    redact_pages,
)
from quant_system.operations.pipeline import run_daily_pipeline
from quant_system.operations.registry import PipelineBusy, pipeline_lock
from quant_system.operations.scheduling import ScheduleState, _page_budget, scheduler_lock
from quant_system.operations.telegram import TelegramClient
from quant_system.operations.weekly import build_weekly_review
from quant_system.quality.prices import PriceDataBlocked
from quant_system.settings import PROJECT_ROOT
from quant_system.storage.parquet import ParquetRepository


def session_windows(config, now):
    if now.utcoffset() is None:
        raise ValueError("job clock must be aware")
    now = now.astimezone(UTC)
    clock = NyseSessionClock()
    day = now.astimezone(ZoneInfo("America/New_York")).date()
    windows = []
    for label in clock.calendar.sessions_in_range(
        day - timedelta(days=10), day + timedelta(days=10)
    ):
        signal = label.date()
        order = clock.next_session(signal)
        opened = clock.session_open_utc(order)
        for job in sorted(set(config.session_jobs)):
            if job == "weekly":
                if signal.isocalendar()[:2] == order.isocalendar()[:2]:
                    continue
                due = clock.session_close_utc(signal) + timedelta(
                    minutes=config.after_close_minutes
                )
                windows.append(
                    dict(
                        kind=job,
                        signal_session=signal,
                        order_session=order,
                        due=due,
                        deadline=opened,
                    )
                )
            elif job == "premarket":
                due = opened - timedelta(minutes=config.premarket_minutes)
                windows.append(
                    dict(
                        kind=job,
                        signal_session=signal,
                        order_session=order,
                        due=due,
                        deadline=opened - timedelta(minutes=12),
                    )
                )
            else:
                minute = {"preopen": -10, "open30": 30, "open60": 60}[job]
                due = opened + timedelta(minutes=minute)
                windows += [
                    dict(
                        kind=job,
                        signal_session=signal,
                        order_session=order,
                        due=due,
                        deadline=due + timedelta(minutes=5),
                    ),
                    dict(
                        kind="snapshot_" + job,
                        signal_session=signal,
                        order_session=order,
                        due=due - timedelta(minutes=2),
                        deadline=due,
                    ),
                ]
    return sorted(windows, key=lambda item: (item["due"], item["kind"]))


def find_session_plan(root, *, signal_session, order_session, cutoff):
    eligible = []
    for path in (root / "reports/daily" / signal_session.isoformat()).glob("*/premarket.json"):
        try:
            report = json.loads(path.read_text())
            metadata = report["metadata"]
            generated = datetime.fromisoformat(metadata["generated_at_utc"])
            news = datetime.fromisoformat(
                metadata.get("news_cutoff_utc", metadata["generated_at_utc"])
            )
            if generated.utcoffset() is None or news.utcoffset() is None:
                continue
            if (
                metadata["signal_session"] != signal_session.isoformat()
                or metadata.get("earliest_order_session") != order_session.isoformat()
            ):
                continue
            if max(generated, news) > cutoff:
                continue
            eligible.append((generated, path, report))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return max(eligible, key=lambda item: (item[0], str(item[1]))) if eligible else None


def run_session_job(
    settings,
    config,
    window,
    *,
    now_utc=None,
    pipeline=run_daily_pipeline,
    collector=collect_intraday,
):
    now = now_utc or datetime.now(UTC)
    kind = window["kind"]
    root = settings.resolved_data_dir
    signal, order = window["signal_session"], window["order_session"]
    if kind == "premarket":
        return pipeline(
            settings,
            signal_session=signal,
            mode="live",
            manual_news=config.manual_news,
            ranking_policy=config.ranking,
            digest_config_path=config.digest_config_path,
            market_context_config_path=config.market_context_config_path,
            now_utc=now_utc,
        )
    if kind == "weekly":
        return build_weekly_review(settings, session=signal, cutoff=now)
    plan = find_session_plan(
        root, signal_session=signal, order_session=order, cutoff=min(now, window["due"])
    )
    if plan is None:
        return {"status": "BLOCKED", "blocked_reasons": ["matching_current_session_plan_missing"]}
    _, path, report = plan
    if signal != NyseSessionClock().latest_completed_session(now):
        return {"status": "BLOCKED", "blocked_reasons": ["expired_signal_session"]}
    with pipeline_lock(root):
        repo = ParquetRepository(root)
        # Canonical critical-price validation also applies to cached plans and snapshots.
        load_price_snapshot(
            repository=repo,
            database_path=root / "db/analytics.duckdb",
            symbols=tuple(row["symbol"] for row in report.get("candidates", [])),
            start=signal,
            end=signal,
        )
        if kind.startswith("snapshot_"):
            source = load_intraday_settings(
                config.intraday_config_path
                if config.intraday_config_path.is_absolute()
                else PROJECT_ROOT / config.intraday_config_path
            )
            return collector(
                settings,
                source,
                repo,
                symbols=tuple(row["symbol"] for row in report.get("candidates", [])),
                session=order,
                now_utc=now_utc,
            )
        rows = select_intraday(repo, session=order, cutoff=window["due"])
        directory = root / "reports/snapshots" / order.isoformat() / str(uuid4())
        directory.mkdir(parents=True, exist_ok=False)
        snapshot = directory / "snapshot.json"
        snapshot.write_text(json.dumps({"rows": rows}, default=str, indent=2))
        kwargs = dict(
            premarket_report_path=path,
            report_root=root / "reports/daily",
            snapshot_path=snapshot,
            generated_at_utc=now,
        )
        if kind == "preopen":
            result, artifacts = run_preopen_refresh_workflow(**kwargs)
        else:
            result, artifacts = run_open_gate_workflow(
                minutes=30 if kind == "open30" else 60, **kwargs
            )
        return {
            "status": "DEGRADED" if result["counts"]["defer_count"] else "COMPLETED",
            "summary": result["counts"],
            "digest": json.loads(Path(result["digest_artifacts"]["json"]).read_text())
            if result.get("digest_artifacts")
            else None,
            "gate_rows": result["rows"],
            "report_path": str(artifacts.json_path),
            "source_report_path": str(path),
            "gate_cutoff_utc": window["due"].isoformat(),
            "snapshot_status": "AVAILABLE"
            if rows
            else "MISSING"
            if report.get("candidates")
            else "NO_CANDIDATES",
        }


def session_notification_pages(output, window, *, max_units=3800):
    kind = window["kind"]
    label = {
        "premarket": "盘前日报刷新",
        "preopen": "开盘前检查",
        "open30": "T+30 门禁",
        "open60": "T+60 门禁",
        "weekly": "周度复盘",
    }.get(kind, kind)
    if kind == "premarket" or output.get("digest"):
        return notification_pages(
            output, window["signal_session"].isoformat(), label=label, max_units=max_units
        )
    summary = output.get("summary", {})
    lines = [f"订单日：{window['order_session']}", f"状态：{output.get('status', 'FAILED')}"]
    if kind == "weekly":
        lines += [
            f"日报：{summary.get('daily_report_count', 0)}/{summary.get('session_count', 0)}；"
            f"候选合计：{summary.get('candidate_count', 0)}",
            f"运行异常/降级：{summary.get('exception_count', 0)}；"
            f"待回填新闻包：{summary.get('pending_manual_research_count', 0)}",
        ]
    else:
        lines += [
            "完整评分报告暂不可用，暂停入场建议。",
            f"KEEP：{summary.get('keep_count', 0)}；DEFER：{summary.get('defer_count', 0)}；"
            f"CANCEL：{summary.get('cancel_count', 0)}",
        ]
        lines += [
            f"{r['symbol']}｜{r['decision']}｜技术分暂不可用" for r in output.get("gate_rows", [])
        ]
        if output.get("snapshot_status"):
            lines.append("盘中快照：" + output["snapshot_status"])
    lines += [str(reason) for reason in output.get("blocked_reasons", [])]
    if output.get("error_type"):
        lines.append("错误类型：" + output["error_type"])
    return paginate_lines(lines, title=f"{label} · {window['signal_session']}", max_units=max_units)


def session_notification(output, window):
    return "\n\n".join(session_notification_pages(output, window))


def tick_session_jobs(settings, config, *, now_utc=None, executor=run_session_job, client=None):
    now = (now_utc or datetime.now(UTC)).astimezone(UTC)
    if now_utc is not None and now_utc.utcoffset() is None:
        raise ValueError("job clock must be aware")
    windows = session_windows(config, now)
    active = [item for item in windows if item["due"] <= now < item["deadline"]]
    upcoming = [
        {
            "kind": item["kind"],
            "due_utc": item["due"].isoformat(),
            "due_tokyo": item["due"].astimezone(ZoneInfo("Asia/Tokyo")).isoformat(),
        }
        for item in windows
        if item["due"] > now
    ][:8]
    base = {"upcoming_jobs": upcoming}
    if not settings.telegram_configured:
        return {"status": "BLOCKED_CONFIGURATION", **base}
    try:
        with scheduler_lock(settings.resolved_data_dir):
            state = ScheduleState(settings.resolved_data_dir / "db/operations.sqlite")
            try:
                destination = hashlib.sha256(
                    (
                        settings.telegram_bot_token.get_secret_value()
                        + "|"
                        + settings.telegram_chat_id
                    ).encode()
                ).hexdigest()
                prefix = f"{config.name}|session|"
                for interrupted in state.recover(prefix=prefix):
                    kind = interrupted["job_key"].split("|")[-2]
                    if not kind.startswith("snapshot_"):
                        signal = date.fromisoformat(interrupted["session"])
                        recovered = {
                            "kind": kind,
                            "signal_session": signal,
                            "order_session": NyseSessionClock().next_session(signal),
                        }
                        text = session_notification(
                            {"status": "FAILED", "error_type": "InterruptedPipeline"}, recovered
                        )
                        state.enqueue(
                            f"{interrupted['job_key']}|{destination}|{interrupted['attempts']}",
                            destination,
                            text,
                            now,
                        )
                results = []
                for window in active:
                    if now_utc is None and datetime.now(UTC) >= window["deadline"]:
                        continue
                    kind = window["kind"]
                    key = f"{prefix}{kind}|{window['signal_session']}"
                    job = state.job(key, window["signal_session"].isoformat(), now)
                    if (
                        job["status"] != "DONE"
                        and job["attempts"] < config.pipeline_attempts
                        and datetime.fromisoformat(job["next_at"]) <= now
                    ):
                        delay = (
                            timedelta(seconds=60)
                            if kind.startswith("snapshot_")
                            else timedelta(minutes=1)
                            if kind in {"preopen", "open30", "open60"}
                            else timedelta(minutes=config.pipeline_backoff_minutes)
                        )
                        state.reserve(key, now + delay)
                        try:
                            output = executor(
                                settings,
                                config,
                                window,
                                now_utc=now if now_utc is not None else None,
                            )
                        except PriceDataBlocked as error:
                            output = {
                                "status": "BLOCKED",
                                "blocked_reasons": error.report.get("blocked_reasons", []),
                            }
                        except Exception as error:
                            output = {"status": "FAILED", "error_type": type(error).__name__}
                        output = {
                            k: v for k, v in output.items() if k not in {"stages", "data_health"}
                        }
                        output["observed_at_utc"] = (
                            now if now_utc is not None else datetime.now(UTC)
                        ).isoformat()
                        # Missing optional sources are terminal conservative observations.
                        completed = output.get("status") in {
                            "COMPLETED",
                            "DEGRADED",
                            "SUCCESS",
                            "SKIPPED",
                            "UNAVAILABLE",
                        }
                        state.finish(key, "DONE" if completed else "FAILED", output)
                        job = state.job(key, window["signal_session"].isoformat(), now)
                    output = json.loads(job["result_json"])
                    if output and not kind.startswith("snapshot_"):
                        pages = session_notification_pages(
                            output, window, max_units=_page_budget(config)
                        )
                        expires = (
                            window["deadline"] if kind in {"preopen", "open30", "open60"} else None
                        )
                        digest = output.get("digest") or {}
                        for stock in digest.get("entry_candidates", []):
                            until = stock.get("gate", {}).get("valid_until_utc")
                            if until and expires is not None:
                                expires = min(expires, datetime.fromisoformat(until))
                        state.enqueue_pages(
                            f"{key}|{destination}|{job['attempts']}",
                            destination,
                            redact_pages(pages, settings),
                            now,
                            expires_at=expires,
                        )
                    results.append(
                        {
                            "kind": kind,
                            "signal_session": window["signal_session"].isoformat(),
                            "status": job["status"],
                            "attempts": job["attempts"],
                            "result_status": output.get("status"),
                        }
                    )
                notifications = state.deliver(
                    client or TelegramClient(settings.telegram_bot_token),
                    settings.telegram_chat_id,
                    destination,
                    now,
                    config.notification_attempts,
                )
                if results:
                    log = settings.resolved_data_dir / "logs/automation-session.log"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    with log.open("a") as stream:
                        stream.write(
                            json.dumps({"time_utc": now.isoformat(), "jobs": results}) + "\n"
                        )
                return {
                    "status": "FAILED"
                    if any(item["status"] == "FAILED" for item in results)
                    else "COMPLETED"
                    if results
                    else "WAITING",
                    "jobs": results,
                    "notifications": notifications,
                    **base,
                }
            finally:
                state.close()
    except PipelineBusy:
        return {"status": "SKIPPED_BUSY", **base}
