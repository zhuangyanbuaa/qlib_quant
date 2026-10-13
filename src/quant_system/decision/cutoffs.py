"""Forward news knowledge may advance without changing historical price signals."""

from datetime import UTC, date, datetime

from quant_system.domain.clocks import NyseSessionClock


def resolve_news_cutoff(
    signal_session: date, generated_at_utc: datetime, requested: datetime | None
):
    clock = NyseSessionClock()
    price_cutoff = clock.available_at_utc(signal_session)
    if requested is None:
        return price_cutoff
    if requested.tzinfo is None or requested.utcoffset() is None:
        raise ValueError("news cutoff must be timezone-aware")
    cutoff = requested.astimezone(UTC)
    if cutoff < price_cutoff or cutoff > generated_at_utc:
        raise ValueError("news cutoff must lie between price cutoff and report creation")
    if cutoff >= clock.session_open_utc(clock.next_session(signal_session)):
        raise ValueError("forward premarket news cutoff must precede the earliest order time")
    return cutoff
