"""Strict universe and benchmark configuration loaders."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BenchmarkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    role: str
    reason: str

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class BenchmarkUniverseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    benchmarks: tuple[BenchmarkConfig, ...]

    @model_validator(mode="after")
    def symbols_are_unique(self) -> BenchmarkUniverseConfig:
        symbols = [benchmark.symbol for benchmark in self.benchmarks]
        if len(symbols) != len(set(symbols)):
            raise ValueError("benchmark symbols must be unique")
        return self


class WatchlistMemberConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    company_name: str
    sector: str
    theme: str
    subtheme: str
    start_date: date
    end_date: date | None = None
    reason: str
    liquidity_tier: Literal["mega", "high", "medium"]
    benchmark_etf: str

    @field_validator("symbol", "benchmark_etf")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def date_range_is_valid(self) -> WatchlistMemberConfig:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError(f"{self.symbol} end_date must not be before start_date")
        return self


class WatchlistSelectionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_date_rule: str
    minimum_size: int = Field(ge=1)
    maximum_size: int = Field(ge=1)
    benchmark_symbols: tuple[str, ...]
    source_basis: tuple[str, ...]

    @field_validator("benchmark_symbols")
    @classmethod
    def normalize_benchmarks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(symbol.upper() for symbol in value)


class WatchlistConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    universe_type: Literal["CURRENT_SNAPSHOT_FORWARD_ONLY"]
    description: str
    selection_policy: WatchlistSelectionPolicy
    symbols: tuple[WatchlistMemberConfig, ...]

    @model_validator(mode="after")
    def validate_membership(self) -> WatchlistConfig:
        members = [member.symbol for member in self.symbols]
        if len(members) != len(set(members)):
            raise ValueError("watchlist symbols must be unique")
        count = len(members)
        if count < self.selection_policy.minimum_size:
            raise ValueError("watchlist is smaller than selection_policy.minimum_size")
        if count > self.selection_policy.maximum_size:
            raise ValueError("watchlist is larger than selection_policy.maximum_size")
        allowed_benchmarks = set(self.selection_policy.benchmark_symbols)
        invalid = sorted(
            {member.benchmark_etf for member in self.symbols} - allowed_benchmarks
        )
        if invalid:
            raise ValueError(f"watchlist members use undeclared benchmarks: {invalid}")
        return self

    @property
    def member_symbols(self) -> tuple[str, ...]:
        return tuple(member.symbol for member in self.symbols)

    @property
    def benchmark_symbols(self) -> tuple[str, ...]:
        return self.selection_policy.benchmark_symbols


def load_benchmark_config(path: Path) -> BenchmarkUniverseConfig:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"benchmark config must be a mapping: {path}")
    return BenchmarkUniverseConfig.model_validate(payload)


def load_watchlist_config(path: Path) -> WatchlistConfig:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"watchlist config must be a mapping: {path}")
    return WatchlistConfig.model_validate(payload)
