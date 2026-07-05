import pytest

from quant_system.ingestion.base import TransientProviderError
from quant_system.ingestion.reliability import (
    DailyCallBudget,
    ProviderGuard,
    RequestBudgetExceeded,
    RetryPolicy,
    SlidingWindowRateLimiter,
)


def test_daily_budget_blocks_calls_over_limit() -> None:
    budget = DailyCallBudget(1)
    budget.consume()

    with pytest.raises(RequestBudgetExceeded):
        budget.consume()


def test_guard_retries_only_transient_errors() -> None:
    calls = 0
    sleeps: list[float] = []

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TransientProviderError("temporary")
        return "ok"

    guard = ProviderGuard(
        budget=DailyCallBudget(3),
        limiter=SlidingWindowRateLimiter(60),
        retry=RetryPolicy(attempts=3, initial_backoff_seconds=1, jitter_seconds=0),
        sleeper=sleeps.append,
        random_uniform=lambda _start, _end: 0,
    )

    assert guard.call(operation) == "ok"
    assert calls == 3
    assert sleeps == [1, 2]


def test_guard_does_not_retry_programming_errors() -> None:
    guard = ProviderGuard(
        budget=DailyCallBudget(3),
        limiter=SlidingWindowRateLimiter(60),
        retry=RetryPolicy(attempts=3),
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(ValueError, match="bad input"):
        guard.call(lambda: (_ for _ in ()).throw(ValueError("bad input")))
    assert guard.budget.used == 1
