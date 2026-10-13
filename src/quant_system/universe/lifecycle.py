"""Point-in-time lifecycle resolution; never splice provider price series."""

from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from quant_system.domain.clocks import NyseSessionClock
from quant_system.domain.securities import SecurityLifecycleEvent
from quant_system.storage.parquet import ParquetRepository
from quant_system.universe.config import WatchlistConfig


@dataclass(frozen=True)
class ResolvedSecurity:
    symbol: str
    security_id: str
    active: bool
    event_ids: tuple[str, ...] = ()
    reason: str = ""


class LifecycleResolver:
    def __init__(self, events: tuple[SecurityLifecycleEvent, ...]):
        self.events = events

    @classmethod
    def from_repository(cls, repository: ParquetRepository):
        return cls(
            tuple(
                SecurityLifecycleEvent.model_validate(row)
                for row in repository.read_security_lifecycle().to_pylist()
            )
        )

    def resolve(self, symbol: str, *, as_of: date, cutoff_utc: datetime) -> ResolvedSecurity:
        if cutoff_utc.tzinfo is None or cutoff_utc.utcoffset() is None:
            raise ValueError("cutoff_utc must be timezone-aware")
        # Filter before deduplication: a later correction cannot erase an earlier fact.
        versions = {}
        for event in self.events:
            if event.available_at_utc <= cutoff_utc:
                previous = versions.get(event.event_id)
                if previous is None or event.fetched_at_utc > previous.fetched_at_utc:
                    versions[event.event_id] = event
        visible = sorted(
            (event for event in versions.values() if event.effective_session <= as_of),
            key=lambda event: (event.effective_session, event.available_at_utc, event.event_id),
        )
        current = symbol.upper()
        identity = f"local:{current}"
        incoming = [
            event
            for event in visible
            if event.successor_symbol == current
            and event.event_type in {"SYMBOL_CHANGE", "SHARE_CONVERSION"}
        ]
        if incoming:
            relation = incoming[-1]
            identity = (
                relation.successor_security_id
                if relation.event_type == "SHARE_CONVERSION"
                else relation.security_id
            )
        visited = set()
        lineage = []
        while current not in visited:
            visited.add(current)
            facts = [event for event in visible if event.symbol == current]
            if not facts:
                return ResolvedSecurity(current, identity, True, tuple(lineage))
            latest = facts[-1]
            lineage.append(latest.event_id)
            if len(visited) == 1:
                identity = latest.security_id
            if latest.is_stale or "diagnostic_only_not_strategy_input" in latest.quality_flags:
                return ResolvedSecurity(
                    current, identity, False, tuple(lineage), "unreliable_lifecycle_fact"
                )
            if latest.event_type == "RESUMED":
                return ResolvedSecurity(current, identity, True, tuple(lineage))
            if latest.event_type not in {"SYMBOL_CHANGE", "SHARE_CONVERSION"}:
                return ResolvedSecurity(current, identity, False, tuple(lineage), latest.event_type)
            current = latest.successor_symbol
            if latest.event_type == "SHARE_CONVERSION":
                identity = latest.successor_security_id
        return ResolvedSecurity(current, identity, False, tuple(lineage), "lifecycle_cycle")

    def is_tradable(self, symbol: str, *, as_of: date, cutoff_utc: datetime) -> bool:
        resolved = self.resolve(symbol, as_of=as_of, cutoff_utc=cutoff_utc)
        return resolved.active and resolved.symbol == symbol.upper()


def apply_lifecycle_quality(
    frame: pd.DataFrame,
    resolver: LifecycleResolver,
    *,
    as_of: date | None = None,
    cutoff_utc: datetime | None = None,
) -> pd.DataFrame:
    """Derived quality only; raw rows and their original metadata remain immutable."""
    if frame.empty or not resolver.events:
        return frame
    result = frame.copy()
    clock = NyseSessionClock()
    eligible = []
    cache = {}
    for row in result.itertuples():
        session = as_of or pd.Timestamp(row.session_date_ny).date()
        cutoff = cutoff_utc or clock.available_at_utc(session)
        key = (row.symbol, session, cutoff)
        if key not in cache:
            cache[key] = resolver.is_tradable(row.symbol, as_of=session, cutoff_utc=cutoff)
        eligible.append(cache[key])
    valid = pd.Series(eligible, index=result.index)
    result["lifecycle_tradable"] = valid
    prior = result.get("price_quality_pass", pd.Series(True, index=result.index))
    result["price_quality_pass"] = prior.fillna(False) & valid
    return result


def decision_members(
    config: WatchlistConfig, *, as_of: date | None = None, resolver: LifecycleResolver | None = None
):
    members = config.symbols if as_of is None else config.active_members(as_of)
    if resolver is None or as_of is None:
        return members
    cutoff = NyseSessionClock().available_at_utc(as_of)
    selected = []
    for member in members:
        resolved = resolver.resolve(member.symbol, as_of=as_of, cutoff_utc=cutoff)
        if resolved.active:
            selected.append(member.model_copy(update={"symbol": resolved.symbol}))
    return tuple(selected)
