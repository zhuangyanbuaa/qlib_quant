from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from quant_system.decision.hierarchy import load_hierarchy_proxies
from quant_system.decision.premarket import load_decision_symbol_benchmarks, load_decision_universe
from quant_system.decision.rotation import load_rotation_members
from quant_system.domain.securities import SecurityLifecycleEvent
from quant_system.storage.duckdb import DuckDBAnalytics
from quant_system.storage.parquet import ParquetRepository
from quant_system.universe.config import load_watchlist_config
from quant_system.universe.lifecycle import LifecycleResolver


def event(**changes):
    fields = dict(
        event_id="rename",
        security_id="local:OLD",
        symbol="OLD",
        successor_symbol="NEW",
        event_type="SYMBOL_CHANGE",
        effective_session=date(2026, 6, 29),
        source_url="https://example.com/notice",
        source="exchange",
        source_version="fixture-1",
        ingestion_run_id=uuid4(),
        fetched_at_utc=datetime(2026, 6, 30, 10, tzinfo=UTC),
        available_at_utc=datetime(2026, 6, 30, 10, tzinfo=UTC),
    )
    return SecurityLifecycleEvent(**(fields | changes))


def test_visibility_and_effective_session_are_independent():
    fact = event()
    resolver = LifecycleResolver((fact,))
    assert (
        resolver.resolve(
            "OLD", as_of=date(2026, 6, 30), cutoff_utc=datetime(2026, 6, 29, 22, tzinfo=UTC)
        ).symbol
        == "OLD"
    )
    assert (
        resolver.resolve("OLD", as_of=date(2026, 6, 26), cutoff_utc=fact.fetched_at_utc).symbol
        == "OLD"
    )
    resolved = resolver.resolve("OLD", as_of=date(2026, 6, 30), cutoff_utc=fact.fetched_at_utc)
    assert resolved.symbol == "NEW"
    assert resolved.security_id == "local:OLD"
    assert resolved.event_ids == ("rename",)
    assert not resolver.is_tradable("OLD", as_of=date(2026, 6, 30), cutoff_utc=fact.fetched_at_utc)


def test_conversion_does_not_carry_security_identity():
    resolver = LifecycleResolver(
        (event(event_type="SHARE_CONVERSION", successor_security_id="local:NEW"),)
    )
    resolved = resolver.resolve(
        "OLD", as_of=date(2026, 7, 1), cutoff_utc=datetime(2026, 7, 1, tzinfo=UTC)
    )
    assert resolved.symbol == "NEW"
    assert resolved.security_id == "local:NEW"
    with pytest.raises(ValueError):
        event(event_type="SHARE_CONVERSION", successor_security_id="local:OLD")


def test_cessation_resumption_and_correction():
    fact = event(event_type="SUSPENDED", successor_symbol="")
    resumed = event(
        event_id="resume",
        event_type="RESUMED",
        successor_symbol="",
        effective_session=date(2026, 7, 1),
    )
    resolver = LifecycleResolver((fact, resumed))
    assert not resolver.resolve(
        "OLD", as_of=date(2026, 6, 30), cutoff_utc=fact.fetched_at_utc
    ).active
    assert resolver.resolve("OLD", as_of=date(2026, 7, 1), cutoff_utc=fact.fetched_at_utc).active
    correction = fact.model_copy(
        update={
            "event_type": "RESUMED",
            "fetched_at_utc": datetime(2026, 7, 2, tzinfo=UTC),
            "available_at_utc": datetime(2026, 7, 2, tzinfo=UTC),
        }
    )
    resolver = LifecycleResolver((fact, correction))
    assert not resolver.resolve(
        "OLD", as_of=date(2026, 7, 1), cutoff_utc=fact.fetched_at_utc
    ).active
    assert resolver.resolve(
        "OLD", as_of=date(2026, 7, 2), cutoff_utc=correction.fetched_at_utc
    ).active


def test_chained_rename_and_cycle_are_bounded():
    first = event()
    second = event(
        event_id="rename2",
        symbol="NEW",
        successor_symbol="LAST",
        effective_session=date(2026, 7, 1),
    )
    assert (
        LifecycleResolver((first, second))
        .resolve("OLD", as_of=date(2026, 7, 2), cutoff_utc=first.fetched_at_utc)
        .symbol
        == "LAST"
    )
    cycle = second.model_copy(update={"successor_symbol": "OLD"})
    assert (
        not LifecycleResolver((first, cycle))
        .resolve("OLD", as_of=date(2026, 7, 2), cutoff_utc=first.fetched_at_utc)
        .active
    )


