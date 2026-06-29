"""Compatibility entry point for rebuilding DuckDB analytical views."""

from __future__ import annotations

import sys

from quant_system.cli import main

if __name__ == "__main__":
    sys.argv[1:1] = ["data", "bootstrap-duckdb"]
    main()
