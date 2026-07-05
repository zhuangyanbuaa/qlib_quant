"""Persist reproducible backtest artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pandas as pd

from quant_system.backtest.engine import BacktestResult
from quant_system.backtest.metrics import annual_metrics, trade_group_metrics
from quant_system.strategy.config import BuyTheDipConfig


@dataclass(frozen=True)
class BacktestArtifacts:
    directory: Path
    summary: Path
    trades: Path
    equity_curve: Path
    rejections: Path
    annual: Path
    by_symbol: Path
    by_regime: Path
    stress: Path | None


def write_backtest_report(
    *,
    result: BacktestResult,
    metrics: dict[str, object],
    config: BuyTheDipConfig,
    run_id: UUID,
    start: date,
    end: date,
    symbols: tuple[str, ...],
    report_root: Path,
    stress_results: pd.DataFrame | None = None,
) -> BacktestArtifacts:
    directory = report_root / str(run_id)
    directory.mkdir(parents=True, exist_ok=False)
    trades_path = directory / "trades.csv"
    equity_path = directory / "equity_curve.csv"
    rejections_path = directory / "rejections.csv"
    annual_path = directory / "annual.csv"
    symbol_path = directory / "by_symbol.csv"
    regime_path = directory / "by_regime.csv"
    stress_path = directory / "stress.csv" if stress_results is not None else None
    summary_path = directory / "summary.json"

    pd.DataFrame(trade.to_dict() for trade in result.trades).to_csv(
        trades_path,
        index=False,
    )
    result.equity_curve.to_csv(equity_path, index=False)
    result.rejections.to_csv(rejections_path, index=False)
    annual_metrics(result.equity_curve, config.portfolio.initial_cash).to_csv(
        annual_path,
        index=False,
    )
    trade_group_metrics(result.trades, "symbol").to_csv(symbol_path, index=False)
    trade_group_metrics(result.trades, "market_regime").to_csv(
        regime_path,
        index=False,
    )
    if stress_results is not None:
        stress_results.to_csv(stress_path, index=False)

    summary = {
        "run_id": str(run_id),
        "strategy": "buy_the_dip_v1",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "universe_type": config.backtest.universe_type,
        "symbols": list(symbols),
        "symbol_count": len(symbols),
        "metrics": _json_safe(metrics),
        "open_positions": list(result.open_positions),
        "config": config.model_dump(mode="json"),
        "config_hash": sha256(
            json.dumps(
                config.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return BacktestArtifacts(
        directory=directory,
        summary=summary_path,
        trades=trades_path,
        equity_curve=equity_path,
        rejections=rejections_path,
        annual=annual_path,
        by_symbol=symbol_path,
        by_regime=regime_path,
        stress=stress_path,
    )


def _json_safe(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: None
        if isinstance(value, float) and (pd.isna(value) or value in (float("inf"), -float("inf")))
        else value
        for key, value in payload.items()
    }
