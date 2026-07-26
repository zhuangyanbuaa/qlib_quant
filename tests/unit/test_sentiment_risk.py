from datetime import UTC, datetime, timedelta

import pandas as pd

from quant_system.domain.enums import EventSeverity
from quant_system.sentiment.classifier import (
    RuleBasedSentimentScorer,
    classify_event,
    classify_severity,
)
from quant_system.sentiment.dedupe import (
    article_dedupe_key,
    canonicalize_url,
    semantic_fingerprint,
)
from quant_system.sentiment.mapping import AliasResolver, CompanyMapping
from quant_system.sentiment.risk import assess_news_risk


def test_url_title_and_semantic_dedupe_are_stable() -> None:
    url = "HTTPS://www.Example.com/a//story/?utm_source=x&b=2&a=1#section"

    assert canonicalize_url(url) == "https://example.com/a/story?a=1&b=2"
    assert article_dedupe_key(url=url, title="Nvidia probe") == article_dedupe_key(
        url="https://example.com/a/story?b=2&a=1",
        title="Different wire title",
    )
    assert semantic_fingerprint("Nvidia DOJ probe", "Probe expands") == semantic_fingerprint(
        "DOJ probe Nvidia",
        "expands probe",
    )


def test_alias_resolver_combines_raw_tickers_and_text_aliases() -> None:
    resolver = AliasResolver(
        (
            CompanyMapping(symbol="NVDA", cik="1045810", aliases=("Nvidia",)),
            CompanyMapping(symbol="AMD", cik="2488", aliases=("Advanced Micro Devices",)),
        )
    )

    assert resolver.resolve_tickers(("CRYPTO:BTC", "nvda")) == ("NVDA",)
    assert resolver.match_text("Advanced Micro Devices launches a new chip") == ("AMD",)
    assert resolver.cik_by_symbol["NVDA"] == "1045810"


def test_classifier_marks_legal_negative_story_as_high_severity() -> None:
    scorer = RuleBasedSentimentScorer()
    sentiment = scorer.score(title="Nvidia faces DOJ investigation", summary="Probe expands")
    event_type = classify_event("Nvidia faces DOJ investigation", "Probe expands", ())

    severity = classify_severity(
        title="Nvidia faces DOJ investigation",
        summary="Probe expands",
        event_type=event_type,
        sentiment_score=sentiment.score,
    )

    assert sentiment.label == "negative"
    assert severity is EventSeverity.HIGH


def test_news_risk_respects_cutoff_and_deduplicates_articles() -> None:
    cutoff = datetime(2026, 7, 1, 22, tzinfo=UTC)
    high_title = "Nvidia faces DOJ investigation"
    duplicate_key = article_dedupe_key(url="https://example.com/nvda", title=high_title)
    articles = pd.DataFrame(
        [
            {
                "article_id": "a1",
                "published_at_utc": cutoff - timedelta(hours=1),
                "available_at_utc": cutoff - timedelta(hours=1),
                "title": high_title,
                "url": "https://example.com/nvda",
                "matched_symbols": ["NVDA"],
                "dedupe_key": duplicate_key,
                "severity": "HIGH",
                "sentiment_score": -0.8,
            },
            {
                "article_id": "a2",
                "published_at_utc": cutoff - timedelta(minutes=30),
                "available_at_utc": cutoff - timedelta(minutes=30),
                "title": high_title,
                "url": "https://wire.example.com/nvda-copy",
                "matched_symbols": ["NVDA"],
                "dedupe_key": duplicate_key,
                "severity": "HIGH",
                "sentiment_score": -0.7,
            },
            {
                "article_id": "future",
                "published_at_utc": cutoff + timedelta(minutes=1),
                "available_at_utc": cutoff + timedelta(minutes=1),
                "title": "Future bad news",
                "url": "https://example.com/future",
                "matched_symbols": ["NVDA"],
                "dedupe_key": "future",
                "severity": "HIGH",
                "sentiment_score": -1.0,
            },
        ]
    )
    events = pd.DataFrame()

    risk = assess_news_risk(
        symbols=("NVDA",),
        articles=articles,
        events=events,
        cutoff_utc=cutoff,
        lookback_hours=72,
    )["NVDA"]

    assert risk.is_veto
    assert risk.article_count == 1
    assert risk.article_references[0]["article_id"] == "a1"
