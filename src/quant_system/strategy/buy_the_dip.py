"""Rules-only Buy-the-Dip candidate generation."""

from __future__ import annotations

from datetime import date
from uuid import NAMESPACE_URL, uuid5

import pandas as pd

from quant_system.domain.clocks import NyseSessionClock
from quant_system.domain.enums import MarketRegime
from quant_system.domain.trading import CandidateSignal
from quant_system.sentiment.risk import NewsRiskAssessment
from quant_system.strategy.config import RulesConfig
from quant_system.strategy.market_regime import classify_market_regime


class BuyTheDipStrategy:
    """Generate identical candidates for daily scans and historical backtests."""

    def __init__(
        self,
        config: RulesConfig,
        *,
        clock: NyseSessionClock | None = None,
    ) -> None:
        self.config = config
        self.clock = clock or NyseSessionClock()

    def generate_signals(
        self,
        features: pd.DataFrame,
        *,
        as_of: date | None = None,
        news_risk: dict[str, NewsRiskAssessment] | None = None,
    ) -> list[CandidateSignal]:
        """Return close-confirmed signals, optionally restricted to one date."""
        frame = self.annotate(features)
        candidates = frame.loc[frame["is_candidate"]].copy()
        if as_of is not None:
            candidates = candidates.loc[
                candidates["session_date_ny"].dt.date == as_of
            ]
        signals = []
        for _, row in candidates.iterrows():
            assessment = news_risk.get(row["symbol"]) if news_risk else None
            if assessment and assessment.is_veto:
                continue
            signals.append(self._to_signal(row, news_risk=assessment))
        return sorted(
            signals,
            key=lambda signal: (
                signal.signal_session,
                -signal.score,
                signal.symbol,
            ),
        )

    def annotate(self, features: pd.DataFrame) -> pd.DataFrame:
        """Attach regime and rule booleans for scans, backtests, and diagnostics."""
        frame = features.copy()
        frame["session_date_ny"] = pd.to_datetime(frame["session_date_ny"])
        benchmark = frame.loc[
            frame["symbol"] == self.config.benchmark_symbol.upper()
        ].copy()
        if benchmark.empty:
            raise ValueError(f"benchmark missing: {self.config.benchmark_symbol}")
        benchmark["market_regime"] = classify_market_regime(
            benchmark,
            yellow_maximum_ma50_decline=self.config.yellow_maximum_ma50_decline,
        )
        regimes = benchmark.set_index("session_date_ny")["market_regime"]
        frame["market_regime"] = frame["session_date_ny"].map(regimes).fillna(
            MarketRegime.RED
        )

        atr_drawdown = (
            frame["rolling_high20"] - frame["close"]
        ) / frame["atr20"].replace(0, pd.NA)
        dip = (
            frame["feature_ready"]
            & (frame["avg_dollar_volume20"] >= self.config.min_average_dollar_volume)
            & (frame["close"] > frame["ma200"])
            & (frame["ma50_slope20"] >= self.config.minimum_ma50_slope)
            & (
                frame["relative_return60"]
                >= self.config.minimum_relative_return_60
            )
            & frame["drawdown_from_high20"].between(
                self.config.minimum_drawdown,
                self.config.maximum_drawdown,
                inclusive="both",
            )
            & frame["rsi14"].between(
                self.config.minimum_rsi,
                self.config.maximum_rsi,
                inclusive="both",
            )
            & (frame["close"] < frame["ma20"])
            & atr_drawdown.between(
                self.config.minimum_atr_drawdown,
                self.config.maximum_atr_drawdown,
                inclusive="both",
            )
        )
        frame["dip_condition"] = dip
        frame["dip_yesterday"] = (
            frame.groupby("symbol", sort=False)["dip_condition"].shift(1).fillna(False)
        )
        reclaimed_ma5 = (frame["close"] > frame["ma5"]) & (
            frame["previous_close"] <= frame["previous_ma5"]
        )
        broke_previous_high = frame["close"] > frame["previous_high"]
        frame["confirmation"] = reclaimed_ma5 | broke_previous_high
        frame["is_candidate"] = (
            frame["dip_yesterday"]
            & frame["confirmation"]
            & frame["feature_ready"]
            & (frame["market_regime"] != MarketRegime.RED)
            & (frame["symbol"] != self.config.benchmark_symbol.upper())
        )
        return frame

    def _to_signal(
        self,
        row: pd.Series,
        *,
        news_risk: NewsRiskAssessment | None = None,
    ) -> CandidateSignal:
        signal_session = row["session_date_ny"].date()
        next_session = self.clock.next_session(signal_session)
        news_references = ()
        news_risk_level = "LOW"
        reasons = ["dip_yesterday", "daily_confirmation", "market_regime_open"]
        if news_risk:
            news_risk_level = news_risk.severity.value
            news_references = (
                *news_risk.article_references,
                *news_risk.event_references,
            )
            if news_risk.reasons:
                reasons.extend(f"news:{reason}" for reason in news_risk.reasons)
        return CandidateSignal(
            signal_id=uuid5(
                NAMESPACE_URL,
                f"buy-the-dip-v1:{row['symbol']}:{signal_session.isoformat()}",
            ),
            symbol=row["symbol"],
            signal_session=signal_session,
            data_cutoff_utc=self.clock.session_close_utc(signal_session),
            signal_time_utc=self.clock.available_at_utc(signal_session),
            earliest_order_session=next_session,
            earliest_order_time_utc=self.clock.session_open_utc(next_session),
            score=float(row["relative_return60"]),
            signal_close=float(row["close"]),
            atr20=float(row["atr20"]),
            market_regime=MarketRegime(row["market_regime"]),
            reasons=tuple(reasons),
            news_risk=news_risk_level,
            news_references=news_references,
        )
