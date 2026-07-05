"""Exchange-session clocks used to prevent incomplete daily bars."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import exchange_calendars as xcals
import pandas as pd


class NyseSessionClock:
    """Resolve completed NYSE sessions and their point-in-time availability."""

    def __init__(self, availability_delay: timedelta = timedelta(minutes=30)) -> None:
        self.calendar = xcals.get_calendar("XNYS")
        self.availability_delay = availability_delay

    def latest_completed_session(self, now_utc: datetime) -> date:
        """Return the latest session whose close plus safety delay has passed."""
        now = self._require_utc(now_utc)
        new_york_date = pd.Timestamp(now).tz_convert("America/New_York").date()
        candidate = self.calendar.date_to_session(new_york_date, direction="previous")
        if pd.Timestamp(now) < self.calendar.session_close(candidate) + self.availability_delay:
            candidate = self.calendar.previous_session(candidate)
        return candidate.date()

    def session_close_utc(self, session: date) -> datetime:
        """Return the calendar's actual close, including early-close sessions."""
        return self.calendar.session_close(pd.Timestamp(session)).to_pydatetime()

    def available_at_utc(self, session: date) -> datetime:
        """Return when a final daily bar becomes eligible for ingestion."""
        return self.session_close_utc(session) + self.availability_delay

    def overlap_start(self, last_session: date, overlap_sessions: int) -> date:
        """Move backwards by a configured number of exchange sessions."""
        if overlap_sessions < 0:
            raise ValueError("overlap_sessions must be non-negative")
        current = pd.Timestamp(last_session)
        if not self.calendar.is_session(current):
            current = self.calendar.date_to_session(current, direction="previous")
        for _ in range(overlap_sessions):
            current = self.calendar.previous_session(current)
        return current.date()

    def next_session(self, session: date) -> date:
        """Return the next exchange session after a session label."""
        return self.calendar.next_session(pd.Timestamp(session)).date()

    def session_open_utc(self, session: date) -> datetime:
        """Return the official UTC open for a session."""
        return self.calendar.session_open(pd.Timestamp(session)).to_pydatetime()

    @staticmethod
    def exclusive_end(session: date) -> date:
        """Translate an inclusive target session to provider-exclusive end date."""
        return session + timedelta(days=1)

    @staticmethod
    def _require_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("now_utc must be timezone-aware UTC")
        return value.astimezone(UTC)
