from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from quant_system.ingestion.budgets import SQLiteDailyCallBudget
from quant_system.ingestion.reliability import RequestBudgetExceeded


def consume_in_process(path):
    budget = SQLiteDailyCallBudget(5, database_path=path, provider="fixture")
    try:
        budget.consume()
        return True
    except RequestBudgetExceeded:
        return False


def test_concurrent_processes_cannot_exceed_shared_budget(tmp_path):
    path = tmp_path / "operations.sqlite"
    SQLiteDailyCallBudget(5, database_path=path, provider="fixture")
    with ProcessPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(consume_in_process, [path] * 12))
    assert sum(outcomes) == 5
    assert SQLiteDailyCallBudget(5, database_path=path, provider="fixture").used == 5


def test_restart_provider_isolation_and_utc_rollover(tmp_path):
    now = [datetime(2026, 10, 12, 23, 59, tzinfo=UTC)]
    kwargs = dict(
        database_path=tmp_path / "operations.sqlite", provider="prices", clock=lambda: now[0]
    )
    original = SQLiteDailyCallBudget(1, **kwargs)
    original.consume()
    restarted = SQLiteDailyCallBudget(1, **kwargs)
    assert restarted.used == 1
    with pytest.raises(RequestBudgetExceeded):
        restarted.consume()
    other = SQLiteDailyCallBudget(
        1, database_path=kwargs["database_path"], provider="news", clock=lambda: now[0]
    )
    other.consume()
    now[0] += timedelta(minutes=2)
    assert restarted.used == 0
    restarted.consume()
    assert restarted.used == 1


def test_conflicting_limits_use_the_smallest_seen_today(tmp_path):
    kwargs = dict(database_path=tmp_path / "operations.sqlite", provider="fixture")
    smaller = SQLiteDailyCallBudget(1, **kwargs)
    larger = SQLiteDailyCallBudget(20, **kwargs)
    smaller.consume()
    with pytest.raises(RequestBudgetExceeded):
        larger.consume()


def test_retries_and_report_attempts_are_counted_independently(tmp_path):
    from quant_system.ingestion.base import TransientProviderError
    from quant_system.ingestion.reliability import (
        ProviderGuard,
        RetryPolicy,
        SlidingWindowRateLimiter,
    )

    kwargs = dict(database_path=tmp_path / "operations.sqlite", provider="fixture")
    other = SQLiteDailyCallBudget(5, **kwargs)
    other.consume()
    guard = ProviderGuard(
        budget=SQLiteDailyCallBudget(5, **kwargs),
        limiter=SlidingWindowRateLimiter(1000),
        retry=RetryPolicy(attempts=2),
        sleeper=lambda _: None,
    )
    with pytest.raises(TransientProviderError):
        guard.call(lambda: (_ for _ in ()).throw(TransientProviderError("temporary")))
    assert guard.budget.used == 3
    assert guard.attempts_used == 2
