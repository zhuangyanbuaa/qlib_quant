from datetime import UTC, datetime, timedelta
from uuid import uuid4

from quant_system.domain.enums import EventSeverity, NewsEventType
from quant_system.domain.models import CompanyEvent, NewsArticle
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository


def metadata(
    run_id,
    *,
    available_at: datetime,
    fetched_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "source": "fixture",
        "source_version": "1",
        "fetched_at_utc": fetched_at or available_at + timedelta(hours=1),
        "available_at_utc": available_at,
        "ingestion_run_id": run_id,
    }


def make_article(run_id, *, article_id: str, fetched_hour: int = 1) -> NewsArticle:
    published = datetime(2026, 7, 1, 20, tzinfo=UTC)
    return NewsArticle(
        **metadata(
            run_id,
            available_at=published,
            fetched_at=published + timedelta(hours=fetched_hour),
        ),
        article_id=article_id,
        published_at_utc=published,
        title="Nvidia faces DOJ investigation",
        summary="Probe expands.",
        url=f"https://example.com/{article_id}?utm_source=x",
        source_domain="example.com",
        canonical_url=f"https://example.com/{article_id}",
        dedupe_key="same-story",
        semantic_key="same-semantic",
        raw_tickers=("NVDA",),
        raw_topics=("technology",),
        matched_symbols=("NVDA",),
        event_type=NewsEventType.LEGAL_REGULATORY,
        severity=EventSeverity.HIGH,
        sentiment_label="negative",
        sentiment_score=-0.8,
    )


def test_news_and_events_are_append_only_and_queryable_point_in_time(tmp_path) -> None:
    repository = ParquetRepository(tmp_path)
    first_run = uuid4()
    second_run = uuid4()
    repository.write_news_articles(
        [make_article(first_run, article_id="old", fetched_hour=1)],
        run_id=first_run,
    )
    repository.write_news_articles(
        [make_article(second_run, article_id="new", fetched_hour=2)],
        run_id=second_run,
    )
    accepted = datetime(2026, 7, 1, 21, tzinfo=UTC)
    repository.write_company_events(
        [
            CompanyEvent(
                **metadata(first_run, available_at=accepted),
                event_id="sec-1",
                cik="0001045810",
                symbol="NVDA",
                form_type="8-K",
                accession_number="0001045810-26-000001",
                filed_at_utc=datetime(2026, 7, 1, tzinfo=UTC),
                accepted_at_utc=accepted,
                filing_url="https://www.sec.gov/Archives/edgar/data/1045810/x/y.htm",
                event_type=NewsEventType.SEC_FILING,
                severity=EventSeverity.HIGH,
            )
        ],
        run_id=first_run,
    )

    assert repository.read_news_articles().num_rows == 2
    assert repository.read_company_events().num_rows == 1
    with DuckDBAnalytics(
        tmp_path / "db" / "analytics.duckdb",
        repository.daily_prices_root,
    ) as analytics:
        analytics.refresh_views()
        articles = analytics.query_news_articles(
            ("NVDA",),
            cutoff_utc=datetime(2026, 7, 1, 22, tzinfo=UTC),
            lookback_hours=72,
        )
        events = analytics.query_company_events(
            ("NVDA",),
            cutoff_utc=datetime(2026, 7, 1, 22, tzinfo=UTC),
            lookback_hours=72,
        )
        early_articles = analytics.query_news_articles(
            ("NVDA",),
            cutoff_utc=datetime(2026, 7, 1, 19, 59, tzinfo=UTC),
            lookback_hours=72,
        )

    assert len(articles) == 1
    assert articles.iloc[0]["article_id"] == "new"
    assert len(events) == 1
    assert early_articles.empty
