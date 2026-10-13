"""One daily entry point; price gates remain canonical and news may degrade."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from quant_system.backtest.workflow import load_price_snapshot
from quant_system.decision.manual_research import load_manual_package
from quant_system.decision.premarket import load_decision_universe
from quant_system.decision.workbench import run_daily_workbench_workflow
from quant_system.domain.clocks import NyseSessionClock
from quant_system.ingestion.config import load_news_source_settings, load_price_source_settings
from quant_system.ingestion.factory import build_news_service, build_price_service
from quant_system.ingestion.source_health import check_sources
from quant_system.operations.registry import PipelineBusy, PipelineRegistry, pipeline_lock
from quant_system.quality.prices import PriceDataBlocked
from quant_system.settings import PROJECT_ROOT
from quant_system.storage.parquet import ParquetRepository
from quant_system.strategy.config import load_buy_the_dip_config
from quant_system.strategy.leverage_overlay import (
    load_leverage_overlay_config,
    load_leverage_overlay_universe_config,
)
from quant_system.universe.lifecycle import LifecycleResolver


def _raw_signature(repository):
    fingerprint = hashlib.sha256()
    for path in sorted((repository.data_root / "raw").rglob("*.parquet")):
        stat = path.stat()
        fingerprint.update(f"{path}|{stat.st_size}|{stat.st_mtime_ns}".encode())
    return fingerprint.hexdigest()


def _report_bundle_exists(path):
    try:
        report = json.loads(Path(path).read_text())
        bundles = [report.get("artifacts", {})] + [
            component.get("artifacts", {}) for component in report.get("components", {}).values()
        ]
        bundles.append(report.get("manual_research", {}).get("artifacts", {}))
        if report.get("manual_research"):
            load_manual_package(report["manual_research"]["artifacts"]["package"])
        return all(Path(value).exists() for bundle in bundles for value in bundle.values() if value)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def run_daily_pipeline(
    settings,
    *,
    signal_session=None,
    mode="live",
    universe_paths=None,
    price_config_path=None,
    news_config_path=None,
    refresh_window=None,
    retry=False,
    manual_news=False,
    now_utc=None,
    price_service=None,
    news_service=None,
    report_builder=run_daily_workbench_workflow,
):
    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None or mode not in {"live", "replay"}:
        raise ValueError("an aware time and live/replay mode are required")
    now = now.astimezone(UTC)
    clock = NyseSessionClock()
    session = signal_session or clock.latest_completed_session(now)
    if not clock.calendar.is_session(session):
        return {"status": "BLOCKED", "blocked_reasons": ["invalid_signal_session"]}
    paths = universe_paths or (
        PROJECT_ROOT / "configs/universe/ai_watchlist.yaml",
        PROJECT_ROOT / "configs/universe/hedge_overlay.yaml",
    )
    price_config_path = price_config_path or PROJECT_ROOT / "configs/sources/prices.yaml"
    news_config_path = news_config_path or PROJECT_ROOT / "configs/sources/news.yaml"
    price_config = load_price_source_settings(price_config_path)
    news_config = load_news_source_settings(news_config_path)
    if mode == "live" and (
        session != clock.latest_completed_session(now)
        or now >= clock.session_open_utc(clock.next_session(session))
    ):
        return {
            "status": "BLOCKED",
            "blocked_reasons": [
                "live_pipeline_requires_current_completed_session_before_next_open"
            ],
        }
    try:
        with pipeline_lock(settings.resolved_data_dir):
            repo = ParquetRepository(settings.resolved_data_dir)
            symbols, roles = load_decision_universe(
                tuple(paths), as_of=session, resolver=LifecycleResolver.from_repository(repo)
            )
            stocks = tuple(symbol for symbol in symbols if roles.get(symbol) != "benchmark")
            window = (
                (
                    refresh_window
                    or now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
                )
                if mode == "live"
                else clock.available_at_utc(session)
            )
            if window.tzinfo is None or window.utcoffset() is None or window > now:
                raise ValueError("refresh window must be aware and at/before run start")
            window = window.astimezone(UTC)
            current_window = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
            if mode == "live" and window != current_window:
                raise ValueError(
                    "live refresh window must equal the current UTC half-hour boundary"
                )
            fingerprint = hashlib.sha256()
            for path in [
                *paths,
                price_config_path,
                news_config_path,
                *sorted((PROJECT_ROOT / "src/quant_system").rglob("*.py")),
                *sorted((PROJECT_ROOT / "configs/strategy").glob("*.yaml")),
                PROJECT_ROOT / "configs/universe/benchmarks.yaml",
                PROJECT_ROOT / "configs/universe/leverage_overlay_universe.yaml",
            ]:
                fingerprint.update(str(path).encode())
                fingerprint.update(path.read_bytes())
            # Public readiness bits allow newly configured sources to create a fresh request.
            fingerprint.update(
                str(
                    (settings.alpha_vantage_key_configured, settings.sec_contact_configured)
                ).encode()
            )
            fingerprint.update(
                json.dumps(
                    [
                        row
                        for row in repo.read_company_mappings().to_pylist()
                        if row["available_at_utc"] <= now
                    ],
                    sort_keys=True,
                    default=str,
                ).encode()
            )
            key = hashlib.sha256(
                f"{mode}|{manual_news}|{session}|{window.isoformat()}|{fingerprint.hexdigest()}".encode()
            ).hexdigest()
            registry = PipelineRegistry(settings.resolved_data_dir / "db/operations.sqlite")
            try:
                run = registry.start(key)
                summary = json.loads(run["summary_json"])
                strategy_config = load_buy_the_dip_config(
                    PROJECT_ROOT / "configs/strategy/buy_the_dip.yaml"
                )

                def validate():
                    load_price_snapshot(
                        repository=repo,
                        database_path=settings.resolved_data_dir / "db/analytics.duckdb",
                        symbols=symbols,
                        start=session,
                        end=session,
                        benchmark_symbol=strategy_config.strategy.benchmark_symbol,
                    )
                    return {
                        "status": "SUCCESS",
                        "price_cutoff_utc": clock.available_at_utc(session).isoformat(),
                    }

                if (
                    not retry
                    and run["status"] in {"COMPLETED", "DEGRADED"}
                    and summary.get("report_path")
                    and _report_bundle_exists(summary["report_path"])
                    and summary.get("raw_signature") == _raw_signature(repo)
                ):
                    try:
                        validate()
                    except PriceDataBlocked:
                        pass
                    else:
                        return summary | {"reused": True}
                run_id = run["run_id"]
                registry.finish(run_id, "RUNNING", {})
                stages = {}

                def stage(name, operation, reuse=False):
                    previous = registry.step(run_id, name)
                    if reuse and not retry and previous and previous["status"] == "SUCCESS":
                        value = json.loads(previous["result_json"])
                        stages[name] = value
                        return value
                    registry.begin_step(run_id, name)
                    try:
                        value = operation()
                        registry.end_step(
                            run_id,
                            name,
                            "BLOCKED" if value.get("status") == "BLOCKED" else "SUCCESS",
                            value,
                        )
                    except Exception as error:
                        value = {"status": "FAILED", "error_type": type(error).__name__}
                        if isinstance(error, PriceDataBlocked):
                            value = error.report
                        registry.end_step(run_id, name, "FAILED", value)
                        raise
                    stages[name] = value
                    return value

                try:
                    stage(
                        "readiness",
                        lambda: (
                            {"status": "PENDING_MANUAL_REVIEW", "news_mode": "MANUAL_RESEARCH"}
                            if manual_news
                            else check_sources(settings, news_config, repo, stocks, cutoff_utc=now)
                        ),
                    )
                    if mode == "live":

                        def update_prices():
                            service = price_service or build_price_service(
                                settings, price_config, repo
                            )
                            report, path = service.run(symbols, now_utc=now, run_id=uuid4())
                            return report.to_dict() | {"report_path": str(path)}

                        price_result = stage("prices", update_prices, reuse=True)
                        if price_result["status"] == "BLOCKED":
                            raise PriceDataBlocked(price_result)

                        def update_news():
                            if manual_news:
                                return {"status": "SKIPPED", "reason": "manual_news_research"}
                            if not stocks:
                                return {"status": "SKIPPED", "reason": "no_active_members"}
                            service = news_service or build_news_service(
                                settings, news_config, repo
                            )
                            end = datetime.now(UTC) if now_utc is None else now
                            report, path = service.run(
                                stocks,
                                start_utc=end - timedelta(hours=news_config.risk.lookback_hours),
                                end_utc=end,
                                run_id=uuid4(),
                            )
                            return report.to_dict() | {"report_path": str(path)}

                        stage("news", update_news, reuse=True)
                    else:
                        stage(
                            "prices",
                            lambda: {
                                "status": "SKIPPED",
                                "reason": "historical_replay_no_acquisition",
                            },
                        )
                        stage(
                            "news",
                            lambda: {
                                "status": "SKIPPED",
                                "reason": "manual_news_research"
                                if manual_news
                                else "historical_replay_no_acquisition",
                            },
                        )

                    stage("quality", validate)

                    def report_stage():
                        generated = datetime.now(UTC) if now_utc is None else now
                        cutoff = generated if mode == "live" else None
                        report, artifacts = report_builder(
                            repository=repo,
                            database_path=settings.resolved_data_dir / "db/analytics.duckdb",
                            operations_database_path=settings.resolved_data_dir
                            / "db/operations.sqlite",
                            report_root=settings.resolved_data_dir / "reports/daily",
                            signal_session=session,
                            universe_paths=tuple(paths),
                            strategy_config=strategy_config,
                            include_news_risk=True,
                            news_lookback_hours=news_config.risk.lookback_hours,
                            benchmark_path=PROJECT_ROOT / "configs/universe/benchmarks.yaml",
                            include_model_ranking=False,
                            model_settings=None,
                            leverage_config=load_leverage_overlay_config(
                                PROJECT_ROOT / "configs/strategy/leverage_overlay.yaml"
                            ),
                            leverage_universe=load_leverage_overlay_universe_config(
                                PROJECT_ROOT / "configs/universe/leverage_overlay_universe.yaml"
                            ),
                            run_id=uuid4(),
                            generated_at_utc=generated,
                            news_cutoff_utc=cutoff,
                            manual_news=manual_news,
                        )
                        output = {
                            "status": report.get("status", "COMPLETED"),
                            "report_path": str(artifacts.json_path),
                            "summary": report["summary"],
                            "data_health": report.get("data_health", {}),
                        }
                        if report.get("manual_research"):
                            output["manual_research"] = report["manual_research"]
                        return output

                    result = stage("report", report_stage)
                    if any(
                        value.get("status") in {"BLOCKED", "DEGRADED", "UNAVAILABLE"}
                        for name, value in stages.items()
                        if mode == "live" or name != "readiness"
                    ):
                        result["status"] = "DEGRADED"
                    summary = {
                        "run_id": run_id,
                        "signal_session": session.isoformat(),
                        "mode": mode,
                        "refresh_window_utc": window.isoformat(),
                        "reused": False,
                        "raw_signature": _raw_signature(repo),
                        **result,
                    }
                except Exception as error:
                    summary = {
                        "run_id": run_id,
                        "signal_session": session.isoformat(),
                        "mode": mode,
                        "status": "BLOCKED" if isinstance(error, PriceDataBlocked) else "FAILED",
                        "error_type": type(error).__name__,
                    }
                    if isinstance(error, PriceDataBlocked):
                        summary["blocked_reasons"] = error.report.get("blocked_reasons", [])
                summary["stages"] = {
                    row["step"]: {
                        "status": row["status"],
                        "attempts": row["attempts"],
                        "result": json.loads(row["result_json"]),
                    }
                    for row in registry.stages(run_id)
                }
                directory = settings.resolved_data_dir / "reports/pipeline" / run_id
                directory.mkdir(parents=True, exist_ok=True)
                summary["pipeline_report_path"] = str(directory / "pipeline.json")
                temp = directory / "pipeline.json.tmp"
                temp.write_text(json.dumps(summary, indent=2, default=str))
                temp.replace(directory / "pipeline.json")
                registry.finish(run_id, summary["status"], summary)
                return summary
            finally:
                registry.close()
    except PipelineBusy:
        return {"status": "BLOCKED", "blocked_reasons": ["pipeline_already_running"]}
