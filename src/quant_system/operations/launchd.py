"""Per-project user LaunchAgent installation; no secrets in job definitions."""

import hashlib
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from quant_system.operations.scheduling import load_schedule
from quant_system.settings import PROJECT_ROOT


def agent_label():
    suffix = hashlib.sha256(str(PROJECT_ROOT).encode()).hexdigest()[:8]
    return f"com.qlib-quant.daily.{suffix}"


def agent_definition(settings, config_path):
    config_path = Path(config_path).resolve()
    config = load_schedule(config_path)
    python = PROJECT_ROOT / ".venv/bin/python"
    if not python.exists():
        raise ValueError("project .venv/bin/python is required for unattended execution")
    logs = settings.resolved_data_dir / "logs"
    return {
        "Label": agent_label(),
        "ProgramArguments": [
            str(python),
            "-m",
            "quant_system",
            "automation",
            "tick",
            "--config",
            str(config_path),
        ],
        "WorkingDirectory": str(PROJECT_ROOT),
        "EnvironmentVariables": {
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "QUANT_DATA_DIR": str(settings.resolved_data_dir),
            "PYTHONUNBUFFERED": "1",
        },
        "RunAtLoad": True,
        "StartInterval": config.poll_seconds,
        "StandardOutPath": str(logs / "automation-launchd.stdout.log"),
        "StandardErrorPath": str(logs / "automation-launchd.stderr.log"),
        "ProcessType": "Background",
    }


def install_agent(settings, config_path, *, directory=None, runner=subprocess.run):
    if sys.platform != "darwin":
        raise ValueError("launchd installation requires macOS")
    definition = agent_definition(settings, config_path)
    directory = directory or Path.home() / "Library/LaunchAgents"
    directory.mkdir(parents=True, exist_ok=True)
    (settings.resolved_data_dir / "logs").mkdir(parents=True, exist_ok=True)
    path = directory / f"{definition['Label']}.plist"
    if path.exists() and plistlib.loads(path.read_bytes()) != definition:
        runner(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(path)],
            capture_output=True,
            check=False,
        )
    path.write_bytes(plistlib.dumps(definition))
    path.chmod(0o600)
    domain = f"gui/{os.getuid()}"
    existing = runner(
        ["launchctl", "print", f"{domain}/{definition['Label']}"], capture_output=True, check=False
    )
    if existing.returncode:
        loaded = runner(
            ["launchctl", "bootstrap", domain, str(path)], capture_output=True, check=False
        )
        if loaded.returncode:
            raise RuntimeError("launchd_bootstrap_failed; inspect launchctl and local logs")
    return {
        "status": "INSTALLED",
        "label": definition["Label"],
        "plist_path": str(path),
        "telegram_configured": settings.telegram_configured,
    }


def uninstall_agent(*, directory=None, runner=subprocess.run):
    directory = directory or Path.home() / "Library/LaunchAgents"
    path = directory / f"{agent_label()}.plist"
    if path.exists():
        runner(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(path)],
            capture_output=True,
            check=False,
        )
        path.unlink()
    return {"status": "UNINSTALLED", "label": agent_label()}


def agent_status(*, runner=subprocess.run):
    result = runner(
        ["launchctl", "print", f"gui/{os.getuid()}/{agent_label()}"],
        capture_output=True,
        check=False,
    )
    return {"registered": result.returncode == 0, "label": agent_label()}
