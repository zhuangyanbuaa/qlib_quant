"""Point-in-time news and SEC event update orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from quant_system.domain.enums import NewsEventType
from quant_system.domain.models import CompanyEvent, NewsArticle
from quant_system.ingestion.base import (
    CompanyEventProvider,
    NewsProvider,
    ProviderError,
    ProviderNewsArticle,
)
from quant_system.ingestion.reliability import ProviderGuard, RequestBudgetExceeded
from quant_system.logging import get_logger
from quant_system.quality.reports import NewsUpdateReport, PipelineStatus
from quant_system.sentiment.classifier import (
    SentimentScorer,
    classify_event,
    classify_sec_form_severity,
    classify_severity,
)
from quant_system.sentiment.dedupe import (
    article_dedupe_key,
    canonicalize_url,
    semantic_fingerprint,
    stable_hash,
)
from quant_system.sentiment.mapping import AliasResolver
from quant_system.storage.parquet import ParquetRepository

logger = get_logger(__name__)


@dataclass(frozen=True)
class NewsUpdateConfig:
    """Operational settings for one news/event update."""

    alpha_vantage_batch_size: int = 10
    high_severity_veto: bool = True

    def __post_init__(self) -> None:
        if self.alpha_vantage_batch_size < 1:
            raise ValueError("alpha_vantage_batch_size must be positive")


class NewsUpdateService:
    """Fetch, classify, and persist point-in-time news facts."""

    def __init__(
        self,
        *,
        repository: ParquetRepository,
        report_directory: Path,
        resolver: AliasResolver,
        scorer: SentimentScorer,
        config: NewsUpdateConfig,
        news_provider: NewsProvider | None = None,
        news_guard: ProviderGuard | None = None,
        event_provider: CompanyEventProvider | None = None,
        event_guard: ProviderGuard | None = None,
    ) -> None:
        self.repository = repository
        self.report_directory = report_directory
        self.resolver = resolver
        self.scorer = scorer
        self.config = config
        self.news_provider = news_provider
        self.news_guard = news_guard
        self.event_provider = event_provider
        self.event_guard = event_guard

    def run(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
        run_id: UUID | None = None,
        dry_run: bool = False,
    ) -> tuple[NewsUpdateReport, Path]:
        """Execute one bounded news/event update."""
        if start_utc.utcoffset() is None or end_utc.utcoffset() is None:
            raise ValueError("news update bounds must be timezone-aware")
        start_utc = start_utc.astimezone(UTC)
        end_utc = end_utc.astimezone(UTC)
        if start_utc > end_utc:
            raise ValueError("start_utc must not follow end_utc")
        run_id = run_id or uuid4()
        started_at = datetime.now(UTC)
        requested = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
        if not requested:
            raise ValueError("at least one symbol is required")

        warnings: dict[str, str] = {}
        failed_sources: dict[str, str] = {}
        raw_articles: list[ProviderNewsArticle] = []
        alpha_calls_before = self.news_guard.budget.used if self.news_guard else 0
        sec_calls_before = self.event_guard.budget.used if self.event_guard else 0

        if self.news_provider and self.news_guard:
            try:
                for batch in self._symbol_batches(requested):
                    result = self.news_guard.call(
                        lambda batch=batch: self.news_provider.fetch_news(
                            batch,
                            start_utc=start_utc,
                            end_utc=end_utc,
                        )
                    )
                    raw_articles.extend(result.articles)
                    warnings.update(
                        {f"news:{key}": value for key, value in result.warnings.items()}
                    )
            except (ProviderError, RequestBudgetExceeded) as error:
                failed_sources[self.news_provider.name] = f"{type(error).__name__}: {error}"

        raw_events = []
        if self.event_provider and self.event_guard:
            for symbol in requested:
                try:
                    result = self.event_guard.call(
                        lambda symbol=symbol: self.event_provider.fetch_events(
                            (symbol,),
                            start_utc=start_utc,
                            end_utc=end_utc,
                        )
                    )
                    raw_events.extend(result.events)
                    warnings.update({f"sec:{key}": value for key, value in result.warnings.items()})
                except (ProviderError, RequestBudgetExceeded) as error:
                    failed_sources[f"{self.event_provider.name}:{symbol}"] = (
                        f"{type(error).__name__}: {error}"
                    )

        news_records = [
            self._to_news_article(article, run_id=run_id, fetched_at_utc=started_at)
            for article in raw_articles
        ]
        event_records = [
            CompanyEvent(
                event_id=stable_hash(
                    f"{event.cik}|{event.accession_number}|{event.form_type}",
                    prefix="sec",
                ),
                cik=event.cik,
                symbol=event.symbol,
                form_type=event.form_type,
                accession_number=event.accession_number,
                filed_at_utc=event.filed_at_utc,
                accepted_at_utc=event.accepted_at_utc,
                filing_url=event.filing_url,
                event_type=NewsEventType.SEC_FILING,
                severity=classify_sec_form_severity(event.form_type),
                source=self.event_provider.name if self.event_provider else "sec_submissions",
                source_version=self.event_provider.version if self.event_provider else "unknown",
                fetched_at_utc=started_at,
                available_at_utc=event.accepted_at_utc,
                ingestion_run_id=run_id,
                is_stale=False,
                quality_flags=(),
            )
            for event in raw_events
        ]

        written_files = []
        if not dry_run:
            written_files.extend(
                self.repository.write_news_articles(
                    news_records,
                    run_id=run_id,
                    batch_id="alpha_vantage",
                )
            )
            written_files.extend(
                self.repository.write_company_events(
                    event_records,
                    run_id=run_id,
                    batch_id="sec",
                )
            )

        blocked_reasons = tuple(
            f"{source}:{reason}" for source, reason in sorted(failed_sources.items())
        )
        if failed_sources and not news_records and not event_records:
            status = PipelineStatus.BLOCKED
        elif failed_sources or warnings:
            status = PipelineStatus.DEGRADED
        else:
            status = PipelineStatus.SUCCESS

        report = NewsUpdateReport(
            run_id=run_id,
            started_at_utc=started_at,
            completed_at_utc=datetime.now(UTC),
            status=status,
            requested_symbols=requested,
            start_utc=start_utc,
            end_utc=end_utc,
            article_rows_written=0 if dry_run else len(news_records),
            event_rows_written=0 if dry_run else len(event_records),
            parquet_files_written=len(written_files),
            alpha_vantage_calls=(
                self.news_guard.budget.used - alpha_calls_before if self.news_guard else 0
            ),
            sec_calls=(self.event_guard.budget.used - sec_calls_before if self.event_guard else 0),
            warnings=dict(sorted(warnings.items())),
            failed_sources=dict(sorted(failed_sources.items())),
            blocked_reasons=blocked_reasons,
        )
        report_path = report.write(self.report_directory)
        logger.info(
            "news_update_completed",
            extra={
                "run_id": str(run_id),
                "status": status.value,
                "article_rows_written": report.article_rows_written,
                "event_rows_written": report.event_rows_written,
                "report_path": str(report_path),
            },
        )
        return report, report_path

    def _symbol_batches(self, symbols: tuple[str, ...]) -> list[tuple[str, ...]]:
        return [
            symbols[offset : offset + self.config.alpha_vantage_batch_size]
            for offset in range(0, len(symbols), self.config.alpha_vantage_batch_size)
        ]

    def _to_news_article(
        self,
        article: ProviderNewsArticle,
        *,
        run_id: UUID,
        fetched_at_utc: datetime,
    ) -> NewsArticle:
        raw_matches = self.resolver.resolve_tickers(article.raw_tickers)
        alias_matches = self.resolver.match_text(f"{article.title} {article.summary}")
        matched_symbols = tuple(dict.fromkeys((*raw_matches, *alias_matches)))
        sentiment = self.scorer.score(title=article.title, summary=article.summary)
        event_type = classify_event(article.title, article.summary, article.raw_topics)
        severity = classify_severity(
            title=article.title,
            summary=article.summary,
            event_type=event_type,
            sentiment_score=sentiment.score,
        )
        canonical_url = canonicalize_url(article.url)
        return NewsArticle(
            article_id=article.article_id,
            published_at_utc=article.published_at_utc,
            title=article.title,
            summary=article.summary,
            url=article.url,
            source_domain=article.source_domain,
            canonical_url=canonical_url,
            dedupe_key=article_dedupe_key(url=article.url, title=article.title),
            semantic_key=semantic_fingerprint(article.title, article.summary),
            language=article.language,
            raw_tickers=article.raw_tickers,
            raw_topics=article.raw_topics,
            matched_symbols=matched_symbols,
            event_type=event_type,
            severity=severity,
            sentiment_label=sentiment.label,
            sentiment_score=sentiment.score,
            source=self.news_provider.name if self.news_provider else "news_provider",
            source_version=self.news_provider.version if self.news_provider else "unknown",
            fetched_at_utc=fetched_at_utc,
            available_at_utc=article.published_at_utc,
            ingestion_run_id=run_id,
            is_stale=False,
            quality_flags=(),
        )
