#!/usr/bin/env python
"""Run the daily workbench with one command.

This script is a thin wrapper around the canonical CLI:

    quant decision daily-workbench

It re-executes inside the local .venv when available, then prints the generated
daily_index paths from the CLI JSON response.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
VENV_ROOT = PROJECT_ROOT / ".venv"


def main() -> None:
    if (
        VENV_PYTHON.exists()
        and Path(sys.prefix).resolve() != VENV_ROOT.resolve()
        and os.environ.get("QUANT_DAILY_WORKBENCH_REEXEC") != "1"
    ):
        env = os.environ.copy()
        env["QUANT_DAILY_WORKBENCH_REEXEC"] = "1"
        os.execve(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv], env)

    args = _parse_args()
    command = [
        sys.executable,
        "-m",
        "quant_system",
        "decision",
        "daily-workbench",
    ]
    if args.date:
        command.extend(["--date", args.date])
    command.append("--news-risk" if args.news_risk else "--no-news-risk")
    command.append("--model-ranking" if args.model_ranking else "--no-model-ranking")
    command.append("--positions" if args.positions else "--no-positions")
    command.extend(["--max-research-symbols", str(args.max_research_symbols)])
    command.extend(["--news-days", str(args.news_days)])

    env = os.environ.copy()
    src_root = PROJECT_ROOT / "src"
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(src_root)
        if not existing_pythonpath
        else f"{src_root}{os.pathsep}{existing_pythonpath}"
    )
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    print(json.dumps(_summary(report), indent=2, sort_keys=True))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the daily quant workbench.")
    parser.add_argument("--date", help="Signal session in YYYY-MM-DD form.")
    parser.add_argument(
        "--news-risk",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Apply news-risk vetoes.",
    )
    parser.add_argument(
        "--model-ranking",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Attach read-only LightGBM rank context.",
    )
    parser.add_argument(
        "--positions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include manual position checks.",
    )
    parser.add_argument("--max-research-symbols", type=int, default=20)
    parser.add_argument("--news-days", type=int, default=7)
    return parser.parse_args()


def _summary(report: dict[str, object]) -> dict[str, object]:
    artifacts = report.get("artifacts", {})
    metadata = report.get("metadata", {})
    summary = report.get("summary", {})
    return {
        "status": "COMPLETED",
        "signal_session": metadata.get("signal_session"),
        "run_id": metadata.get("run_id"),
        "summary": summary,
        "daily_index": artifacts,
    }


if __name__ == "__main__":
    main()
