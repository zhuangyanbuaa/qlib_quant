# qlib-quant

A reproducible daily research and decision-support system for a rules-first
Buy-the-Dip strategy. The project is being rebuilt as a modular Python
application; it does not place brokerage orders.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync --dev
uv run quant --help
```

Create local configuration only when needed:

```bash
cp .env.example .env
```

## Development

```bash
uv run pytest
uv run ruff check .
uv run quant --help
```

Runtime datasets, model artifacts, reports, secrets, and virtual environments
are intentionally excluded from Git. The current architecture and phased
roadmap live in
[`docs/plans/2026-06-28-quant-repo-refactor.md`](docs/plans/2026-06-28-quant-repo-refactor.md).
