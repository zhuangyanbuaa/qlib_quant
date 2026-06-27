from typer.testing import CliRunner

from quant_system import __version__
from quant_system.cli import app

runner = CliRunner()


def test_help_lists_foundation_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "health" in result.stdout
    assert "show-config" in result.stdout


def test_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_health() -> None:
    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
