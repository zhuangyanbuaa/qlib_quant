from datetime import UTC, datetime
from pathlib import Path

from quant_system.ingestion.base import (
    ProviderCompanyEvent,
    ProviderCompanyEventResult,
    ProviderNewsArticle,
    ProviderNewsResult,
)
from quant_system.ingestion.news import NewsUpdateConfig, NewsUpdateService
from quant_system.ingestion.reliability import DailyCallBudget, ProviderGuard, RetryPolicy
from quant_system.quality.reports import PipelineStatus
from quant_system.sentiment.classifier import RuleBasedSentimentScorer, SentimentResult
from quant_system.sentiment.mapping import AliasResolver, CompanyMapping
from quant_system.storage.parquet import ParquetRepository


class FakeNewsProvider:
    name = "fake_news"
    version = "1"

    def fetch_news(self, symbols, *, start_utc, end_utc):
        return ProviderNewsResult(
            articles=(
                ProviderNewsArticle(
                    article_id="n1",
                    published_at_utc=datetime(2026, 7, 1, 20, tzinfo=UTC),
                    title="Nvidia faces DOJ investigation",
                    summary="Probe expands.",
                    url="https://example.com/nvda?utm_source=test",
                    source_domain="example.com",
                    language="en",
                    raw_tickers=("NVDA",),
                    raw_topics=("technology",),
                ),
            )
        )


class FakeEventProvider:
    name = "fake_sec"
    version = "1"

    def fetch_events(self, symbols, *, start_utc, end_utc):
        return ProviderCompanyEventResult(
            events=(
                ProviderCompanyEvent(
                    cik="0001045810",
                    symbol=symbols[0],
                    form_type="8-K",
                    accession_number="0001045810-26-000001",
                    filed_at_utc=datetime(2026, 7, 1, tzinfo=UTC),
                    accepted_at_utc=datetime(2026, 7, 1, 21, tzinfo=UTC),
                    filing_url="https://www.sec.gov/Archives/edgar/data/1045810/x/y.htm",
                ),
            )
        )


def guard() -> ProviderGuard:
    return ProviderGuard(
        budget=DailyCallBudget(10),
        limiter=NoopLimiter(),
        retry=RetryPolicy(attempts=1),
    )


class NoopLimiter:
    def acquire(self) -> None:
        return None


class CountingScorer:
    def __init__(self) -> None:
        self.calls = 0

    def score(self, *, title: str, summary: str) -> SentimentResult:
        self.calls += 1
        return SentimentResult(label="positive", score=0.25)


class FailingScorer:
    def score(self, *, title: str, summary: str) -> SentimentResult:
        raise RuntimeError("model unavailable")


def test_news_update_service_classifies_and_writes_traceable_records(tmp_path) -> None:
    repository = ParquetRepository(tmp_path)
    service = NewsUpdateService(
        repository=repository,
        report_directory=tmp_path / "reports",
        resolver=AliasResolver(
            (CompanyMapping(symbol="NVDA", cik="1045810", aliases=("Nvidia",)),)
        ),
        scorer=RuleBasedSentimentScorer(),
        config=NewsUpdateConfig(alpha_vantage_batch_size=10),
        news_provider=FakeNewsProvider(),
        news_guard=guard(),
        event_provider=FakeEventProvider(),
        event_guard=guard(),
    )

    report, report_path = service.run(
        ("NVDA",),
        start_utc=datetime(2026, 7, 1, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, tzinfo=UTC),
    )
    articles = repository.read_news_articles().to_pylist()
    events = repository.read_company_events().to_pylist()

    assert report.article_rows_written == 1
    assert report.event_rows_written == 1
    assert report_path.exists()
    assert articles[0]["matched_symbols"] == ["NVDA"]
    assert articles[0]["severity"] == "HIGH"
    assert events[0]["event_id"].startswith("sec:")
    assert report.sentiment_status is PipelineStatus.SUCCESS


def test_news_update_service_uses_article_level_sentiment_cache(tmp_path) -> None:
    cache_dir = tmp_path / "cache"
    scorer = CountingScorer()
    service = _news_service(
        tmp_path=tmp_path,
        scorer=scorer,
        cache_dir=cache_dir,
    )

    first_report, _ = service.run(
        ("NVDA",),
        start_utc=datetime(2026, 7, 1, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, tzinfo=UTC),
        dry_run=True,
    )
    cached_service = _news_service(
        tmp_path=tmp_path,
        scorer=FailingScorer(),
        cache_dir=cache_dir,
    )
    second_report, _ = cached_service.run(
        ("NVDA",),
        start_utc=datetime(2026, 7, 1, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, tzinfo=UTC),
        dry_run=True,
    )

    assert scorer.calls == 1
    assert first_report.sentiment_cache_misses == 1
    assert second_report.sentiment_cache_hits == 1
    assert second_report.sentiment_degraded_count == 0
    assert second_report.sentiment_status is PipelineStatus.SUCCESS


def test_news_update_service_degrades_to_rule_based_sentiment_on_model_failure(
    tmp_path,
) -> None:
    service = _news_service(
        tmp_path=tmp_path,
        scorer=FailingScorer(),
        cache_dir=tmp_path / "cache",
    )

    report, _ = service.run(
        ("NVDA",),
        start_utc=datetime(2026, 7, 1, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, tzinfo=UTC),
    )
    article = ParquetRepository(tmp_path).read_news_articles().to_pylist()[0]

    assert report.status is PipelineStatus.DEGRADED
    assert report.sentiment_status is PipelineStatus.DEGRADED
    assert report.sentiment_degraded_count == 1
    assert article["sentiment_label"] == "negative"
    assert "sentiment_status:DEGRADED" in article["quality_flags"]


def _news_service(
    *,
    tmp_path: Path,
    scorer,
    cache_dir: Path,
) -> NewsUpdateService:
    return NewsUpdateService(
        repository=ParquetRepository(tmp_path),
        report_directory=tmp_path / "reports",
        resolver=AliasResolver(
            (CompanyMapping(symbol="NVDA", cik="1045810", aliases=("Nvidia",)),)
        ),
        scorer=scorer,
        config=NewsUpdateConfig(
            alpha_vantage_batch_size=10,
            sentiment_cache_directory=cache_dir,
            sentiment_model_key="test-model:v1",
            sentiment_timeout_seconds=1,
        ),
        news_provider=FakeNewsProvider(),
        news_guard=guard(),
    )
