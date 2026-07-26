"""Company alias and ticker mapping for news and SEC events."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class CompanyMapping(BaseModel):
    """One canonical trading symbol and its external identifiers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1)
    cik: str | None = None
    aliases: tuple[str, ...] = ()
    sector: str | None = None


@dataclass(frozen=True)
class AliasResolver:
    """Resolve provider tickers and company-name aliases to canonical symbols."""

    mappings: tuple[CompanyMapping, ...]

    def __post_init__(self) -> None:
        if not self.mappings:
            raise ValueError("at least one company mapping is required")

    @property
    def cik_by_symbol(self) -> dict[str, str]:
        return {
            mapping.symbol.upper(): mapping.cik
            for mapping in self.mappings
            if mapping.cik is not None
        }

    @property
    def symbol_by_cik(self) -> dict[str, str]:
        return {
            mapping.cik.zfill(10): mapping.symbol.upper()
            for mapping in self.mappings
            if mapping.cik is not None
        }

    def resolve_tickers(self, raw_tickers: tuple[str, ...]) -> tuple[str, ...]:
        """Map provider ticker strings to known canonical symbols."""
        known = {mapping.symbol.upper() for mapping in self.mappings}
        resolved = []
        for ticker in raw_tickers:
            symbol = ticker.split(":", maxsplit=1)[-1].upper().strip()
            if symbol in known and symbol not in resolved:
                resolved.append(symbol)
        return tuple(resolved)

    def match_text(self, text: str) -> tuple[str, ...]:
        """Resolve company aliases mentioned in a title or summary."""
        lowered = text.lower()
        matches = []
        for mapping in self.mappings:
            symbol = mapping.symbol.upper()
            candidates = (symbol, *mapping.aliases)
            if any(candidate.lower() in lowered for candidate in candidates):
                matches.append(symbol)
        return tuple(dict.fromkeys(matches))
