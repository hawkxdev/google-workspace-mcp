"""Bound cooperative operation phases."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event

_LOGGER = logging.getLogger(__name__)
_current: ContextVar[OperationDeadline | None] = ContextVar(
    'operation_deadline', default=None
)


class OperationTimeout(TimeoutError):
    """Report scrubbed operation expiry."""


class OperationDeadline:
    """Track one monotonic operation."""

    def __init__(
        self,
        total_seconds: float,
        attempt_seconds: float,
        backoff_seconds: float = 0.5,
        *,
        request_id: str = 'unavailable',
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize operation timing state."""
        self.clock = clock
        self.started = clock()
        self.expires = self.started + total_seconds
        self.attempt_seconds = attempt_seconds
        self.backoff_seconds = backoff_seconds
        self.phase = 'credentials'
        self.cancelled = Event()
        self.cleanups: list[Callable[[], None]] = []
        self.request_id = request_id

    def elapsed(self) -> float:
        """Return monotonic elapsed seconds."""
        return max(0.0, self.clock() - self.started)

    def timeout(self) -> OperationTimeout:
        """Create scrubbed timeout diagnostic."""
        return OperationTimeout(
            f'Gmail read timed out: phase={self.phase} '
            f'elapsed_seconds={self.elapsed():.3f} '
            f'request_id={self.request_id}'
        )

    def remaining(self) -> float:
        """Require remaining phase budget."""
        value = self.expires - self.clock()
        if self.cancelled.is_set() or value <= 0:
            raise self.timeout()
        return value

    def pause(self, delay: float) -> None:
        """Wait within remaining budget."""
        with self.measure('backoff'):
            if delay >= self.remaining():
                raise self.timeout()
            if self.cancelled.wait(delay):
                raise self.timeout()
            self.remaining()

    @contextmanager
    def measure(self, phase: str) -> Generator[None]:
        """Record scrubbed phase timing."""
        self.phase = phase
        began = self.clock()
        self.remaining()
        outcome = 'error'
        try:
            yield
            self.remaining()
            outcome = 'success'
        except TimeoutError:
            outcome = 'timeout'
            raise
        finally:
            _LOGGER.info(
                'gmail_read_phase phase=%s outcome=%s '
                'phase_seconds=%.3f elapsed_seconds=%.3f request_id=%s',
                phase,
                outcome,
                self.clock() - began,
                self.elapsed(),
                self.request_id,
            )


def current_deadline() -> OperationDeadline | None:
    """Return optional operation deadline."""
    return _current.get()


@contextmanager
def bind_deadline(deadline: OperationDeadline) -> Generator[None]:
    """Bind operation timing context."""
    token = _current.set(deadline)
    try:
        yield
    finally:
        _current.reset(token)
