from typer.testing import CliRunner

from quant_system import __version__
from quant_system.cli import app

runner = CliRunner()


def test_help_lists_foundation_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "health" in result.stdout
    assert "show-config" in result.stdout
    assert "data" in result.stdout
    assert "strategy" in result.stdout
    assert "backtest" in result.stdout


def test_data_help_lists_phase_one_commands() -> None:
    result = runner.invoke(app, ["data", "--help"])

    assert result.exit_code == 0
    assert "migrate-legacy" in result.stdout
    assert "bootstrap-duckdb" in result.stdout
    assert "price-range" in result.stdout
    assert "update-prices" in result.stdout


def test_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_health() -> None:
    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
