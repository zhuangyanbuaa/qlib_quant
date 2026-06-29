"""Compatibility entry point for the legacy-price migration command."""

from __future__ import annotations

import sys

from quant_system.cli import main

if __name__ == "__main__":
    sys.argv[1:1] = ["data", "migrate-legacy"]
    main()
