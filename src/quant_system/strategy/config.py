"""Strict configuration for the rules-only Buy-the-Dip baseline."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class RulesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark_symbol: str = "QQQ"
    min_average_dollar_volume: float = Field(gt=0)
    minimum_ma50_slope: float
    minimum_relative_return_60: float
    minimum_drawdown: float = Field(ge=0, le=1)
    maximum_drawdown: float = Field(ge=0, le=1)
    minimum_rsi: float = Field(ge=0, le=100)
    maximum_rsi: float = Field(ge=0, le=100)
    minimum_atr_drawdown: float = Field(gt=0)
    maximum_atr_drawdown: float = Field(gt=0)
    yellow_maximum_ma50_decline: float = Field(ge=0)


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    holding_sessions: int = Field(ge=1)
    stop_atr_multiple: float = Field(gt=0)
    target_atr_multiple: float = Field(gt=0)
    maximum_gap_up: float = Field(ge=0)
    maximum_gap_down: float = Field(ge=0)
    slippage_bps: float = Field(ge=0)
    commission_bps: float = Field(ge=0)


class PortfolioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    initial_cash: float = Field(gt=0)
    risk_per_trade: float = Field(gt=0, le=1)
    reserve_cash_fraction: float = Field(ge=0, lt=1)
    maximum_position_fraction: float = Field(gt=0, le=1)
    maximum_gross_exposure: float = Field(gt=0, le=1)
    maximum_positions: int = Field(ge=1)
    yellow_risk_multiplier: float = Field(gt=0, le=1)


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    universe_type: str


class BuyTheDipConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: RulesConfig
    execution: ExecutionConfig
    portfolio: PortfolioConfig
    backtest: BacktestConfig


def load_buy_the_dip_config(path: Path) -> BuyTheDipConfig:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"strategy config must be a mapping: {path}")
    config = BuyTheDipConfig.model_validate(payload)
    if config.strategy.minimum_drawdown > config.strategy.maximum_drawdown:
        raise ValueError("minimum_drawdown must not exceed maximum_drawdown")
    if config.strategy.minimum_rsi > config.strategy.maximum_rsi:
        raise ValueError("minimum_rsi must not exceed maximum_rsi")
    if config.strategy.minimum_atr_drawdown > config.strategy.maximum_atr_drawdown:
        raise ValueError("minimum_atr_drawdown must not exceed maximum_atr_drawdown")
    return config
