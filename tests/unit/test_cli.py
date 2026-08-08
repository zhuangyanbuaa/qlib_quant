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
    assert "positions" in result.stdout
    assert "rotation" in result.stdout
    assert "hierarchy" in result.stdout
    assert "preopen-refresh" in result.stdout
    assert "open-gate" in result.stdout


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
