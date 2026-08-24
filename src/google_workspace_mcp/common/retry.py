"""Retry transient Google failures."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from google.auth.exceptions import RefreshError, TransportError


def is_retryable_google_error(exc: BaseException) -> bool:
    """Classify retryable Google error."""
    if isinstance(exc, RefreshError):
        return exc.retryable is True
    return isinstance(exc, (TransportError, TimeoutError, ConnectionError))


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """Configure bounded retry delays."""

    max_attempts: int = 3
    initial_delay: float = 0.5
    max_delay: float = 16.0
    backoff_factor: float = 2.0
    jitter: bool = True

    def calculate_delay(
        self,
        attempt: int,
        *,
        random_fn: Callable[[float, float], float] = random.uniform,
    ) -> float:
        """Calculate bounded retry delay."""
        raw_delay = self.initial_delay * (self.backoff_factor ** (attempt - 1))
        capped_delay = min(raw_delay, self.max_delay)
        if self.jitter:
            return random_fn(0.0, capped_delay)
        return capped_delay


def execute_with_retry[T](
    operation: Callable[[], T],
    *,
    config: RetryConfig | None = None,
    is_retryable: Callable[[BaseException], bool] = is_retryable_google_error,
    sleep_fn: Callable[[float], None] = time.sleep,
    random_fn: Callable[[float, float], float] = random.uniform,
) -> T:
    """Execute operation with retries."""
    resolved = config or RetryConfig()
    attempt = 1
    while True:
        try:
            return operation()
        except Exception as exc:
            if attempt >= resolved.max_attempts or not is_retryable(exc):
                raise
            delay = resolved.calculate_delay(
                attempt,
                random_fn=random_fn,
            )
            sleep_fn(delay)
            attempt += 1
