"""Portfolio, annual, symbol, and regime performance metrics."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quant_system.backtest.engine import BacktestResult


def calculate_metrics(
    result: BacktestResult,
    *,
    benchmark_prices: pd.DataFrame,
    initial_cash: float,
) -> dict[str, float | int | str | None]:
    equity = result.equity_curve.copy()
    if equity.empty:
        raise ValueError("equity curve is empty")
    equity["session_date_ny"] = pd.to_datetime(equity["session_date_ny"])
    equity["daily_return"] = equity["equity"].pct_change().fillna(
        equity["equity"].iloc[0] / initial_cash - 1
    )
    returns = equity["daily_return"]
    total_return = equity["equity"].iloc[-1] / initial_cash - 1
    elapsed_days = max(
        1,
        (equity["session_date_ny"].iloc[-1] - equity["session_date_ny"].iloc[0]).days,
    )
    years = elapsed_days / 365.25
    cagr = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1.0
    running_peak = equity["equity"].cummax().clip(lower=initial_cash)
    maximum_drawdown = (equity["equity"] / running_peak - 1).min()
    volatility = returns.std(ddof=1)
    sharpe = (
        math.sqrt(252) * returns.mean() / volatility
        if volatility and not np.isnan(volatility)
        else None
    )
    downside = returns.loc[returns < 0].std(ddof=1)
    sortino = (
        math.sqrt(252) * returns.mean() / downside
        if downside and not np.isnan(downside)
        else None
    )

    trades = pd.DataFrame(trade.to_dict() for trade in result.trades)
    wins = trades.loc[trades["net_pnl"] > 0] if not trades.empty else trades
    losses = trades.loc[trades["net_pnl"] < 0] if not trades.empty else trades
    gross_profit = float(wins["net_pnl"].sum()) if not wins.empty else 0.0
    gross_loss = float(-losses["net_pnl"].sum()) if not losses.empty else 0.0
    benchmark_return = _benchmark_return(benchmark_prices)
    turnover_notional = (
        float(
            (
                trades["entry_price"] * trades["quantity"]
                + trades["exit_price"] * trades["quantity"]
            ).sum()
        )
        if not trades.empty
        else 0.0
    )
    closed_trade_count = len(result.trades)
    return {
        "initial_cash": initial_cash,
        "final_equity": float(equity["equity"].iloc[-1]),
        "total_return": float(total_return),
        "cagr": float(cagr),
        "maximum_drawdown": float(maximum_drawdown),
        "sharpe": float(sharpe) if sharpe is not None else None,
        "sortino": float(sortino) if sortino is not None else None,
        "benchmark_total_return": benchmark_return,
        "relative_total_return": (
            float(total_return - benchmark_return)
            if benchmark_return is not None
            else None
        ),
        "candidate_count": len(result.signals),
        "closed_trade_count": closed_trade_count,
        "sample_size_status": _sample_size_status(closed_trade_count),
        "open_position_count": len(result.open_positions),
        "rejection_count": len(result.rejections),
        "win_rate": float(len(wins) / len(trades)) if len(trades) else None,
        "average_trade_return": (
            float(trades["net_return"].mean()) if not trades.empty else None
        ),
        "expectancy_dollars": (
            float(trades["net_pnl"].mean()) if not trades.empty else None
        ),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "exposure_fraction": float(equity["gross_exposure_fraction"].mean()),
        "turnover": turnover_notional / float(equity["equity"].mean()),
    }


def annual_metrics(equity_curve: pd.DataFrame, initial_cash: float) -> pd.DataFrame:
    """Return calendar-year portfolio performance from daily equity."""
    equity = equity_curve.copy()
    if equity.empty:
        return pd.DataFrame(columns=["year", "starting_equity", "ending_equity", "return"])
    equity["session_date_ny"] = pd.to_datetime(equity["session_date_ny"])
    equity["year"] = equity["session_date_ny"].dt.year
    rows = []
    prior_equity = initial_cash
    for year, group in equity.groupby("year", sort=True):
        ending_equity = float(group.iloc[-1]["equity"])
        rows.append(
            {
                "year": int(year),
                "starting_equity": prior_equity,
                "ending_equity": ending_equity,
                "return": ending_equity / prior_equity - 1,
            }
        )
        prior_equity = ending_equity
    return pd.DataFrame(rows)


def trade_group_metrics(trades: tuple, group_field: str) -> pd.DataFrame:
    """Aggregate closed-trade outcomes by symbol or entry regime."""
    frame = pd.DataFrame(trade.to_dict() for trade in trades)
    if frame.empty:
        return pd.DataFrame(
            columns=[group_field, "trades", "win_rate", "net_pnl", "average_return"]
        )
    grouped = frame.groupby(group_field, sort=True)
    return grouped.agg(
        trades=("net_pnl", "size"),
        win_rate=("net_pnl", lambda values: float((values > 0).mean())),
        net_pnl=("net_pnl", "sum"),
        average_return=("net_return", "mean"),
    ).reset_index()


def reconstruct_realized_equity(initial_cash: float, trades: tuple) -> float:
    """Rebuild realized cash equity strictly from the closed trade ledger."""
    return initial_cash + sum(trade.net_pnl for trade in trades)


def _benchmark_return(benchmark_prices: pd.DataFrame) -> float | None:
    if benchmark_prices.empty:
        return None
    ordered = benchmark_prices.sort_values("session_date_ny")
    first = float(ordered.iloc[0]["close"])
    last = float(ordered.iloc[-1]["close"])
    return last / first - 1


def _sample_size_status(closed_trade_count: int) -> str:
    if closed_trade_count < 30:
        return "INSUFFICIENT"
    if closed_trade_count < 100:
        return "PRELIMINARY"
    if closed_trade_count < 200:
        return "ADEQUATE"
    return "HIGH_CONFIDENCE"
