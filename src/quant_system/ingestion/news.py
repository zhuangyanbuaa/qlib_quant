"""Point-in-time news and SEC event update orchestration."""

from __future__ import annotations

import concurrent.futures
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from quant_system.domain.coverage import NewsCoverageObservation
from quant_system.domain.enums import NewsEventType
from quant_system.domain.models import CompanyEvent, NewsArticle
from quant_system.ingestion.base import (
    CompanyEventProvider,
    NewsProvider,
    ProviderError,
    ProviderNewsArticle,
    ProviderUnavailableError,
)
from quant_system.ingestion.reliability import ProviderGuard, RequestBudgetExceeded
from quant_system.logging import get_logger
from quant_system.quality.reports import NewsUpdateReport, PipelineStatus
from quant_system.sentiment.cache import SentimentCache
from quant_system.sentiment.classifier import (
    RuleBasedSentimentScorer,
    SentimentResult,
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
    sentiment_cache_directory: Path | None = None
    sentiment_cache_enabled: bool = True
    sentiment_timeout_seconds: float = 20
    sentiment_model_key: str = "rule_based"

    def __post_init__(self) -> None:
        if self.alpha_vantage_batch_size < 1:
            raise ValueError("alpha_vantage_batch_size must be positive")
        if self.sentiment_timeout_seconds <= 0:
            raise ValueError("sentiment_timeout_seconds must be positive")


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
        self.fallback_scorer = RuleBasedSentimentScorer()
        self.config = config
        self.sentiment_cache = (
            SentimentCache(config.sentiment_cache_directory)
            if config.sentiment_cache_enabled and config.sentiment_cache_directory is not None
            else None
        )
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
        if end_utc > started_at:
            raise ValueError("news coverage cannot certify a future interval")
        requested = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
        if not requested:
            raise ValueError("at least one symbol is required")

        warnings: dict[str, str] = {}
        failed_sources: dict[str, str] = {}
        raw_articles: list[ProviderNewsArticle] = []
        coverage_results = {
            (symbol, channel): ("UNAVAILABLE", 0, "source_not_configured")
            for symbol in requested
            for channel in ("news", "sec")
        }
        alpha_calls_before = self.news_guard.attempts_used if self.news_guard else 0
        sec_calls_before = self.event_guard.attempts_used if self.event_guard else 0

        if self.news_provider and self.news_guard:
            known_symbols = {mapping.symbol.upper() for mapping in self.resolver.mappings}
            for symbol in requested:
                coverage_results[(symbol, "news")] = (
                    "UNAVAILABLE",
                    0,
                    "query_not_attempted"
                    if symbol in known_symbols
                    else "symbol_mapping_not_configured",
                )
            try:
                for batch in self._symbol_batches(
                    tuple(symbol for symbol in requested if symbol in known_symbols)
                ):
                    result = self.news_guard.call(
                        lambda batch=batch: self.news_provider.fetch_news(
                            batch,
                            start_utc=start_utc,
                            end_utc=end_utc,
                        )
                    )
                    raw_articles.extend(result.articles)
                    limited = len(result.articles) >= getattr(
                        self.news_provider, "limit_per_call", float("inf")
                    )
                    partial = bool(result.warnings) or limited
                    for symbol in batch:
                        count = sum(
                            symbol
                            in {
                                *self.resolver.resolve_tickers(article.raw_tickers),
                                *self.resolver.match_text(f"{article.title} {article.summary}"),
                            }
                            for article in result.articles
                        )
                        coverage_results[(symbol, "news")] = (
                            "PARTIAL" if partial else ("SUCCESS" if count else "EMPTY"),
                            count,
                            "provider_warning_or_limit" if partial else "",
                        )
                        if symbol not in {
                            mapping.symbol.upper() for mapping in self.resolver.mappings
                        }:
                            coverage_results[(symbol, "news")] = (
                                "UNAVAILABLE",
                                count,
                                "symbol_mapping_not_configured",
                            )
                    warnings.update(
                        {f"news:{key}": value for key, value in result.warnings.items()}
                    )
            except (ProviderError, RequestBudgetExceeded) as error:
                failed_sources[self.news_provider.name] = f"{type(error).__name__}: {error}"
                for symbol in batch:
                    coverage_results[(symbol, "news")] = (
                        "UNAVAILABLE" if isinstance(error, ProviderUnavailableError) else "FAILED",
                        0,
                        f"{type(error).__name__}: query failed",
                    )

        raw_events = []
        if self.event_provider and self.event_guard:
            for symbol in requested:
                if (
                    hasattr(self.event_provider, "cik_by_symbol")
                    and symbol not in self.event_provider.cik_by_symbol
                ):
                    coverage_results[(symbol, "sec")] = ("UNAVAILABLE", 0, "missing_cik")
                    warnings[f"sec:{symbol}"] = "missing_cik"
                    continue
                try:
                    result = self.event_guard.call(
                        lambda symbol=symbol: self.event_provider.fetch_events(
                            (symbol,),
                            start_utc=start_utc,
                            end_utc=end_utc,
                        )
                    )
                    raw_events.extend(result.events)
                    warning = result.warnings.get(symbol)
                    coverage_results[(symbol, "sec")] = (
                        "UNAVAILABLE"
                        if warning == "missing_cik"
                        else (
                            "PARTIAL"
                            if result.warnings
                            else ("SUCCESS" if result.events else "EMPTY")
                        ),
                        len(result.events),
                        warning or ("provider_warning" if result.warnings else ""),
                    )
                    warnings.update({f"sec:{key}": value for key, value in result.warnings.items()})
                except (ProviderError, RequestBudgetExceeded) as error:
                    failed_sources[f"{self.event_provider.name}:{symbol}"] = (
                        f"{type(error).__name__}: {error}"
                    )
                    coverage_results[(symbol, "sec")] = (
                        "FAILED",
                        0,
                        f"{type(error).__name__}: query failed",
                    )

        news_records = []
        sentiment_counts: Counter[str] = Counter()
        cache_hits = 0
        cache_misses = 0
        for article in raw_articles:
            record, sentiment_metadata = self._to_news_article(
                article,
                run_id=run_id,
                fetched_at_utc=started_at,
            )
            news_records.append(record)
            sentiment_counts.update((sentiment_metadata["status"],))
            cache_hits += int(bool(sentiment_metadata["cache_hit"]))
            cache_misses += int(not bool(sentiment_metadata["cache_hit"]))
            if sentiment_metadata["status"] == PipelineStatus.DEGRADED.value:
                warnings[f"sentiment:{article.article_id}"] = str(sentiment_metadata["reason"])
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

        completed_at = datetime.now(UTC)
        coverage_records = []
        for (symbol, channel), (result, count, reason) in coverage_results.items():
            provider = self.news_provider if channel == "news" else self.event_provider
            coverage_records.append(
                NewsCoverageObservation(
                    coverage_id=f"{run_id}:{channel}:{symbol}",
                    symbol=symbol,
                    channel=channel,
                    window_start_utc=start_utc,
                    window_end_utc=end_utc,
                    result=result,
                    rows_returned=count,
                    reason=reason,
                    source=provider.name
                    if provider
                    else ("alpha_vantage_news" if channel == "news" else "sec_submissions"),
                    source_version=f"{provider.version if provider else 'unconfigured'}/coverage-1",
                    fetched_at_utc=completed_at,
                    available_at_utc=completed_at,
                    ingestion_run_id=run_id,
                    is_stale=False,
                    quality_flags=("bounded_query_observation",),
                )
            )
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
            written_files.extend(
                self.repository.write_news_coverage(
                    coverage_records, run_id=run_id, batch_id="coverage"
                )
            )

        blocked_reasons = tuple(
            f"{source}:{reason}" for source, reason in sorted(failed_sources.items())
        )
        sentiment_degraded_count = sentiment_counts[PipelineStatus.DEGRADED.value]
        sentiment_status = (
            PipelineStatus.DEGRADED if sentiment_degraded_count else PipelineStatus.SUCCESS
        )

        incomplete_coverage = any(
            record.result not in {"SUCCESS", "EMPTY"} for record in coverage_records
        )
        if failed_sources and not news_records and not event_records:
            status = PipelineStatus.BLOCKED
        elif (
            incomplete_coverage
            or failed_sources
            or warnings
            or sentiment_status is PipelineStatus.DEGRADED
        ):
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
            sentiment_status=sentiment_status,
            sentiment_cache_hits=cache_hits,
            sentiment_cache_misses=cache_misses,
            sentiment_degraded_count=sentiment_degraded_count,
            alpha_vantage_calls=(
                self.news_guard.attempts_used - alpha_calls_before if self.news_guard else 0
            ),
            sec_calls=(
                self.event_guard.attempts_used - sec_calls_before if self.event_guard else 0
            ),
            warnings=dict(sorted(warnings.items())),
            failed_sources=dict(sorted(failed_sources.items())),
            blocked_reasons=blocked_reasons,
            coverage_rows_written=0 if dry_run else len(coverage_records),
            coverage_results={
                f"{record.channel}:{record.symbol}": record.result for record in coverage_records
            },
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
        batch_size = (
            self.config.alpha_vantage_batch_size
            if getattr(self.news_provider, "supports_symbol_union", True)
            else 1
        )
        return [
            symbols[offset : offset + batch_size] for offset in range(0, len(symbols), batch_size)
        ]

    def _to_news_article(
        self,
        article: ProviderNewsArticle,
        *,
        run_id: UUID,
        fetched_at_utc: datetime,
    ) -> tuple[NewsArticle, dict[str, Any]]:
        raw_matches = self.resolver.resolve_tickers(article.raw_tickers)
        alias_matches = self.resolver.match_text(f"{article.title} {article.summary}")
        matched_symbols = tuple(dict.fromkeys((*raw_matches, *alias_matches)))
        dedupe_key = article_dedupe_key(url=article.url, title=article.title)
        sentiment, sentiment_metadata = self._score_article(
            article=article,
            dedupe_key=dedupe_key,
        )
        event_type = classify_event(article.title, article.summary, article.raw_topics)
        severity = classify_severity(
            title=article.title,
            summary=article.summary,
            event_type=event_type,
            sentiment_score=sentiment.score,
        )
        canonical_url = canonicalize_url(article.url)
        record = NewsArticle(
            article_id=article.article_id,
            published_at_utc=article.published_at_utc,
            title=article.title,
            summary=article.summary,
            url=article.url,
            source_domain=article.source_domain,
            canonical_url=canonical_url,
            dedupe_key=dedupe_key,
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
            quality_flags=_sentiment_quality_flags(sentiment_metadata),
        )
        return record, sentiment_metadata

    def _score_article(
        self,
        *,
        article: ProviderNewsArticle,
        dedupe_key: str,
    ) -> tuple[SentimentResult, dict[str, Any]]:
        article_key = article.article_id or dedupe_key
        model_key = self.config.sentiment_model_key
        if self.sentiment_cache is not None:
            cached = self.sentiment_cache.get(
                article_key=article_key,
                model_key=model_key,
            )
            if cached is not None:
                return cached.to_result(), {
                    "status": PipelineStatus.SUCCESS.value,
                    "cache_hit": True,
                    "reason": "cache_hit",
                }

        try:
            sentiment = _score_with_timeout(
                self.scorer,
                title=article.title,
                summary=article.summary,
                timeout_seconds=self.config.sentiment_timeout_seconds,
            )
        except (TimeoutError, RuntimeError) as error:
            fallback = self.fallback_scorer.score(
                title=article.title,
                summary=article.summary,
            )
            return fallback, {
                "status": PipelineStatus.DEGRADED.value,
                "cache_hit": False,
                "reason": f"{type(error).__name__}: {error}",
            }

        if self.sentiment_cache is not None:
            self.sentiment_cache.put(
                article_key=article_key,
                model_key=model_key,
                result=sentiment,
            )
        return sentiment, {
            "status": PipelineStatus.SUCCESS.value,
            "cache_hit": False,
            "reason": "scored",
        }


def _score_with_timeout(
    scorer: SentimentScorer,
    *,
    title: str,
    summary: str,
    timeout_seconds: float,
) -> SentimentResult:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(scorer.score, title=title, summary=summary)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as error:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise TimeoutError(f"sentiment scoring exceeded {timeout_seconds}s") from error
    finally:
        if future.done():
            executor.shutdown(wait=True)


def _sentiment_quality_flags(metadata: dict[str, Any]) -> tuple[str, ...]:
    if metadata["status"] != PipelineStatus.DEGRADED.value:
        return ()
    return (
        "sentiment_status:DEGRADED",
        f"sentiment_reason:{metadata['reason']}",
    )
