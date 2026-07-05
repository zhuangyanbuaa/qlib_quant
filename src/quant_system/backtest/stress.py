"""Small, predeclared robustness grid for execution assumptions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from quant_system.backtest.engine import BacktestEngine
from quant_system.backtest.metrics import calculate_metrics
from quant_system.strategy.buy_the_dip import BuyTheDipStrategy
from quant_system.strategy.config import BuyTheDipConfig


@dataclass(frozen=True)
class StressScenario:
    name: str
    friction_multiplier: float = 1.0
    stop_multiplier: float = 1.0
    target_multiplier: float = 1.0


DEFAULT_SCENARIOS = (
    StressScenario("baseline"),
    StressScenario("friction_1_5x", friction_multiplier=1.5),
    StressScenario("friction_2x", friction_multiplier=2.0),
    StressScenario("tight_stop", stop_multiplier=0.8),
    StressScenario("wide_stop", stop_multiplier=1.2),
    StressScenario("near_target", target_multiplier=0.8),
    StressScenario("far_target", target_multiplier=1.2),
)


def run_stress_grid(
    features: pd.DataFrame,
    benchmark_prices: pd.DataFrame,
    config: BuyTheDipConfig,
    *,
    start: date,
    end: date,
) -> pd.DataFrame:
    """Run a fixed robustness grid; do not optimize against its best row."""
    rows: list[dict[str, object]] = []
    for scenario in DEFAULT_SCENARIOS:
        execution = config.execution.model_copy(
            update={
                "slippage_bps": (
                    config.execution.slippage_bps * scenario.friction_multiplier
                ),
                "commission_bps": (
                    config.execution.commission_bps * scenario.friction_multiplier
                ),
                "stop_atr_multiple": (
                    config.execution.stop_atr_multiple * scenario.stop_multiplier
                ),
                "target_atr_multiple": (
                    config.execution.target_atr_multiple * scenario.target_multiplier
                ),
            }
        )
        scenario_config = config.model_copy(update={"execution": execution})
        strategy = BuyTheDipStrategy(scenario_config.strategy)
        result = BacktestEngine(strategy, scenario_config).run(
            features,
            start=start,
            end=end,
        )
        metrics = calculate_metrics(
            result,
            benchmark_prices=benchmark_prices,
            initial_cash=scenario_config.portfolio.initial_cash,
        )
        rows.append(
            {
                "scenario": scenario.name,
                "total_return": metrics["total_return"],
                "maximum_drawdown": metrics["maximum_drawdown"],
                "sharpe": metrics["sharpe"],
                "closed_trade_count": metrics["closed_trade_count"],
                "win_rate": metrics["win_rate"],
                "expectancy_dollars": metrics["expectancy_dollars"],
            }
        )
    return pd.DataFrame(rows)
