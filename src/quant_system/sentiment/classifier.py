"""Deterministic event classification plus optional lazy FinBERT scoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Protocol

from quant_system.domain.enums import EventSeverity, NewsEventType


@dataclass(frozen=True)
class SentimentResult:
    """Normalized article sentiment in the system's canonical range."""

    label: str
    score: float


class SentimentScorer(Protocol):
    """Provider-neutral sentiment scorer."""

    def score(self, *, title: str, summary: str) -> SentimentResult: ...


class RuleBasedSentimentScorer:
    """Small deterministic fallback scorer used when FinBERT is unavailable."""

    negative_terms: ClassVar[dict[str, float]] = {
        "fraud": 0.75,
        "probe": 0.45,
        "investigation": 0.65,
        "lawsuit": 0.55,
        "sued": 0.55,
        "bankruptcy": 0.9,
        "default": 0.8,
        "downgrade": 0.45,
        "misses": 0.4,
        "cuts guidance": 0.7,
        "lower guidance": 0.7,
        "recall": 0.55,
        "breach": 0.55,
        "layoffs": 0.35,
        "outage": 0.35,
        "delay": 0.3,
    }
    positive_terms: ClassVar[dict[str, float]] = {
        "beats": 0.35,
        "raises guidance": 0.55,
        "upgrade": 0.35,
        "approval": 0.35,
        "contract": 0.25,
        "buyback": 0.3,
    }

    def score(self, *, title: str, summary: str) -> SentimentResult:
        text = f"{title} {summary}".lower()
        negative = sum(weight for term, weight in self.negative_terms.items() if term in text)
        positive = sum(weight for term, weight in self.positive_terms.items() if term in text)
        raw_score = max(-1.0, min(1.0, positive - negative))
        if raw_score <= -0.2:
            label = "negative"
        elif raw_score >= 0.2:
            label = "positive"
        else:
            label = "neutral"
        return SentimentResult(label=label, score=raw_score)


class FinbertSentimentScorer:
    """Lazy wrapper around a pinned FinBERT model.

    The wrapper intentionally imports transformers only on first use so Phase 4
    remains runnable without model weights or GPU-specific packages installed.
    """

    def __init__(
        self,
        *,
        model_name: str,
        model_revision: str,
        device: str = "auto",
        max_length: int = 384,
    ) -> None:
        self.model_name = model_name
        self.model_revision = model_revision
        self.device = device
        self.max_length = max_length
        self._pipeline = None

    def score(self, *, title: str, summary: str) -> SentimentResult:
        if self._pipeline is None:
            self._pipeline = self._load_pipeline()
        prediction = self._pipeline(
            f"{title}\n\n{summary}",
            truncation=True,
            max_length=self.max_length,
        )[0]
        label = str(prediction["label"]).lower()
        confidence = float(prediction["score"])
        if "negative" in label:
            score = -confidence
            label = "negative"
        elif "positive" in label:
            score = confidence
            label = "positive"
        else:
            score = 0.0
            label = "neutral"
        return SentimentResult(label=label, score=score)

    def _load_pipeline(self):
        try:
            from transformers import pipeline
        except ImportError as error:  # pragma: no cover - optional dependency path
            raise RuntimeError(
                "FinBERT requires the optional transformers dependency"
            ) from error
        device_arg = -1
        if self.device not in {"auto", "cpu"}:
            device_arg = self.device
        return pipeline(
            "sentiment-analysis",
            model=self.model_name,
            revision=self.model_revision,
            device=device_arg,
        )


def classify_event(title: str, summary: str, topics: tuple[str, ...]) -> NewsEventType:
    """Classify the main event family using transparent keyword rules."""
    text = f"{title} {summary} {' '.join(topics)}".lower()
    if any(term in text for term in ("earnings", "results", "eps", "revenue")):
        return NewsEventType.EARNINGS
    if any(term in text for term in ("guidance", "forecast", "outlook")):
        return NewsEventType.GUIDANCE
    if any(term in text for term in ("merger", "acquisition", "takeover", "m&a")):
        return NewsEventType.M_AND_A
    if any(term in text for term in ("sec", "doj", "ftc", "lawsuit", "probe", "investigation")):
        return NewsEventType.LEGAL_REGULATORY
    if any(term in text for term in ("ceo", "cfo", "resigns", "steps down")):
        return NewsEventType.MANAGEMENT_CHANGE
    if any(term in text for term in ("offering", "debt", "convertible", "secondary")):
        return NewsEventType.FINANCING
    if any(term in text for term in ("upgrade", "downgrade", "price target", "initiates")):
        return NewsEventType.ANALYST_RATING
    if any(term in text for term in ("product", "launch", "shipment", "order")):
        return NewsEventType.PRODUCT
    if any(term in text for term in ("fed", "inflation", "tariff", "rates", "macro")):
        return NewsEventType.MACRO
    return NewsEventType.OTHER


def classify_severity(
    *,
    title: str,
    summary: str,
    event_type: NewsEventType,
    sentiment_score: float,
) -> EventSeverity:
    """Assign severity for risk vetoes; high severity must be explainable."""
    text = f"{title} {summary}".lower()
    high_terms = (
        "fraud",
        "bankruptcy",
        "default",
        "sec charges",
        "doj investigation",
        "accounting irregular",
        "restatement",
        "data breach",
        "criminal",
    )
    medium_terms = (
        "lawsuit",
        "probe",
        "investigation",
        "downgrade",
        "cuts guidance",
        "lower guidance",
        "recall",
        "layoffs",
        "outage",
        "delay",
    )
    if any(term in text for term in high_terms) or sentiment_score <= -0.7:
        return EventSeverity.HIGH
    if (
        any(term in text for term in medium_terms)
        or sentiment_score <= -0.35
        or event_type
        in {
            NewsEventType.LEGAL_REGULATORY,
            NewsEventType.GUIDANCE,
            NewsEventType.FINANCING,
            NewsEventType.MANAGEMENT_CHANGE,
        }
    ):
        return EventSeverity.MEDIUM
    return EventSeverity.LOW


def classify_sec_form_severity(form_type: str) -> EventSeverity:
    """Conservative severity defaults for SEC filings."""
    form = form_type.upper()
    if form in {"8-K", "6-K"}:
        return EventSeverity.HIGH
    if form in {"10-K", "10-Q", "20-F", "40-F", "S-1", "S-3", "424B5"}:
        return EventSeverity.MEDIUM
    return EventSeverity.LOW
