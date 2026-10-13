import json

import pytest
from typer.testing import CliRunner

from quant_system import __version__
from quant_system.cli import app
from quant_system.settings import get_settings

runner = CliRunner()


def test_update_prices_cli_reuses_budget_across_invocations(tmp_path, monkeypatch):
    from pathlib import Path

    import yaml

    from quant_system.ingestion.base import ProviderBatchResult

    calls = []

    class Provider:
        name = "fixture_cli"
        version = "1"

        def __init__(self, **kwargs):
            pass

        def fetch_daily(self, symbols, **kwargs):
            calls.append(symbols)
            return ProviderBatchResult(empty_symbols=symbols)

    config = yaml.safe_load(Path("configs/sources/prices.yaml").read_text())
    config.update(daily_call_budget=1, calls_per_minute=1000)
    config_path = tmp_path / "prices.yaml"
    config_path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("quant_system.cli.YahooFinancePriceAdapter", Provider)
    get_settings.cache_clear()
    try:
        command = ["data", "update-prices", "--symbols", "AAPL", "--config", str(config_path)]
        first = runner.invoke(app, command)
        second = runner.invoke(app, command)
        assert first.exit_code == second.exit_code == 2
        assert json.loads(first.stdout)["provider_calls"] == 1
        assert json.loads(second.stdout)["provider_calls"] == 0
        assert len(calls) == 1
        assert "RequestBudgetExceeded" in str(json.loads(second.stdout)["failed_symbols"])
    finally:
        get_settings.cache_clear()


