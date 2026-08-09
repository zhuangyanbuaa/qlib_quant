"""Manual-only 2x leveraged ETF overlay strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LeverageOverlayRules(BaseModel):
    """Configurable thresholds for the 2x overlay checklist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_market_symbol: str = "QQQ"
    default_sector_etf: str = "SOXX"
    minimum_review_score: int = Field(ge=1, le=8)
    common_stock_preferred_score: int = Field(ge=1, le=8)
    maximum_stop_pct: float = Field(gt=0, le=0.30)
    minimum_pullback_pct: float = Field(ge=0, le=0.30)
    maximum_pullback_pct: float = Field(gt=0, le=0.50)
    maximum_rsi: float = Field(ge=40, le=100)
    maximum_return_3d: float = Field(gt=0, le=1)
    maximum_return_5d: float = Field(gt=0, le=1)
    minimum_relative_return_60: float
    target_risk_reward: float = Field(ge=1)
    risk_reward_tolerance: float = Field(default=0.00001, ge=0, le=0.01)
    tactical_position_fraction_hint: float = Field(gt=0, le=0.20)


class LeverageOverlayConfig(BaseModel):
    """Top-level 2x overlay config."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: LeverageOverlayRules


class LeverageOverlayUniverseMember(BaseModel):
    """One manual-only leveraged-product mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    underlying_symbol: str
    leveraged_etf_symbol: str
    alternative_leveraged_etfs: tuple[str, ...] = ()
    sector_etf: str
    market_symbol: str | None = None
    tier: Literal["primary", "satellite", "index", "watch_only"]
    provider: str
    product_type: Literal["single_stock_2x", "sector_2x", "index_2x"]
    universe_role: str
    notes: str
    source_urls: tuple[str, ...] = ()

    @field_validator(
        "underlying_symbol",
        "leveraged_etf_symbol",
        "sector_etf",
        "market_symbol",
        mode="before",
    )
    @classmethod
    def normalize_optional_symbol(cls, value: str | None) -> str | None:
        return value.upper().replace(".", "-") if isinstance(value, str) else value

    @field_validator("alternative_leveraged_etfs", mode="before")
    @classmethod
    def normalize_alternatives(cls, value: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        return tuple(symbol.upper().replace(".", "-") for symbol in value)


class LeverageOverlayUniverseConfig(BaseModel):
    """Batch scan universe for manual 2x overlay candidates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    universe_type: Literal["LEVERAGE_OVERLAY"]
    description: str
    selection_policy: dict[str, Any]
    pairs: tuple[LeverageOverlayUniverseMember, ...]

    @model_validator(mode="after")
    def pairs_are_unique(self) -> LeverageOverlayUniverseConfig:
        keys = [
            (member.underlying_symbol, member.leveraged_etf_symbol)
            for member in self.pairs
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("leverage overlay pairs must be unique")
        return self


@dataclass(frozen=True)
class LeverageOverlayAssessment:
    """One manual 2x ETF overlay assessment."""

    underlying_symbol: str
    leveraged_etf_symbol: str | None
    sector_etf: str
    signal_session: date
    action: str
    score: int
    max_score: int
    checklist: tuple[dict[str, Any], ...]
    setup_type: str
    entry_reference: float | None
    stop_reference: float | None
    target_reference: float | None
    stop_pct: float | None
    risk_reward_estimate: float | None
    tactical_position_fraction_hint: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "leveraged_etf_symbol": self.leveraged_etf_symbol,
            "sector_etf": self.sector_etf,
            "signal_session": self.signal_session.isoformat(),
            "action": self.action,
            "score": self.score,
            "max_score": self.max_score,
            "checklist": list(self.checklist),
            "setup_type": self.setup_type,
            "entry_reference": self.entry_reference,
            "stop_reference": self.stop_reference,
            "target_reference": self.target_reference,
            "stop_pct": self.stop_pct,
            "risk_reward_estimate": self.risk_reward_estimate,
            "tactical_position_fraction_hint": self.tactical_position_fraction_hint,
            "reasons": list(self.reasons),
        }


def load_leverage_overlay_config(path: Path) -> LeverageOverlayConfig:
    """Load manual 2x overlay config from YAML."""
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"leverage overlay config must be a mapping: {path}")
    config = LeverageOverlayConfig.model_validate(payload)
    if config.strategy.minimum_pullback_pct > config.strategy.maximum_pullback_pct:
        raise ValueError("minimum_pullback_pct must not exceed maximum_pullback_pct")
    if config.strategy.common_stock_preferred_score > config.strategy.minimum_review_score:
        raise ValueError("common_stock_preferred_score must not exceed minimum_review_score")
    return config


def load_leverage_overlay_universe_config(path: Path) -> LeverageOverlayUniverseConfig:
    """Load the manual 2x overlay batch universe from YAML."""
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"leverage overlay universe config must be a mapping: {path}")
    return LeverageOverlayUniverseConfig.model_validate(payload)


def evaluate_leverage_overlay(
    *,
    features: pd.DataFrame,
    signal_session: date,
    underlying_symbol: str,
    leveraged_etf_symbol: str | None,
    sector_etf: str,
    market_symbol: str,
    config: LeverageOverlayConfig,
    catalyst_confirmed: bool = False,
) -> LeverageOverlayAssessment:
    """Evaluate whether a 2x ETF deserves manual-review consideration."""
    rules = config.strategy
    frame = features.copy()
    frame["symbol"] = frame["symbol"].astype("string").str.upper()
    frame["session_date_ny"] = pd.to_datetime(frame["session_date_ny"]).dt.date
    frame = frame.loc[frame["session_date_ny"] <= signal_session].sort_values(
        ["symbol", "session_date_ny"]
    )
    underlying_symbol = underlying_symbol.upper()
    market_symbol = market_symbol.upper()
    sector_etf = sector_etf.upper()
    leveraged_etf_symbol = leveraged_etf_symbol.upper() if leveraged_etf_symbol else None

    underlying = _latest_row(frame, underlying_symbol, signal_session)
    market = _latest_row(frame, market_symbol, signal_session)
    sector = _latest_row(frame, sector_etf, signal_session)
    if underlying is None:
        return _missing_assessment(
            underlying_symbol=underlying_symbol,
            leveraged_etf_symbol=leveraged_etf_symbol,
            sector_etf=sector_etf,
            signal_session=signal_session,
            reason=f"missing_underlying_features:{underlying_symbol}",
            rules=rules,
        )
    if market is None:
        return _missing_assessment(
            underlying_symbol=underlying_symbol,
            leveraged_etf_symbol=leveraged_etf_symbol,
            sector_etf=sector_etf,
            signal_session=signal_session,
            reason=f"missing_market_features:{market_symbol}",
            rules=rules,
        )
    if sector is None:
        return _missing_assessment(
            underlying_symbol=underlying_symbol,
            leveraged_etf_symbol=leveraged_etf_symbol,
            sector_etf=sector_etf,
            signal_session=signal_session,
            reason=f"missing_sector_features:{sector_etf}",
            rules=rules,
        )

    history = frame.loc[frame["symbol"] == underlying_symbol].copy()
    returns = _recent_returns(history)
    setup_type = _setup_type(underlying, rules)
    entry = _float_or_none(underlying.get("close"))
    stop = _stop_reference(underlying, history)
    stop_pct = _stop_pct(entry, stop)
    stop_ok = stop_pct is not None and 0 < stop_pct <= rules.maximum_stop_pct
    target = _target_reference(entry, stop, rules.target_risk_reward)
    risk_reward = _risk_reward(entry, stop, target)

    checklist = (
        _check(
            "market_risk_on",
            _risk_on(market),
            "QQQ/market proxy is above key trend filters.",
            "Market proxy is not risk-on enough for leverage.",
        ),
        _check(
            "industry_risk_on",
            _risk_on(sector) and _relative_ok(sector, rules),
            "Sector ETF is trend-positive and not lagging the benchmark.",
            "Sector ETF does not confirm a 2x long overlay.",
        ),
        _check(
            "common_stock_uptrend",
            _common_stock_uptrend(underlying, rules),
            "Underlying common stock is in an uptrend.",
            "Underlying common stock trend is not confirmed.",
        ),
        _check(
            "technical_structure",
            setup_type in {"BREAKOUT", "FIRST_PULLBACK"},
            f"Technical setup is {setup_type}.",
            f"Technical setup is {setup_type}; not suitable for 2x.",
        ),
        _check(
            "catalyst_confirmed",
            catalyst_confirmed,
            "Catalyst was marked confirmed by the user/news review.",
            "Catalyst is not confirmed; news research is required.",
        ),
        _check(
            "stop_defined",
            stop_ok,
            "Stop can be defined within the configured risk window.",
            "Stop is missing or too wide for a 2x overlay.",
        ),
        _check(
            "risk_reward",
            stop_ok
            and risk_reward is not None
            and risk_reward >= rules.target_risk_reward - rules.risk_reward_tolerance,
            "Draft risk/reward is acceptable.",
            "Draft risk/reward is not acceptable.",
        ),
        _check(
            "not_chasing",
            _not_chasing(underlying, returns, rules),
            "Underlying is not in a FOMO/overextended location.",
            "Underlying looks too extended for 2x chase.",
        ),
    )
    score = sum(1 for item in checklist if item["passed"])
    failed_critical = _failed_critical(checklist)
    reasons = tuple(item["message"] for item in checklist if not item["passed"])
    action = _action(score, failed_critical, catalyst_confirmed, rules)
    return LeverageOverlayAssessment(
        underlying_symbol=underlying_symbol,
        leveraged_etf_symbol=leveraged_etf_symbol,
        sector_etf=sector_etf,
        signal_session=signal_session,
        action=action,
        score=score,
        max_score=len(checklist),
        checklist=checklist,
        setup_type=setup_type,
        entry_reference=entry,
        stop_reference=stop,
        target_reference=target,
        stop_pct=stop_pct,
        risk_reward_estimate=risk_reward,
        tactical_position_fraction_hint=rules.tactical_position_fraction_hint,
        reasons=reasons,
    )


def _latest_row(
    frame: pd.DataFrame,
    symbol: str,
    signal_session: date,
) -> dict[str, Any] | None:
    rows = frame.loc[(frame["symbol"] == symbol) & (frame["session_date_ny"] == signal_session)]
    if rows.empty:
        return None
    return rows.iloc[-1].to_dict()


def _risk_on(row: dict[str, Any]) -> bool:
    close = _float_or_none(row.get("close"))
    ma20 = _float_or_none(row.get("ma20"))
    ma50 = _float_or_none(row.get("ma50"))
    ma50_slope = _float_or_none(row.get("ma50_slope20"))
    return (
        close is not None
        and ma20 is not None
        and ma50 is not None
        and ma50_slope is not None
        and close > ma20
        and close > ma50
        and ma50_slope >= 0
    )


def _relative_ok(row: dict[str, Any], rules: LeverageOverlayRules) -> bool:
    relative = _float_or_none(row.get("relative_return60"))
    return relative is not None and relative >= rules.minimum_relative_return_60


def _common_stock_uptrend(row: dict[str, Any], rules: LeverageOverlayRules) -> bool:
    close = _float_or_none(row.get("close"))
    ma20 = _float_or_none(row.get("ma20"))
    ma50 = _float_or_none(row.get("ma50"))
    ma50_slope = _float_or_none(row.get("ma50_slope20"))
    relative = _float_or_none(row.get("relative_return60"))
    return (
        close is not None
        and ma20 is not None
        and ma50 is not None
        and ma50_slope is not None
        and relative is not None
        and close > ma20
        and ma20 >= ma50
        and ma50_slope > 0
        and relative >= rules.minimum_relative_return_60
    )


def _setup_type(row: dict[str, Any], rules: LeverageOverlayRules) -> str:
    close = _float_or_none(row.get("close"))
    rolling_high = _float_or_none(row.get("rolling_high20"))
    previous_high = _float_or_none(row.get("previous_high"))
    previous_close = _float_or_none(row.get("previous_close"))
    ma20 = _float_or_none(row.get("ma20"))
    drawdown = _float_or_none(row.get("drawdown_from_high20"))
    if None in {close, rolling_high, previous_high, previous_close, ma20, drawdown}:
        return "INSUFFICIENT_DATA"
    if close >= rolling_high * 0.995 and close > previous_high:
        return "BREAKOUT"
    if (
        rules.minimum_pullback_pct <= drawdown <= rules.maximum_pullback_pct
        and close > ma20
        and close > previous_close
    ):
        return "FIRST_PULLBACK"
    if close > ma20:
        return "UPTREND_NO_ENTRY_STRUCTURE"
    return "NO_TREND_STRUCTURE"


def _recent_returns(history: pd.DataFrame) -> dict[str, float | None]:
    close = history["close"].astype(float).reset_index(drop=True)
    return {
        "return_3d": _window_return(close, 3),
        "return_5d": _window_return(close, 5),
    }


def _window_return(close: pd.Series, period: int) -> float | None:
    if len(close) <= period:
        return None
    start = float(close.iloc[-period - 1])
    end = float(close.iloc[-1])
    return None if start <= 0 else end / start - 1


def _stop_reference(row: dict[str, Any], history: pd.DataFrame) -> float | None:
    entry = _float_or_none(row.get("close"))
    ma20 = _float_or_none(row.get("ma20"))
    atr20 = _float_or_none(row.get("atr20"))
    lows = history["low"].astype(float).tail(10)
    swing_low = float(lows.min()) if not lows.empty else None
    candidates = [
        value
        for value in (
            ma20 * 0.99 if ma20 is not None else None,
            swing_low * 0.99 if swing_low is not None else None,
            entry - atr20 if entry is not None and atr20 is not None else None,
        )
        if value is not None and entry is not None and value < entry
    ]
    return round(max(candidates), 4) if candidates else None


def _target_reference(
    entry: float | None,
    stop: float | None,
    target_risk_reward: float,
) -> float | None:
    if entry is None or stop is None or stop >= entry:
        return None
    risk = entry - stop
    return round(entry + risk * target_risk_reward, 4)


def _stop_pct(entry: float | None, stop: float | None) -> float | None:
    if entry is None or stop is None or entry <= 0 or stop >= entry:
        return None
    return round(1 - stop / entry, 6)


def _risk_reward(entry: float | None, stop: float | None, target: float | None) -> float | None:
    if entry is None or stop is None or target is None or stop >= entry:
        return None
    return round((target - entry) / (entry - stop), 6)


def _not_chasing(
    row: dict[str, Any],
    returns: dict[str, float | None],
    rules: LeverageOverlayRules,
) -> bool:
    rsi = _float_or_none(row.get("rsi14"))
    return_3d = returns["return_3d"]
    return_5d = returns["return_5d"]
    return (
        rsi is not None
        and rsi <= rules.maximum_rsi
        and return_3d is not None
        and return_3d <= rules.maximum_return_3d
        and return_5d is not None
        and return_5d <= rules.maximum_return_5d
    )


def _check(name: str, passed: bool, passed_message: str, failed_message: str) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "message": passed_message if passed else failed_message,
    }


def _failed_critical(checklist: tuple[dict[str, Any], ...]) -> bool:
    critical = {
        "market_risk_on",
        "industry_risk_on",
        "common_stock_uptrend",
        "technical_structure",
        "stop_defined",
        "not_chasing",
    }
    return any(item["name"] in critical and not item["passed"] for item in checklist)


def _action(
    score: int,
    failed_critical: bool,
    catalyst_confirmed: bool,
    rules: LeverageOverlayRules,
) -> str:
    if failed_critical:
        return "NO_2X_TRADE"
    if score >= rules.minimum_review_score and catalyst_confirmed:
        return "ALLOW_MANUAL_REVIEW"
    if score >= rules.minimum_review_score:
        return "NEED_CATALYST_REVIEW"
    if score >= rules.common_stock_preferred_score:
        return "COMMON_STOCK_PREFERRED"
    return "NO_2X_TRADE"


def _missing_assessment(
    *,
    underlying_symbol: str,
    leveraged_etf_symbol: str | None,
    sector_etf: str,
    signal_session: date,
    reason: str,
    rules: LeverageOverlayRules,
) -> LeverageOverlayAssessment:
    return LeverageOverlayAssessment(
        underlying_symbol=underlying_symbol,
        leveraged_etf_symbol=leveraged_etf_symbol,
        sector_etf=sector_etf,
        signal_session=signal_session,
        action="NO_2X_TRADE",
        score=0,
        max_score=8,
        checklist=(),
        setup_type="INSUFFICIENT_DATA",
        entry_reference=None,
        stop_reference=None,
        target_reference=None,
        stop_pct=None,
        risk_reward_estimate=None,
        tactical_position_fraction_hint=rules.tactical_position_fraction_hint,
        reasons=(reason,),
    )


def _float_or_none(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)
