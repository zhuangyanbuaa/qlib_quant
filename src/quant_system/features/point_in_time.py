"""Causal features from immutable daily-price versions."""

from __future__ import annotations

import pandas as pd

from quant_system.domain.clocks import NyseSessionClock
from quant_system.features.technical import TechnicalFeatureConfig, build_technical_features
from quant_system.quality.prices import critical_symbols, price_quality_mask


def build_point_in_time_features(
    versions: pd.DataFrame,
    *,
    benchmark_symbol: str = "QQQ",
    config: TechnicalFeatureConfig | None = None,
    clock: NyseSessionClock | None = None,
) -> pd.DataFrame:
    """Compute each decision row using historical versions visible at its cutoff.

    Ordinary bars are causal inputs to the existing rolling feature builder.
    Late publications and revisions split the calculation into segments: a new
    segment recomputes history after that version becomes visible, while earlier
    output rows remain unchanged. Raw versions are loaded only once.
    """
    if versions.empty:
        return versions.copy()
    clock = clock or NyseSessionClock()
    frame = versions.copy()
    frame["session_date_ny"] = pd.to_datetime(frame["session_date_ny"])
    frame["available_at_utc"] = pd.to_datetime(frame["available_at_utc"], utc=True)
    frame["fetched_at_utc"] = pd.to_datetime(frame["fetched_at_utc"], utc=True)
    dates = frame["session_date_ny"].dt.date
    row_cutoffs = pd.to_datetime(
        dates.map({session: clock.available_at_utc(session) for session in dates.unique()}),
        utc=True,
    )
    sessions = clock.calendar.sessions_in_range(dates.min(), dates.max())
    cutoffs = pd.DatetimeIndex([clock.available_at_utc(session.date()) for session in sessions])
    boundaries = {dates.min()}
    for available in frame.loc[frame["available_at_utc"] > row_cutoffs, "available_at_utc"]:
        position = cutoffs.searchsorted(available)
        if position < len(sessions):
            boundaries.add(sessions[position].date())
    boundaries = sorted(boundaries)
    segments: list[pd.DataFrame] = []
    order = ["fetched_at_utc", *(["filename"] if "filename" in frame else [])]
    for index, start in enumerate(boundaries):
        start_cutoff = pd.Timestamp(clock.available_at_utc(start))
        threshold = row_cutoffs.where(row_cutoffs >= start_cutoff, start_cutoff)
        visible = frame.loc[frame["available_at_utc"] <= threshold]
        snapshot = visible.sort_values(order, kind="stable").drop_duplicates(
            ["symbol", "session_date_ny"],
            keep="last",
        )
        snapshot = snapshot.drop(columns=["filename"], errors="ignore")
        features = build_technical_features(
            snapshot, benchmark_symbol=benchmark_symbol, config=config
        )
        selected = features["session_date_ny"].dt.date >= start
        if index + 1 < len(boundaries):
            selected &= features["session_date_ny"].dt.date < boundaries[index + 1]
        segments.append(features.loc[selected])
    result = (
        pd.concat(segments, ignore_index=True)
        .sort_values(
            ["session_date_ny", "symbol"],
        )
        .reset_index(drop=True)
    )
    result["data_cutoff_utc"] = pd.to_datetime(
        result["session_date_ny"].dt.date.map(
            {session.date(): cutoff for session, cutoff in zip(sessions, cutoffs, strict=True)}
        ),
        utc=True,
    )
    result["price_quality_pass"] = price_quality_mask(result)
    required = set(critical_symbols(benchmark_symbol))
    valid_by_session = (
        result.loc[result["price_quality_pass"]].groupby("session_date_ny")["symbol"].agg(set)
    )
    result["critical_prices_available"] = (
        result["session_date_ny"]
        .map(valid_by_session.map(required.issubset))
        .fillna(False)
        .astype(bool)
    )
    return result