def test_lifecycle_import_validates_before_append_and_is_idempotent(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from uuid import uuid4

    from quant_system.domain.securities import SecurityLifecycleEvent
    from quant_system.storage.parquet import ParquetRepository

    monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        fact = SecurityLifecycleEvent(
            event_id="dead",
            security_id="local:OLD",
            symbol="OLD",
            event_type="DELISTED",
            effective_session="2026-06-29",
            source_url="https://example.com/notice",
            source="exchange",
            source_version="fixture-1",
            ingestion_run_id=uuid4(),
            fetched_at_utc=datetime(2026, 6, 30, tzinfo=UTC),
            available_at_utc=datetime(2026, 6, 30, tzinfo=UTC),
        )
        manifest = tmp_path / "lifecycle.json"
        manifest.write_text(json.dumps({"events": [fact.model_dump(mode="json")]}))
        command = ["data", "import-lifecycle", "--manifest", str(manifest)]
        dry = runner.invoke(app, [*command, "--dry-run"])
        assert dry.exit_code == 0
        assert json.loads(dry.stdout)["files_written"] == 0
        repository = ParquetRepository(tmp_path / "data")
        assert repository.read_security_lifecycle().num_rows == 0
        result = runner.invoke(app, command)
        assert result.exit_code == 0
        assert json.loads(result.stdout)["new_events"] == 1
        again = runner.invoke(app, command)
        assert again.exit_code == 0
        assert json.loads(again.stdout)["new_events"] == 0
        assert repository.read_security_lifecycle().num_rows == 1
        manifest.write_text(
            json.dumps({"events": [fact.model_dump(mode="json"), {"symbol": "BAD"}]})
        )
        invalid = runner.invoke(app, command)
        assert invalid.exit_code != 0
        assert repository.read_security_lifecycle().num_rows == 1
    finally:
        get_settings.cache_clear()


def test_company_mapping_import_is_atomic_idempotent_and_supports_dry_run(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from uuid import uuid4

    from quant_system.domain.companies import ReviewedCompanyMapping
    from quant_system.storage.parquet import ParquetRepository

    monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        fact = ReviewedCompanyMapping(
            mapping_id="review-aapl",
            symbol="AAPL",
            cik="320193",
            aliases=("Apple",),
            source="sec_review",
            source_version="fixture-1",
            source_url="https://www.sec.gov/Archives/edgar/data/320193/",
            ingestion_run_id=uuid4(),
            fetched_at_utc=datetime(2026, 6, 30, tzinfo=UTC),
            available_at_utc=datetime(2026, 6, 30, tzinfo=UTC),
        )
        manifest = tmp_path / "mappings.json"
        manifest.write_text(json.dumps({"mappings": [fact.model_dump(mode="json")]}))
        command = ["data", "import-company-mappings", "--manifest", str(manifest)]
        dry = runner.invoke(app, [*command, "--dry-run"])
        repository = ParquetRepository(tmp_path / "data")
        assert dry.exit_code == 0 and json.loads(dry.stdout)["files_written"] == 0
        assert repository.read_company_mappings().num_rows == 0
        first = runner.invoke(app, command)
        assert first.exit_code == 0 and json.loads(first.stdout)["new_mappings"] == 1
        files = list(repository.company_mappings_root.rglob("*.parquet"))
        before = {path: path.read_bytes() for path in files}
        repeated = runner.invoke(app, command)
        assert repeated.exit_code == 0 and json.loads(repeated.stdout)["new_mappings"] == 0
        extra = fact.model_copy(update={"mapping_id": "extra"}).model_dump(mode="json")
        manifest.write_text(json.dumps({"mappings": [extra, {"symbol": "INVALID"}]}))
        assert runner.invoke(app, command).exit_code != 0
        assert repository.read_company_mappings().num_rows == 1
        assert all(path.read_bytes() == value for path, value in before.items())
    finally:
        get_settings.cache_clear()


def test_verify_news_caps_sample_before_any_acquisition(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path / "data"))
    calls = []
    monkeypatch.setattr("quant_system.cli.build_news_service", lambda *args: calls.append(args))
    get_settings.cache_clear()
    try:
        result = runner.invoke(app, ["data", "verify-news", "--symbols", "AAPL,MSFT,NVDA,AMD"])
        assert result.exit_code != 0
        assert not calls
        assert not list((tmp_path / "data/raw").rglob("*.parquet"))
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("timestamp", ["2026-07-01T12:00:00Z", "2026-07-01T21:00:00+09:00"])
def test_pipeline_cli_accepts_explicit_timezone_and_normalizes_utc(monkeypatch, timestamp):
    from datetime import UTC, datetime

    received = []
    monkeypatch.setattr(
        "quant_system.cli.run_daily_pipeline",
        lambda *args, **kwargs: received.append(kwargs) or {"status": "COMPLETED"},
    )
    result = runner.invoke(app, ["data", "update-daily", "--refresh-window", timestamp])
    assert result.exit_code == 0, result.output
    assert received[0]["refresh_window"] == datetime(2026, 7, 1, 12, tzinfo=UTC)


def test_manual_news_cli_modes_forward_option_and_keep_news_risk_enabled(monkeypatch):
    received = []
    monkeypatch.setattr(
        "quant_system.cli.run_daily_pipeline",
        lambda *args, **kwargs: received.append(kwargs) or {"status": "DEGRADED"},
    )
    assert runner.invoke(app, ["data", "update-daily", "--manual-news"]).exit_code == 0
    assert received[-1]["manual_news"] is True
    monkeypatch.setattr(
        "quant_system.cli.run_daily_workbench_workflow",
        lambda **kwargs: (received.append(kwargs) or {}, None),
    )
    assert runner.invoke(app, ["decision", "daily-workbench", "--manual-news"]).exit_code == 0
    assert received[-1]["manual_news"] is True
    assert received[-1]["include_news_risk"] is True


@pytest.mark.parametrize(
    ("command", "workflow"),
    [
        ("premarket", "run_premarket_workflow"),
        ("satellite", "run_premarket_workflow"),
        ("research-list", "run_research_list_workflow"),
        ("daily-workbench", "run_daily_workbench_workflow"),
    ],
)
def test_decision_cli_passes_aware_news_cutoff(monkeypatch, command, workflow):
    from datetime import UTC, datetime

    received = []
    monkeypatch.setattr(
        f"quant_system.cli.{workflow}",
        lambda **kwargs: (received.append(kwargs) or {}, None),
    )
    result = runner.invoke(
        app, ["decision", command, "--date", "2026-06-30", "--news-cutoff", "2026-07-01T12:00:00Z"]
    )
    assert result.exit_code == 0, result.output
    assert received[0]["news_cutoff_utc"] == datetime(2026, 7, 1, 12, tzinfo=UTC)


def test_help_lists_foundation_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "health" in result.stdout
    assert "show-config" in result.stdout
    assert "data" in result.stdout
    assert "strategy" in result.stdout
    assert "backtest" in result.stdout
    assert "model" in result.stdout
    assert "decision" in result.stdout
    assert "journal" in result.stdout
    assert "paper" in result.stdout


def test_data_help_lists_phase_one_commands() -> None:
    result = runner.invoke(app, ["data", "--help"])

    assert result.exit_code == 0
    assert "migrate-legacy" in result.stdout
    assert "bootstrap-duckdb" in result.stdout
    assert "price-range" in result.stdout
    assert "update-prices" in result.stdout
    assert "update-news" in result.stdout
    assert "news-risk" in result.stdout


def test_update_prices_help_lists_universe_option() -> None:
    result = runner.invoke(app, ["data", "update-prices", "--help"])

    assert result.exit_code == 0
    assert "--universe" in result.stdout


def test_model_help_lists_phase_five_commands() -> None:
    result = runner.invoke(app, ["model", "--help"])

    assert result.exit_code == 0
    assert "ridge-baseline" in result.stdout
    assert "ranking-baseline" in result.stdout


def test_decision_help_lists_phase_six_commands() -> None:
    result = runner.invoke(app, ["decision", "--help"])

    assert result.exit_code == 0
    assert "premarket" in result.stdout
    assert "satellite" in result.stdout
    assert "research-list" in result.stdout
    assert "daily-workbench" in result.stdout
    assert "leverage-overlay" in result.stdout
    assert "positions" in result.stdout
    assert "rotation" in result.stdout
    assert "hierarchy" in result.stdout
    assert "preopen-refresh" in result.stdout
    assert "open-gate" in result.stdout


def test_leverage_overlay_help_lists_batch_option() -> None:
    result = runner.invoke(app, ["decision", "leverage-overlay", "--help"])

    assert result.exit_code == 0
    assert "--all" in result.stdout
    assert "universe YAML" in result.stdout


def test_journal_help_lists_phase_six_commands() -> None:
    result = runner.invoke(app, ["journal", "--help"])

    assert result.exit_code == 0
    assert "add-fill" in result.stdout
    assert "positions" in result.stdout
    assert "decision" in result.stdout


def test_paper_help_lists_phase_six_commands() -> None:
    result = runner.invoke(app, ["paper", "--help"])

    assert result.exit_code == 0
    assert "update" in result.stdout
    assert "advance" in result.stdout
    assert "positions" in result.stdout


def test_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_health() -> None:
    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"


def test_price_gate_returns_blocked_json_and_exit_two(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        result = runner.invoke(
            app,
            [
                "strategy",
                "scan",
                "--symbols",
                "AAPL",
                "--date",
                "2026-06-26",
                "--no-news-risk",
            ],
        )
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 2
    report = json.loads(result.stdout)
    assert report["status"] == "BLOCKED"
    assert report["data_cutoff_utc"] == "2026-06-26T20:30:00+00:00"
    assert len(report["blocked_reasons"]) == 2
