from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpen(RuntimeError):
    """Raised when a call is blocked because the breaker is open."""


@dataclass
class CircuitBreaker:
    """Minimal in-process circuit breaker.

    Threshold semantics:
      * `failure_threshold` consecutive failures flip the breaker to OPEN.
      * After `reset_after_seconds` from the OPEN transition, the next call
        is allowed in HALF_OPEN mode. A success closes the breaker; a failure
        re-opens it and restarts the cooldown.
    """

    name: str
    failure_threshold: int = 5
    reset_after_seconds: float = 30.0
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _lock: Lock = field(default_factory=Lock, init=False)

    @property
    def state(self) -> CircuitState:
        return self._state

    def _now(self) -> float:
        return time.monotonic()

    def allow(self) -> bool:
        """Return True if a call is allowed to proceed, and (if so) move the
        breaker to HALF_OPEN when its cooldown has elapsed."""
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                if self._now() - self._opened_at >= self.reset_after_seconds:
                    self._state = CircuitState.HALF_OPEN
                    return True
                return False
            return True  # HALF_OPEN: allow one probing call

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            if (
                self._state == CircuitState.HALF_OPEN
                or self._failure_count >= self.failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = self._now()

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._opened_at = 0.0


# Single shared breaker for all Hyperliquid info endpoint calls. Worker and API
# share the same process, so a single instance is enough.
hyperliquid_info_breaker = CircuitBreaker(
    name="hyperliquid_info",
    failure_threshold=5,
    reset_after_seconds=30.0,
)
