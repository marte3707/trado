import time

import pytest

from app.services._circuit import CircuitBreaker, CircuitState


def test_breaker_starts_closed_and_allows_calls() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=3, reset_after_seconds=1.0)
    assert cb.state is CircuitState.CLOSED
    assert cb.allow() is True


def test_breaker_opens_after_consecutive_failures() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=3, reset_after_seconds=10.0)
    for _ in range(3):
        cb.record_failure()
    assert cb.state is CircuitState.OPEN
    assert cb.allow() is False


def test_breaker_success_resets_failure_count() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=3, reset_after_seconds=10.0)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    assert cb.state is CircuitState.CLOSED  # only 2 consecutive, threshold is 3


def test_breaker_enters_half_open_after_cooldown() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=1, reset_after_seconds=0.05)
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    assert cb.allow() is False
    time.sleep(0.06)
    assert cb.allow() is True
    assert cb.state is CircuitState.HALF_OPEN


def test_breaker_closes_after_successful_probe() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=1, reset_after_seconds=0.05)
    cb.record_failure()
    time.sleep(0.06)
    cb.allow()  # transitions to half-open
    cb.record_success()
    assert cb.state is CircuitState.CLOSED


def test_breaker_reopens_after_failed_probe() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=1, reset_after_seconds=0.05)
    cb.record_failure()
    time.sleep(0.06)
    cb.allow()  # transitions to half-open
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
