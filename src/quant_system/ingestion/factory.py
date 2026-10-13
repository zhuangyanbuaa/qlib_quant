"""Shared production ingestion construction for CLI and daily orchestration."""

from datetime import UTC, datetime

from quant_system.ingestion.alpha_vantage import AlphaVantageNewsAdapter
from quant_system.ingestion.budgets import SQLiteDailyCallBudget
from quant_system.ingestion.news import NewsUpdateConfig, NewsUpdateService
from quant_system.ingestion.prices import PriceUpdateService
from quant_system.ingestion.reliability import ProviderGuard, SlidingWindowRateLimiter
from quant_system.ingestion.sec import SecCompanyEventAdapter
from quant_system.ingestion.source_health import effective_companies
from quant_system.ingestion.yahoo import YahooFinancePriceAdapter
from quant_system.sentiment.classifier import FinbertSentimentScorer, RuleBasedSentimentScorer
from quant_system.sentiment.mapping import AliasResolver


def build_guard(settings, provider, config):
    return ProviderGuard(
        budget=SQLiteDailyCallBudget(
            config.daily_call_budget,
            database_path=settings.resolved_data_dir / "db" / "operations.sqlite",
            provider=provider.name,
        ),
        limiter=SlidingWindowRateLimiter(config.calls_per_minute),
        retry=config.retry.to_policy(),
    )


def build_news_service(settings, config, repository):
    companies = effective_companies(config, repository, cutoff_utc=datetime.now(UTC))
    resolver = AliasResolver(companies)
    news = (
        AlphaVantageNewsAdapter(
            api_key=settings.alpha_vantage_api_key,
            timeout_seconds=config.alpha_vantage.timeout_seconds,
            limit_per_call=config.alpha_vantage.limit_per_call,
            topics=config.alpha_vantage.topics,
        )
        if config.alpha_vantage.enabled and settings.alpha_vantage_key_configured
        else None
    )
    sec = (
        SecCompanyEventAdapter(
            cik_by_symbol=resolver.cik_by_symbol,
            user_agent=settings.sec_user_agent,
            timeout_seconds=config.sec.timeout_seconds,
            forms=config.sec.forms,
        )
        if config.sec.enabled and settings.sec_contact_configured
        else None
    )
    scorer = RuleBasedSentimentScorer()
    key = "rule_based:v1"
    if config.sentiment.scorer == "finbert":
        scorer = FinbertSentimentScorer(
            model_name=config.sentiment.finbert.model_name,
            model_revision=config.sentiment.finbert.model_revision,
            device=config.sentiment.finbert.device,
        )
        key = (f"finbert:{config.sentiment.finbert.model_name}:"
               f"{config.sentiment.finbert.model_revision}")
    return NewsUpdateService(
        repository=repository,
        report_directory=settings.resolved_data_dir / "reports" / "quality",
        resolver=resolver,
        scorer=scorer,
        config=NewsUpdateConfig(
            alpha_vantage_batch_size=config.alpha_vantage.batch_size,
            high_severity_veto=config.risk.high_severity_veto,
            sentiment_cache_directory=settings.resolved_data_dir / "cache" / "sentiment",
            sentiment_cache_enabled=config.sentiment.finbert.cache_enabled,
            sentiment_timeout_seconds=config.sentiment.finbert.inference_timeout_seconds,
            sentiment_model_key=key,
        ),
        news_provider=news,
        news_guard=build_guard(settings, news, config.alpha_vantage) if news else None,
        event_provider=sec,
        event_guard=build_guard(settings, sec, config.sec) if sec else None,
    )


def build_price_service(settings, config, repository):
    provider = YahooFinancePriceAdapter(timeout_seconds=config.timeout_seconds)
    return PriceUpdateService(
        provider=provider,
        guard=build_guard(settings, provider, config),
        repository=repository,
        database_path=settings.resolved_data_dir / "db" / "analytics.duckdb",
        report_directory=settings.resolved_data_dir / "reports" / "quality",
        config=config.to_update_config(),
    )
