"""Bulkhead birim testleri."""

import threading
import time
from unittest.mock import AsyncMock, Mock

import pytest

from attempt import Bulkhead, BulkheadFullError, RetryError, async_attempt, attempt


def test_try_acquire_and_release():
    bh = Bulkhead(max_concurrent=2, name="db")
    assert bh.available == 2
    assert bh.try_acquire() is True
    assert bh.try_acquire() is True
    assert bh.try_acquire() is False
    assert bh.inflight == 2
    bh.release()
    assert bh.try_acquire() is True
    assert bh.available == 0


def test_invalid_params():
    with pytest.raises(ValueError):
        Bulkhead(max_concurrent=0)


def test_acquire_timeout_false_when_full():
    bh = Bulkhead(max_concurrent=1)
    assert bh.try_acquire() is True
    assert bh.acquire(timeout=0.0) is False
    assert bh.acquire(timeout=0.02) is False


def test_acquire_waits_for_release():
    bh = Bulkhead(max_concurrent=1)
    assert bh.try_acquire() is True
    released = []

    def _free():
        time.sleep(0.04)
        released.append(True)
        bh.release()

    t = threading.Thread(target=_free)
    t.start()
    start = time.monotonic()
    assert bh.acquire(timeout=0.5) is True
    assert time.monotonic() - start >= 0.03
    t.join()
    assert released


def test_context_manager():
    bh = Bulkhead(max_concurrent=1)
    with bh:
        assert bh.inflight == 1
        assert bh.try_acquire() is False
    assert bh.inflight == 0


def test_attempt_holds_slot_only_during_call():
    bh = Bulkhead(max_concurrent=1, name="http")
    seen = []

    def _fn():
        seen.append(bh.inflight)
        return "ok"

    assert attempt(_fn, max_attempts=1, bulkhead=bh) == "ok"
    assert seen == [1]
    assert bh.inflight == 0


def test_attempt_raises_when_full_and_no_wait():
    bh = Bulkhead(max_concurrent=1, name="api")
    assert bh.try_acquire() is True
    fn = Mock(return_value="never")
    with pytest.raises(BulkheadFullError) as ei:
        attempt(fn, max_attempts=3, base_delay=0.0, jitter=False, bulkhead=bh, timeout=0.0)
    assert ei.value.bulkhead is bh
    fn.assert_not_called()
    bh.release()


def test_attempt_bulkhead_as_retry_error():
    bh = Bulkhead(max_concurrent=1, name="q")
    bh.try_acquire()
    with pytest.raises(RetryError) as ei:
        attempt(
            Mock(return_value=1),
            max_attempts=2,
            bulkhead=bh,
            timeout=0.0,
            reraise_as_retry_error=True,
        )
    assert isinstance(ei.value.last_exception, BulkheadFullError)


def test_slot_released_on_exception():
    bh = Bulkhead(max_concurrent=1)

    def _boom():
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        attempt(_boom, max_attempts=1, bulkhead=bh)
    assert bh.inflight == 0


def test_retry_does_not_hold_slot_during_sleep():
    bh = Bulkhead(max_concurrent=1)
    calls = []

    def _fn():
        calls.append(bh.inflight)
        if len(calls) == 1:
            raise RuntimeError("again")
        return "ok"

    result = attempt(_fn, max_attempts=3, base_delay=0.05, jitter=False, bulkhead=bh)
    assert result == "ok"
    assert calls == [1, 1]
    assert bh.inflight == 0


@pytest.mark.asyncio
async def test_async_attempt_respects_bulkhead():
    bh = Bulkhead(max_concurrent=1, name="async-api")
    assert bh.try_acquire() is True
    fn = AsyncMock(return_value="x")
    with pytest.raises(BulkheadFullError):
        await async_attempt(
            fn, max_attempts=2, bulkhead=bh, timeout=0.0, base_delay=0.0, jitter=False
        )
    fn.assert_not_awaited()
    bh.release()


@pytest.mark.asyncio
async def test_async_attempt_releases_slot():
    bh = Bulkhead(max_concurrent=1)

    async def _fn():
        assert bh.inflight == 1
        return 7

    assert await async_attempt(_fn, max_attempts=1, bulkhead=bh) == 7
    assert bh.inflight == 0
