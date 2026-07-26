from datetime import UTC, datetime

from quant_system.ingestion.base import (
    ProviderCompanyEvent,
    ProviderCompanyEventResult,
    ProviderNewsArticle,
    ProviderNewsResult,
)
from quant_system.ingestion.news import NewsUpdateConfig, NewsUpdateService
from quant_system.ingestion.reliability import DailyCallBudget, ProviderGuard, RetryPolicy
from quant_system.sentiment.classifier import RuleBasedSentimentScorer
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
