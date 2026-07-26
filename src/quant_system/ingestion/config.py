"""Validated YAML configuration for external price sources."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from quant_system.ingestion.prices import PriceUpdateConfig
from quant_system.ingestion.reliability import RetryPolicy
from quant_system.sentiment.mapping import CompanyMapping


class RetrySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempts: int = Field(default=3, ge=1)
    initial_backoff_seconds: float = Field(default=1, ge=0)
    maximum_backoff_seconds: float = Field(default=8, ge=0)
    jitter_seconds: float = Field(default=0.25, ge=0)

    def to_policy(self) -> RetryPolicy:
        return RetryPolicy(**self.model_dump())


class PriceSourceSettings(BaseModel):
    """Provider and operational limits loaded from prices.yaml."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["yahoo_finance"] = "yahoo_finance"
    bootstrap_start: date
    core_symbols: tuple[str, ...] = ("SPY", "QQQ")
    default_symbols: tuple[str, ...] = ("SPY", "QQQ")
    overlap_sessions: int = Field(default=2, ge=0)
    stale_after_sessions: int = Field(default=1, ge=0)
    max_failure_fraction: float = Field(default=0.1, ge=0, le=1)
    batch_size: int = Field(default=25, ge=1)
    max_workers: int = Field(default=2, ge=1)
    timeout_seconds: float = Field(default=15, gt=0)
    calls_per_minute: int = Field(default=30, ge=1)
    daily_call_budget: int = Field(default=500, ge=1)
    retry: RetrySettings = RetrySettings()

    def to_update_config(self) -> PriceUpdateConfig:
        return PriceUpdateConfig(
            bootstrap_start=self.bootstrap_start,
            core_symbols=tuple(symbol.upper() for symbol in self.core_symbols),
            overlap_sessions=self.overlap_sessions,
            stale_after_sessions=self.stale_after_sessions,
            max_failure_fraction=self.max_failure_fraction,
            batch_size=self.batch_size,
            max_workers=self.max_workers,
        )


def load_price_source_settings(path: Path) -> PriceSourceSettings:
    """Load a strict provider config; unknown keys fail fast."""
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"price source config must be a mapping: {path}")
    return PriceSourceSettings.model_validate(payload)


class AlphaVantageNewsSettings(BaseModel):
    """Alpha Vantage NEWS_SENTIMENT limits and request shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    calls_per_minute: int = Field(default=5, ge=1)
    daily_call_budget: int = Field(default=25, ge=1)
    timeout_seconds: float = Field(default=20, gt=0)
    batch_size: int = Field(default=10, ge=1)
    limit_per_call: int = Field(default=50, ge=1, le=1000)
    topics: tuple[str, ...] = ()
    retry: RetrySettings = RetrySettings()


class SecEventSettings(BaseModel):
    """SEC submissions API limits and form filters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    calls_per_second: int = Field(default=5, ge=1, le=10)
    daily_call_budget: int = Field(default=200, ge=1)
    timeout_seconds: float = Field(default=20, gt=0)
    forms: tuple[str, ...] = ("8-K", "10-Q", "10-K")
    retry: RetrySettings = RetrySettings()

    @property
    def calls_per_minute(self) -> int:
        return self.calls_per_second * 60


class FinbertSettings(BaseModel):
    """Pinned optional FinBERT runtime settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_name: str = "ProsusAI/finbert"
    model_revision: str = "main"
    device: Literal["auto", "cpu", "mps"] = "auto"
    batch_size: int = Field(default=16, ge=1)
    max_articles_per_run: int = Field(default=500, ge=1)


class SentimentSettings(BaseModel):
    """Sentiment scoring configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scorer: Literal["rule_based", "finbert"] = "rule_based"
    finbert: FinbertSettings = FinbertSettings()


class NewsRiskSettings(BaseModel):
    """Risk aggregation and veto settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lookback_hours: int = Field(default=72, ge=1)
    high_severity_veto: bool = True


class NewsSourceSettings(BaseModel):
    """Validated Phase 4 news and SEC source configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alpha_vantage: AlphaVantageNewsSettings = AlphaVantageNewsSettings()
    sec: SecEventSettings = SecEventSettings()
    sentiment: SentimentSettings = SentimentSettings()
    risk: NewsRiskSettings = NewsRiskSettings()
    companies: tuple[CompanyMapping, ...]


def load_news_source_settings(path: Path) -> NewsSourceSettings:
    """Load a strict news-source config; unknown keys fail fast."""
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"news source config must be a mapping: {path}")
    return NewsSourceSettings.model_validate(payload)
