"""Command-line entry point for the quant system."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from quant_system import __version__
from quant_system.logging import configure_logging, get_logger
from quant_system.settings import get_settings

app = typer.Typer(
    name="quant",
    help="Daily quantitative research and trading decision support.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def cli(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """Run research, data, and decision-support workflows."""
    settings = get_settings()
    configure_logging(settings.log_level)


@app.command()
def health() -> None:
    """Check that configuration and logging can initialize."""
    settings = get_settings()
    logger = get_logger(__name__)
    logger.info("health_check", extra={"app_env": settings.app_env})
    typer.echo("ok")


@app.command("show-config")
def show_config() -> None:
    """Print non-secret effective configuration."""
    settings = get_settings()
    typer.echo(json.dumps(settings.public_config(), indent=2, sort_keys=True))


def main() -> None:
    """Invoke the Typer application."""
    app()
