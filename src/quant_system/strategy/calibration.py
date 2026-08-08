"""Read-only candidate tiers used to calibrate the daily strategy workbench."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from quant_system.domain.trading import CandidateSignal
from quant_system.sentiment.risk import NewsRiskAssessment
from quant_system.strategy.buy_the_dip import BuyTheDipStrategy
from quant_system.strategy.config import RulesConfig

TIER_ORDER = ("STRICT", "BASELINE", "RELAXED")


@dataclass(frozen=True)
class TieredCandidateSignal:
    """A candidate signal annotated with calibration tier membership."""

    signal: CandidateSignal
    calibration_tier: str
    passed_tiers: tuple[str, ...]


def generate_tiered_candidates(
    *,
    features: pd.DataFrame,
    as_of: date,
    base_config: RulesConfig,
    news_risk: dict[str, NewsRiskAssessment] | None = None,
) -> list[TieredCandidateSignal]:
    """Run strict, baseline, and relaxed scans through the canonical strategy class."""
    signals_by_symbol: dict[str, dict[str, CandidateSignal]] = {}
    for tier in TIER_ORDER:
        tier_config = rules_for_tier(base_config, tier)
        tier_signals = BuyTheDipStrategy(tier_config).generate_signals(
            features,
            as_of=as_of,
            news_risk=news_risk,
        )
        for signal in tier_signals:
            signals_by_symbol.setdefault(signal.symbol, {})[tier] = signal

    tiered: list[TieredCandidateSignal] = []
    for tier_signals in signals_by_symbol.values():
        passed_tiers = tuple(tier for tier in TIER_ORDER if tier in tier_signals)
        calibration_tier = passed_tiers[0]
        tiered.append(
            TieredCandidateSignal(
                signal=tier_signals[calibration_tier],
                calibration_tier=calibration_tier,
                passed_tiers=passed_tiers,
            )
        )
    return sorted(
        tiered,
        key=lambda candidate: (
            TIER_ORDER.index(candidate.calibration_tier),
            -candidate.signal.score,
            candidate.signal.symbol,
        ),
    )


def rules_for_tier(base_config: RulesConfig, tier: str) -> RulesConfig:
    """Return deterministic tier-specific rule thresholds."""
    tier = tier.upper()
    if tier == "BASELINE":
        return base_config
    if tier == "STRICT":
        return base_config.model_copy(
            update={
                "minimum_ma50_slope": max(base_config.minimum_ma50_slope, 0.0),
                "minimum_relative_return_60": base_config.minimum_relative_return_60
                + 0.05,
                "minimum_drawdown": max(base_config.minimum_drawdown, 0.07),
                "maximum_drawdown": min(base_config.maximum_drawdown, 0.12),
                "minimum_rsi": max(base_config.minimum_rsi, 32),
                "maximum_rsi": min(base_config.maximum_rsi, 42),
                "minimum_atr_drawdown": max(base_config.minimum_atr_drawdown, 1.25),
                "maximum_atr_drawdown": min(base_config.maximum_atr_drawdown, 2.50),
            }
        )
    if tier == "RELAXED":
        return base_config.model_copy(
            update={
                "minimum_ma50_slope": min(base_config.minimum_ma50_slope, -0.05),
                "minimum_relative_return_60": base_config.minimum_relative_return_60
                - 0.05,
                "minimum_drawdown": min(base_config.minimum_drawdown, 0.03),
                "maximum_drawdown": max(base_config.maximum_drawdown, 0.22),
                "minimum_rsi": max(0.0, base_config.minimum_rsi - 5),
                "maximum_rsi": min(100.0, base_config.maximum_rsi + 10),
                "minimum_atr_drawdown": min(base_config.minimum_atr_drawdown, 0.50),
                "maximum_atr_drawdown": max(base_config.maximum_atr_drawdown, 4.00),
            }
        )
    raise ValueError(f"unknown calibration tier: {tier}")