def test_lifecycle_parquet_is_append_only_and_preserves_lineage(tmp_path):
    repository = ParquetRepository(tmp_path)
    fact = event()
    files = repository.write_security_lifecycle((fact,), run_id=fact.ingestion_run_id)
    before = files[0].read_bytes()
    assert repository.read_security_lifecycle().to_pylist()[0]["source_url"] == fact.source_url
    with pytest.raises(FileExistsError):
        repository.write_security_lifecycle((fact,), run_id=fact.ingestion_run_id)
    assert files[0].read_bytes() == before
    assert LifecycleResolver.from_repository(repository).events == (fact,)


def test_rename_requires_successor_and_cutoff_requires_timezone():
    with pytest.raises(ValueError):
        event(successor_symbol="")
    with pytest.raises(ValueError):
        LifecycleResolver(()).resolve(
            "OLD", as_of=date(2026, 6, 30), cutoff_utc=datetime(2026, 6, 30)
        )


def test_duckdb_lifecycle_snapshot_filters_before_deduplication(tmp_path):
    repository = ParquetRepository(tmp_path)
    first = event()
    correction = event(
        successor_symbol="CORRECT",
        fetched_at_utc=datetime(2026, 7, 2, tzinfo=UTC),
        available_at_utc=datetime(2026, 7, 2, tzinfo=UTC),
    )
    repository.write_security_lifecycle((first,), run_id=first.ingestion_run_id)
    repository.write_security_lifecycle((correction,), run_id=correction.ingestion_run_id)
    with DuckDBAnalytics(tmp_path / "analytics.duckdb", repository.daily_prices_root) as analytics:
        analytics.refresh_views()
        assert (
            analytics.query_security_lifecycle(cutoff_utc=first.available_at_utc).iloc[0][
                "successor_symbol"
            ]
            == "NEW"
        )
        assert (
            analytics.query_security_lifecycle(cutoff_utc=correction.available_at_utc).iloc[0][
                "successor_symbol"
            ]
            == "CORRECT"
        )


def test_successor_has_stable_identity_and_stale_lifecycle_blocks():
    fact = event()
    resolver = LifecycleResolver((fact,))
    assert (
        resolver.resolve("NEW", as_of=date(2026, 6, 30), cutoff_utc=fact.fetched_at_utc).security_id
        == fact.security_id
    )
    stale = fact.model_copy(update={"is_stale": True})
    assert (
        not LifecycleResolver((stale,))
        .resolve("OLD", as_of=date(2026, 6, 30), cutoff_utc=fact.fetched_at_utc)
        .active
    )


def test_all_decision_membership_loaders_use_dates_and_visible_lifecycle(tmp_path):
    watchlist = load_watchlist_config(Path("configs/universe/ai_watchlist.yaml"))
    payload = watchlist.model_dump(mode="json")
    payload["selection_policy"].update(minimum_size=1, maximum_size=1)
    payload["symbols"] = [
        payload["symbols"][0]
        | {"symbol": "OLD", "start_date": "2026-06-29", "end_date": "2026-06-30"}
    ]
    path = tmp_path / "watchlist.yaml"
    path.write_text(yaml.safe_dump(payload))
    paths = (path,)
    resolver = LifecycleResolver((event(),))
    for as_of, expected in [
        (date(2026, 6, 26), ()),
        (date(2026, 6, 29), ("OLD",)),
        (date(2026, 6, 30), ("NEW",)),
        (date(2026, 7, 1), ()),
    ]:
        symbols, roles = load_decision_universe(paths, as_of=as_of, resolver=resolver)
        assert tuple(symbol for symbol in symbols if roles[symbol] != "benchmark") == expected
        assert (
            tuple(load_decision_symbol_benchmarks(paths, as_of=as_of, resolver=resolver))
            == expected
        )
        assert (
            tuple(
                member.symbol
                for member in load_rotation_members(paths, as_of=as_of, resolver=resolver)
            )
            == expected
        )
        proxies = load_hierarchy_proxies(
            universe_paths=paths,
            benchmark_path=Path("configs/universe/benchmarks.yaml"),
            as_of=as_of,
            resolver=resolver,
        )
        assert tuple(proxy.symbol for proxy in proxies if proxy.layer == "stock") == expected
