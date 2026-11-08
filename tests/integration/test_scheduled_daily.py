"""Offline scheduled digest and fake Telegram acceptance."""

# ruff: noqa: RUF001

import json
from pathlib import Path

from pydantic import SecretStr
from test_daily_pipeline import Service, context

from quant_system.operations.pipeline import run_daily_pipeline
from quant_system.operations.scheduling import DailySchedule, tick_daily


def test_scheduled_manual_pipeline_and_notification_smoke(tmp_path):
    settings, kwargs = context(tmp_path)
    from uuid import uuid4

    from test_price_decision_cutoff import price, seed

    from quant_system.ingestion.market_context import parse_cnn
    from quant_system.storage.parquet import ParquetRepository

    repo = ParquetRepository(settings.resolved_data_dir)
    payload = json.loads(Path("tests/fixtures/market_context/cnn_contract.json").read_text())
    fact = parse_cnn(payload, fetched_at=kwargs["now_utc"], run_id=uuid4())
    repo.write_market_observations([fact], run_id=fact.ingestion_run_id)
    seed(repo, [price("^VIX", kwargs["signal_session"], 18.3)])
    settings = settings.model_copy(
        update={
            "telegram_bot_token": SecretStr("123456:" + "fixture_not_a_real_token" * 2),
            "telegram_chat_id": "42",
        }
    )
    calls, reports = [], []
    prices, news = Service(), Service(crash=True)

    class Notifier:
        def send(self, chat, text):
            calls.append(text)
            return 7

    def pipeline(settings, **args):
        args["universe_paths"] = kwargs["universe_paths"]
        args["context_price_service"] = kwargs["context_price_service"]
        result = run_daily_pipeline(settings, price_service=prices, news_service=news, **args)
        reports.append(result)
        assert Path(result["manual_research"]["artifacts"]["prompt"]).exists()
        assert (
            json.loads(Path(result["report_path"]).read_text())["summary"][
                "execution_eligible_candidate_count"
            ]
            == 0
        )
        return result

    options = dict(now_utc=kwargs["now_utc"], pipeline=pipeline, client=Notifier())
    result = tick_daily(settings, DailySchedule(), **options)
    repeated = tick_daily(settings, DailySchedule(), **options)
    assert result["status"] == repeated["status"] == "DONE"
    assert result["notifications"][0]["status"] == "SENT"
    assert len(prices.calls) == len(calls) == 1 and not news.calls
    assert len(kwargs["context_price_service"].calls) == 1
    assert "DEGRADED" in calls[0] and "CNN" in calls[0] and "VIX" in calls[0]
    assert "本机" not in calls[0] and "prompt" not in calls[0]
    assert "CNN 恐惧与贪婪：42.5/100" in calls[0] and "VIX：18.3" in calls[0]
    digest = reports[0]["digest"]
    markdown = (settings.resolved_data_dir / "reports/daily/latest.md").read_text()
    assert f"系统技术分：{digest['market']['technical_score']['value']:g}/100" in calls[0]
    for sector in digest["sectors"]:
        if sector["proxy_kind"] == "ETF":
            assert sector["name"] in calls[0] and sector["name"] in markdown
        else:
            assert sector["symbol"] in markdown
    assert "网络与光通信" in calls[0]
    for stock in digest["pending_candidates"]:
        assert stock["symbol"] in calls[0] and stock["symbol"] in markdown
