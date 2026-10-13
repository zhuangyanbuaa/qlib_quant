"""Read-only source readiness and point-in-time reviewed mapping selection."""

from datetime import datetime

from quant_system.domain.companies import ReviewedCompanyMapping
from quant_system.ingestion.config import NewsSourceSettings
from quant_system.sentiment.mapping import CompanyMapping
from quant_system.settings import Settings
from quant_system.storage.parquet import ParquetRepository
from quant_system.universe.lifecycle import LifecycleResolver


def effective_companies(
    config: NewsSourceSettings, repository: ParquetRepository, *, cutoff_utc: datetime
):
    mappings = {mapping.symbol.upper(): mapping for mapping in config.companies}
    blocked_symbols = set()
    facts = [
        ReviewedCompanyMapping.model_validate(row)
        for row in repository.read_company_mappings().to_pylist()
    ]
    visible = sorted(
        (fact for fact in facts if fact.available_at_utc <= cutoff_utc),
        key=lambda fact: (fact.fetched_at_utc, fact.available_at_utc, fact.mapping_id),
    )
    for fact in visible:
        if fact.is_stale or any(
            flag == "quality_blocked" or flag.startswith("invalid_") for flag in fact.quality_flags
        ):
            mappings.pop(fact.symbol, None)
            blocked_symbols.add(fact.symbol)
        else:
            blocked_symbols.discard(fact.symbol)
            mappings[fact.symbol] = CompanyMapping(
                symbol=fact.symbol, cik=fact.cik, aliases=fact.aliases
            )
    # Only identity-preserving renames carry company identifiers forward.
    resolver = LifecycleResolver.from_repository(repository)
    from zoneinfo import ZoneInfo

    as_of = cutoff_utc.astimezone(ZoneInfo("America/New_York")).date()
    resolved = {}
    for symbol, mapping in mappings.items():
        current = resolver.resolve(symbol, as_of=as_of, cutoff_utc=cutoff_utc)
        if (
            current.active
            and current.symbol not in blocked_symbols
            and (current.symbol == symbol or current.security_id == f"local:{symbol}")
        ):
            value = mapping.model_copy(update={"symbol": current.symbol})
            if current.symbol == symbol:
                resolved[current.symbol] = value
            else:
                resolved.setdefault(current.symbol, value)
    return tuple(resolved[symbol] for symbol in sorted(resolved))


def check_sources(
    settings: Settings,
    config: NewsSourceSettings,
    repository: ParquetRepository,
    symbols: tuple[str, ...],
    *,
    cutoff_utc: datetime,
):
    mappings = {
        row.symbol: row for row in effective_companies(config, repository, cutoff_utc=cutoff_utc)
    }
    requested = sorted(set(symbol.upper() for symbol in symbols))
    from zoneinfo import ZoneInfo

    resolver = LifecycleResolver.from_repository(repository)
    resolved = {
        symbol: resolver.resolve(
            symbol,
            as_of=cutoff_utc.astimezone(ZoneInfo("America/New_York")).date(),
            cutoff_utc=cutoff_utc,
        )
        for symbol in requested
    }
    requested = sorted({value.symbol for value in resolved.values() if value.active})
    sources = {
        "news": {
            "enabled": config.alpha_vantage.enabled,
            "configured": settings.alpha_vantage_key_configured,
            "entitlement": "UNVERIFIED"
            if settings.alpha_vantage_key_configured
            else "NOT_CONFIGURED",
        },
        "sec": {
            "enabled": config.sec.enabled,
            "configured": settings.sec_contact_configured,
            "entitlement": "NOT_REQUIRED",
        },
    }
    return {
        "status": "READY_FOR_PROBE"
        if all(source["enabled"] and source["configured"] for source in sources.values())
        else "UNAVAILABLE",
        "checked_at_utc": cutoff_utc.isoformat(),
        "sources": sources,
        "resolved_symbols": {
            symbol: value.symbol
            for symbol, value in resolved.items()
            if value.active and symbol != value.symbol
        },
        "inactive_symbols": {
            symbol: value.reason for symbol, value in resolved.items() if not value.active
        },
        "mapped_symbols": [symbol for symbol in requested if symbol in mappings],
        "unmapped_symbols": [symbol for symbol in requested if symbol not in mappings],
        "missing_cik_symbols": [
            symbol for symbol in requested if symbol not in mappings or not mappings[symbol].cik
        ],
    }
