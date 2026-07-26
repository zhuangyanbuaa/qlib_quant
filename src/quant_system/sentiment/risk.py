"""Point-in-time news risk aggregation and veto decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from quant_system.domain.enums import EventSeverity


@dataclass(frozen=True)
class NewsRiskAssessment:
    """Symbol-level risk snapshot visible at one historical cutoff."""

    symbol: str
    severity: EventSeverity = EventSeverity.LOW
    average_sentiment_score: float = 0.0
    article_count: int = 0
    event_count: int = 0
    article_references: tuple[dict[str, Any], ...] = ()
    event_references: tuple[dict[str, Any], ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def is_veto(self) -> bool:
        return self.severity is EventSeverity.HIGH

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "severity": self.severity.value,
            "average_sentiment_score": self.average_sentiment_score,
            "article_count": self.article_count,
            "event_count": self.event_count,
            "article_references": list(self.article_references),
            "event_references": list(self.event_references),
            "reasons": list(self.reasons),
            "is_veto": self.is_veto,
        }


def assess_news_risk(
    *,
    symbols: tuple[str, ...],
    articles: pd.DataFrame,
    events: pd.DataFrame,
    cutoff_utc: datetime,
    lookback_hours: int,
) -> dict[str, NewsRiskAssessment]:
    """Aggregate deduplicated news and SEC events using only visible facts."""
    start_utc = cutoff_utc - timedelta(hours=lookback_hours)
    results: dict[str, NewsRiskAssessment] = {}
    for symbol in tuple(dict.fromkeys(symbol.upper() for symbol in symbols)):
        symbol_articles = _filter_articles(articles, symbol, start_utc, cutoff_utc)
        symbol_events = _filter_events(events, symbol, start_utc, cutoff_utc)
        results[symbol] = _assess_symbol(symbol, symbol_articles, symbol_events)
    return results


def _assess_symbol(
    symbol: str,
    articles: pd.DataFrame,
    events: pd.DataFrame,
) -> NewsRiskAssessment:
    article_refs = tuple(_article_reference(row) for _, row in articles.head(5).iterrows())
    event_refs = tuple(_event_reference(row) for _, row in events.head(5).iterrows())
    reasons: list[str] = []
    severity = EventSeverity.LOW
    avg_score = float(articles["sentiment_score"].mean()) if not articles.empty else 0.0

    if _has_high(articles, "severity"):
        severity = EventSeverity.HIGH
        reasons.append("high_severity_news")
    if _has_high(events, "severity"):
        severity = EventSeverity.HIGH
        reasons.append("high_severity_sec_event")
    if severity is not EventSeverity.HIGH:
        if _has_medium(articles, "severity") or _has_medium(events, "severity"):
            severity = EventSeverity.MEDIUM
            reasons.append("medium_severity_event")
        elif avg_score <= -0.35:
            severity = EventSeverity.MEDIUM
            reasons.append("negative_sentiment_cluster")

    return NewsRiskAssessment(
        symbol=symbol,
        severity=severity,
        average_sentiment_score=avg_score,
        article_count=len(articles),
        event_count=len(events),
        article_references=article_refs,
        event_references=event_refs,
        reasons=tuple(reasons),
    )


def _filter_articles(
    articles: pd.DataFrame,
    symbol: str,
    start_utc: datetime,
    cutoff_utc: datetime,
) -> pd.DataFrame:
    if articles.empty:
        return articles
    frame = articles.copy()
    frame["available_at_utc"] = pd.to_datetime(frame["available_at_utc"], utc=True)
    frame["published_at_utc"] = pd.to_datetime(frame["published_at_utc"], utc=True)
    visible = frame.loc[
        (frame["available_at_utc"] <= pd.Timestamp(cutoff_utc))
        & (frame["published_at_utc"] >= pd.Timestamp(start_utc))
        & (frame["published_at_utc"] <= pd.Timestamp(cutoff_utc))
        & frame["matched_symbols"].map(lambda values: symbol in set(values))
    ].copy()
    if "dedupe_key" in visible.columns:
        visible = visible.drop_duplicates("dedupe_key", keep="first")
    return visible.sort_values(["severity", "published_at_utc"], ascending=[True, False])


def _filter_events(
    events: pd.DataFrame,
    symbol: str,
    start_utc: datetime,
    cutoff_utc: datetime,
) -> pd.DataFrame:
    if events.empty:
        return events
    frame = events.copy()
    frame["available_at_utc"] = pd.to_datetime(frame["available_at_utc"], utc=True)
    frame["accepted_at_utc"] = pd.to_datetime(frame["accepted_at_utc"], utc=True)
    visible = frame.loc[
        (frame["available_at_utc"] <= pd.Timestamp(cutoff_utc))
        & (frame["accepted_at_utc"] >= pd.Timestamp(start_utc))
        & (frame["accepted_at_utc"] <= pd.Timestamp(cutoff_utc))
        & (frame["symbol"] == symbol)
    ].copy()
    if "event_id" in visible.columns:
        visible = visible.drop_duplicates("event_id", keep="first")
    return visible.sort_values(["severity", "accepted_at_utc"], ascending=[True, False])


def _has_high(frame: pd.DataFrame, column: str) -> bool:
    return not frame.empty and (frame[column].astype(str) == EventSeverity.HIGH.value).any()


def _has_medium(frame: pd.DataFrame, column: str) -> bool:
    return not frame.empty and (frame[column].astype(str) == EventSeverity.MEDIUM.value).any()


def _article_reference(row: pd.Series) -> dict[str, Any]:
    return {
        "article_id": row["article_id"],
        "published_at_utc": row["published_at_utc"].isoformat(),
        "title": row["title"],
        "url": row["url"],
        "severity": row["severity"],
        "sentiment_score": float(row["sentiment_score"]),
    }


def _event_reference(row: pd.Series) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "accepted_at_utc": row["accepted_at_utc"].isoformat(),
        "form_type": row["form_type"],
        "filing_url": row["filing_url"],
        "severity": row["severity"],
    }
