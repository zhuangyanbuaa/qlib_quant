"""Strict universe and benchmark configuration loaders."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant_system.settings import PROJECT_ROOT


class DigestProxyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    symbol: str
    name: str
    group: Literal["market", "sector", "theme"]
    source_url: str


class DailyDigestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str
    score_version: str
    universe_paths: tuple[Path, ...] = Field(min_length=1)
    review_paths: tuple[Path, ...] = ()
    proxies: tuple[DigestProxyConfig, ...]
    telegram_page_units: int = Field(default=3800, ge=512, le=4096)

    @model_validator(mode="after")
    def unique_inputs(self):
        if len(set(self.universe_paths)) != len(self.universe_paths):
            raise ValueError("duplicate digest universe path")
        symbols = [proxy.symbol for proxy in self.proxies]
        if len(set(symbols)) != len(symbols):
            raise ValueError("duplicate digest proxy")
        if set(self.universe_paths) & set(self.review_paths):
            raise ValueError("review-only paths cannot be production inputs")
        return self


def load_daily_digest_config(path: Path | None = None) -> DailyDigestConfig:
    path = path or PROJECT_ROOT / "configs/reports/daily_digest.yaml"
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    config = DailyDigestConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    return config.model_copy(
        update={
            name: tuple(p if p.is_absolute() else PROJECT_ROOT / p for p in getattr(config, name))
            for name in ("universe_paths", "review_paths")
        }
    )


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
    universe_type: Literal[
        "CURRENT_SNAPSHOT_FORWARD_ONLY",
        "AI_SATELLITE",
        "INTERNET_PLATFORM_SATELLITE",
        "SPACE_SATELLITE",
        "CRYPTO_COMPUTE_SATELLITE",
        "POWER_ENERGY_SATELLITE",
        "RAW_REVIEW",
        "HEDGE_OVERLAY",
    ]
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
        invalid = sorted({member.benchmark_etf for member in self.symbols} - allowed_benchmarks)
        if invalid:
            raise ValueError(f"watchlist members use undeclared benchmarks: {invalid}")
        return self

    @property
    def member_symbols(self) -> tuple[str, ...]:
        return tuple(member.symbol for member in self.symbols)

    def active_members(self, as_of: date) -> tuple[WatchlistMemberConfig, ...]:
        """Membership dates are inclusive; the complete inventory is unchanged."""
        return tuple(
            member
            for member in self.symbols
            if member.start_date <= as_of and (member.end_date is None or as_of <= member.end_date)
        )

    @property
    def benchmark_symbols(self) -> tuple[str, ...]:
        return self.selection_policy.benchmark_symbols


class RawCandidateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    company_name: str
    source_files: tuple[str, ...] = ()
    futu_industries: tuple[str, ...] = ()
    watchlist_role: Literal[
        "core_ai_hardware",
        "infra_enabler",
        "ai_platform_software",
        "hedge_overlay",
        "satellite",
        "watch_only",
    ]
    candidate_bucket: Literal[
        "ai_hardware",
        "ai_infrastructure",
        "ai_software",
        "defensive_healthcare",
        "defensive_staples",
        "defensive_utilities",
        "defensive_financials",
        "speculative_satellite",
        "exclude_or_review",
    ]
    ai_exposure: Literal["direct", "indirect", "hedge", "none"]
    suggested_action: Literal[
        "promote_to_ai_watchlist",
        "keep_as_satellite",
        "use_as_hedge_overlay",
        "watch_only",
        "exclude_or_manual_review",
    ]
    reason: str
    added_by: Literal["futu", "codex"]
    source_urls: tuple[str, ...] = ()

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper().replace(".", "-")


class RawCandidatePoolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    universe_type: Literal["RAW_RESEARCH_CANDIDATES"]
    description: str
    source_directory: str
    symbols: tuple[RawCandidateConfig, ...]

    @model_validator(mode="after")
    def symbols_are_unique(self) -> RawCandidatePoolConfig:
        symbols = [member.symbol for member in self.symbols]
        if len(symbols) != len(set(symbols)):
            raise ValueError("raw candidate symbols must be unique")
        return self

    @property
    def member_symbols(self) -> tuple[str, ...]:
        return tuple(member.symbol for member in self.symbols)


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


def load_raw_candidate_pool_config(path: Path) -> RawCandidatePoolConfig:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"raw candidate pool config must be a mapping: {path}")
    return RawCandidatePoolConfig.model_validate(payload)
