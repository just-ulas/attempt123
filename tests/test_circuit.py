"""Circuit breaker birim testleri."""

import threading
import time
from unittest.mock import AsyncMock, Mock

import pytest

from attempt import (
    CircuitBreaker,
    CircuitOpenError,
    RetryError,
    async_attempt,
    attempt,
)


def test_starts_closed():
    br = CircuitBreaker(failure_threshold=3, recovery_timeout=1.0)
    assert br.state == "closed"
    assert br.allow() is True
    assert br.seconds_until_retry() == 0.0


def test_opens_after_threshold():
    br = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0, name="api")
    br.record_failure()
    br.record_failure()
    assert br.state == "closed"
    br.record_failure()
    assert br.state == "open"
    assert br.allow() is False
    assert br.seconds_until_retry() > 0


def test_success_resets_consecutive_failures():
    br = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0)
    br.record_failure()
    br.record_failure()
    br.record_success()
    br.record_failure()
    assert br.state == "closed"


def test_half_open_success_closes():
    br = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05, success_threshold=2)
    br.record_failure()
    assert br.state == "open"
    time.sleep(0.06)
    assert br.state == "half_open"
    assert br.allow() is True
    br.record_success()
    assert br.state == "half_open"
    br.allow()
    br.record_success()
    assert br.state == "closed"


def test_half_open_failure_reopens():
    br = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05)
    br.record_failure()
    time.sleep(0.06)
    assert br.state == "half_open"
    br.allow()
    br.record_failure()
    assert br.state == "open"
    assert br.allow() is False


def test_half_open_rejects_extra_probes():
    br = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05, max_half_open=1)
    br.record_failure()
    time.sleep(0.06)
    assert br.state == "half_open"
    assert br.allow() is True
    assert br.half_open_inflight == 1
    assert br.allow() is False
    br.record_success()
    assert br.state == "closed"
    assert br.half_open_inflight == 0


def test_max_half_open_allows_bounded_probes():
    br = CircuitBreaker(
        failure_threshold=1,
        recovery_timeout=0.05,
        success_threshold=2,
        max_half_open=2,
    )
    br.record_failure()
    time.sleep(0.06)
    assert br.allow() is True
    assert br.allow() is True
    assert br.allow() is False
    br.record_success()
    assert br.state == "half_open"
    br.record_success()
    assert br.state == "closed"


def test_invalid_max_half_open():
    with pytest.raises(ValueError):
        CircuitBreaker(max_half_open=0)


def test_reset_forces_closed():
    br = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
    br.record_failure()
    assert br.state == "open"
    br.reset()
    assert br.state == "closed"
    assert br.allow() is True


def test_invalid_params():
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker(recovery_timeout=-1)
    with pytest.raises(ValueError):
        CircuitBreaker(success_threshold=0)


def test_attempt_trips_and_short_circuits():
    br = CircuitBreaker(failure_threshold=2, recovery_timeout=30.0, name="payments")
    fn = Mock(side_effect=RuntimeError("down"))

    with pytest.raises(RuntimeError):
        attempt(fn, max_attempts=2, base_delay=0.0, jitter=False, circuit=br)
    assert fn.call_count == 2
    assert br.state == "open"

    with pytest.raises(CircuitOpenError) as ei:
        attempt(fn, max_attempts=5, base_delay=0.0, jitter=False, circuit=br)
    assert ei.value.breaker is br
    assert ei.value.retry_after is not None
    # Açıkken fonksiyon tekrar çağrılmamalı
    assert fn.call_count == 2


def test_attempt_records_success_and_keeps_closed():
    br = CircuitBreaker(failure_threshold=2, recovery_timeout=30.0)
    fn = Mock(side_effect=[RuntimeError("x"), "ok"])
    result = attempt(fn, max_attempts=3, base_delay=0.0, jitter=False, circuit=br)
    assert result == "ok"
    assert br.state == "closed"
    assert br.failure_count == 0


def test_attempt_circuit_open_as_retry_error():
    br = CircuitBreaker(failure_threshold=1, recovery_timeout=30.0, name="db")
    br.record_failure()
    with pytest.raises(RetryError) as ei:
        attempt(
            Mock(return_value=1),
            max_attempts=3,
            circuit=br,
            reraise_as_retry_error=True,
        )
    assert isinstance(ei.value.last_exception, CircuitOpenError)


def test_rejected_result_counts_as_failure():
    br = CircuitBreaker(failure_threshold=2, recovery_timeout=30.0)
    fn = Mock(return_value=[])
    with pytest.raises(RuntimeError):
        attempt(
            fn,
            max_attempts=2,
            base_delay=0.0,
            jitter=False,
            retry_if_result=lambda r: len(r) == 0,
            circuit=br,
        )
    assert br.state == "open"


def test_attempt_rejects_surplus_half_open_probes():
    br = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05, max_half_open=1, name="probe")
    br.record_failure()
    time.sleep(0.06)
    assert br.state == "half_open"
    assert br.allow() is True

    with pytest.raises(CircuitOpenError) as ei:
        attempt(Mock(return_value="should-not-run"), max_attempts=2, circuit=br)
    assert "probe limiti" in str(ei.value)
    assert ei.value.breaker is br
    br.record_success()
    assert br.state == "closed"


def test_concurrent_half_open_only_one_probe():
    br = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05, max_half_open=1)
    br.record_failure()
    time.sleep(0.06)

    allowed = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        allowed.append(br.allow())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for ok in allowed if ok) == 1
    assert br.half_open_inflight == 1


@pytest.mark.asyncio
async def test_async_attempt_respects_open_circuit():
    br = CircuitBreaker(failure_threshold=1, recovery_timeout=30.0, name="async-api")
    fn = AsyncMock(side_effect=ConnectionError("nope"))
    with pytest.raises(ConnectionError):
        await async_attempt(fn, max_attempts=1, circuit=br)
    assert br.state == "open"

    with pytest.raises(CircuitOpenError):
        await async_attempt(fn, max_attempts=4, base_delay=0.0, jitter=False, circuit=br)
    assert fn.await_count == 1
