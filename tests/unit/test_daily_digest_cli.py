import json

from typer.testing import CliRunner

from quant_system.cli import app
from quant_system.settings import Settings
from quant_system.storage.parquet import ParquetRepository
from quant_system.universe.config import load_daily_digest_config


def test_manual_workbench_uses_shared_full_universe_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "quant_system.cli.run_daily_workbench_workflow",
        lambda **kwargs: (calls.append(kwargs) or {}, None),
    )
    result = CliRunner().invoke(app, ["decision", "daily-workbench", "--manual-news"])
    assert result.exit_code == 0, result.output
    assert calls[0]["universe_paths"] == load_daily_digest_config().universe_paths


def test_manual_cnn_cli_imports_without_network_or_notification(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    monkeypatch.setattr("quant_system.cli.get_settings", lambda: settings)
    source = tmp_path / "cnn.json"
    source.write_text(
        json.dumps(
            {
                "value": 42,
                "observed_at_utc": "2026-06-30T20:00:00Z",
                "source_url": "https://www.cnn.com/markets/fear-and-greed",
            }
        )
    )
    result = CliRunner().invoke(app, ["data", "import-cnn", str(source)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["rows_written"] == 1
    fact = ParquetRepository(settings.resolved_data_dir).read_market_observations().to_pylist()[0]
    assert fact["available_at_utc"] > fact["observed_at_utc"]
    source.write_text('{"value": 42}')
    assert CliRunner().invoke(app, ["data", "import-cnn", str(source)]).exit_code != 0
    assert ParquetRepository(settings.resolved_data_dir).read_market_observations().num_rows == 1
