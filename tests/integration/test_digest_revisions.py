import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from test_daily_pipeline import Service, context

from quant_system.decision.digest_reports import refresh_digest_after_event
from quant_system.decision.gates import run_open_gate_workflow
from quant_system.decision.workbench import run_daily_workbench_workflow
from quant_system.operations.pipeline import run_daily_pipeline
from quant_system.sentiment.risk import NewsRiskAssessment


def test_current_gate_revises_live_digest_and_expiry_revokes_entry(tmp_path, monkeypatch):
    settings, kwargs = context(tmp_path)
    monkeypatch.setattr(
        "quant_system.decision.premarket._load_news_risk",
        lambda **_: {"AAPL": NewsRiskAssessment(symbol="AAPL", coverage_status="CURRENT")},
    )

    def build(**args):
        config = args["strategy_config"]
        args["strategy_config"] = config.model_copy(
            update={
                "strategy": config.strategy.model_copy(
                    update={
                        "minimum_rsi": 0,
                        "maximum_rsi": 100,
                        "maximum_atr_drawdown": 100,
                        "minimum_relative_return_60": -0.05,
                    }
                )
            }
        )
        return run_daily_workbench_workflow(**args)

    result = run_daily_pipeline(
        settings, price_service=Service(), news_service=Service(), report_builder=build, **kwargs
    )
    daily = json.loads(Path(result["report_path"]).read_text())
    root = settings.resolved_data_dir / "reports/daily"
    source = Path(daily["components"]["premarket"]["artifacts"]["json"])
    original = Path(daily["artifacts"]["digest_json"]).read_bytes()
    for row in daily["digest"]["stocks"]:
        lineage = row["technical_score"]["provenance"]
        assert lineage["available_at_utc"].endswith("+00:00")
        assert lineage["fetched_at_utc"].endswith("+00:00")
    assert daily["digest"]["pending_candidates"][0]["entry_status"] == "PENDING_GATE"
    plan = json.loads(source.read_text())["candidates"][0]
    cutoff = kwargs["now_utc"].replace(hour=14)
    quote = tmp_path / "quote.json"
    quote.write_text(
        json.dumps(
            [
                {
                    "symbol": "AAPL",
                    "last_price": plan["signal_close"],
                    "news_risk": "LOW",
                    "liquidity_status": "OK",
                    "quote_at_utc": cutoff.isoformat(),
                    "session_date_ny": "2026-07-01",
                    "source": "local_fixture",
                    "source_version": "fixed-v1",
                    "fetched_at_utc": cutoff.isoformat(),
                    "available_at_utc": cutoff.isoformat(),
                    "ingestion_run_id": str(uuid4()),
                }
            ]
        )
    )
    gate, _ = run_open_gate_workflow(
        premarket_report_path=source,
        report_root=root,
        minutes=30,
        snapshot_path=quote,
        generated_at_utc=cutoff,
    )
    manifest = json.loads((root / "latest.json").read_text())
    active_path = Path(manifest["json"])
    active_bytes = active_path.read_bytes()
    revised = json.loads(active_bytes)
    assert revised["metadata"]["publication_kind"] == "gate"
    assert revised["entry_candidates"][0]["symbol"] == "AAPL"
    assert (
        revised["entry_candidates"][0]["gate"]["valid_until_utc"]
        == (cutoff + timedelta(minutes=5)).isoformat()
    )
    assert Path(daily["artifacts"]["digest_json"]).read_bytes() == original
    # A later reading revision must evaluate freshness again, retaining original evidence.
    event = {"metadata": {"generated_at_utc": (cutoff + timedelta(minutes=6)).isoformat()}}
    refresh_digest_after_event(
        report_root=root, premarket_path=source, event=event, kind="research"
    )
    latest = json.loads((root / "latest.json").read_text())
    expired = json.loads(Path(latest["json"]).read_text())
    assert not expired["entry_candidates"]
    assert expired["pending_candidates"][0]["entry_status"] == "PENDING_GATE"
    assert active_path.read_bytes() == active_bytes
    # A delayed repeat of the earlier gate cannot regress the newer reading entry.
    refresh_digest_after_event(report_root=root, premarket_path=source, event=gate, kind="gate")
    assert json.loads((root / "latest.json").read_text()) == latest
