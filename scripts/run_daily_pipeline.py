#!/usr/bin/env python
"""Thin wrapper for the canonical daily pipeline CLI."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
python = ROOT / ".venv/bin/python"
environment = os.environ.copy()
environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
result = subprocess.run(
    [
        str(python) if python.exists() else sys.executable,
        "-m",
        "quant_system",
        "data",
        "update-daily",
        *sys.argv[1:],
    ],
    cwd=ROOT,
    env=environment,
    check=False,
)
raise SystemExit(result.returncode)
