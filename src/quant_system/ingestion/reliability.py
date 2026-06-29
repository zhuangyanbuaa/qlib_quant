"""Bounded retries, rate limits, and daily request budgets."""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from quant_system.ingestion.base import TransientProviderError

T = TypeVar("T")


class RequestBudgetExceeded(RuntimeError):
    """Raised before a provider's configured daily call budget is exceeded."""


class DailyCallBudget:
    """Thread-safe in-process daily call counter."""

    def __init__(self, maximum_calls: int) -> None:
        if maximum_calls < 1:
            raise ValueError("maximum_calls must be positive")
        self.maximum_calls = maximum_calls
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def consume(self) -> None:
        with self._lock:
            if self._used >= self.maximum_calls:
                raise RequestBudgetExceeded(
                    f"daily provider call budget exhausted ({self.maximum_calls})"
                )
            self._used += 1


class SlidingWindowRateLimiter:
    """Limit calls over a rolling minute without global socket mutation."""

    def __init__(
        self,
        calls_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if calls_per_minute < 1:
            raise ValueError("calls_per_minute must be positive")
        self.calls_per_minute = calls_per_minute
        self._clock = clock
        self._sleeper = sleeper
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= 60:
                    self._calls.popleft()
                if len(self._calls) < self.calls_per_minute:
                    self._calls.append(now)
                    return
                wait_seconds = max(0.0, 60 - (now - self._calls[0]))
            self._sleeper(wait_seconds)


@dataclass(frozen=True)
class RetryPolicy:
    """Retry settings for transient provider failures."""

    attempts: int = 3
    initial_backoff_seconds: float = 1.0
    maximum_backoff_seconds: float = 8.0
    jitter_seconds: float = 0.25

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be positive")
        if min(
            self.initial_backoff_seconds,
            self.maximum_backoff_seconds,
            self.jitter_seconds,
        ) < 0:
            raise ValueError("retry delays must be non-negative")


class ProviderGuard:
    """Apply call budget, rate limit, and retry policy to provider calls."""

    def __init__(
        self,
        *,
        budget: DailyCallBudget,
        limiter: SlidingWindowRateLimiter,
        retry: RetryPolicy,
        sleeper: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.budget = budget
        self.limiter = limiter
        self.retry = retry
        self._sleeper = sleeper
        self._random_uniform = random_uniform

    def call(self, operation: Callable[[], T]) -> T:
        last_error: TransientProviderError | None = None
        for attempt in range(self.retry.attempts):
            self.budget.consume()
            self.limiter.acquire()
            try:
                return operation()
            except TransientProviderError as error:
                last_error = error
                if attempt + 1 == self.retry.attempts:
                    break
                backoff = min(
                    self.retry.maximum_backoff_seconds,
                    self.retry.initial_backoff_seconds * (2**attempt),
                )
                jitter = self._random_uniform(0, self.retry.jitter_seconds)
                self._sleeper(backoff + jitter)
        assert last_error is not None
        raise last_error
