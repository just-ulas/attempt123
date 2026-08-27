"""Token-bucket RateLimiter birim testleri."""

import time
from unittest.mock import AsyncMock, Mock

import pytest

from attempt import RateLimitError, RateLimiter, RetryError, async_attempt, attempt


def test_starts_full():
    lim = RateLimiter(rate=10, burst=5, name="api")
    assert lim.tokens == pytest.approx(5.0)
    assert lim.try_acquire(5) is True
    assert lim.tokens == pytest.approx(0.0)
    assert lim.try_acquire() is False


def test_refills_over_time():
    lim = RateLimiter(rate=20, burst=2)
    assert lim.try_acquire(2) is True
    time.sleep(0.06)
    assert lim.tokens == pytest.approx(1.2, abs=0.4)
    assert lim.try_acquire(1) is True


def test_acquire_waits_then_succeeds():
    lim = RateLimiter(rate=50, burst=1)
    assert lim.try_acquire(1) is True
    start = time.monotonic()
    assert lim.acquire(1, timeout=0.2) is True
    assert time.monotonic() - start >= 0.015


def test_acquire_timeout_returns_false():
    lim = RateLimiter(rate=1, burst=1)
    lim.try_acquire(1)
    assert lim.acquire(1, timeout=0.0) is False
    assert lim.acquire(1, timeout=0.02) is False


def test_zero_rate_never_refills():
    lim = RateLimiter(rate=0, burst=1)
    lim.try_acquire(1)
    assert lim.seconds_until_available(1) == float("inf")
    assert lim.acquire(1, timeout=None) is False


def test_invalid_params():
    with pytest.raises(ValueError):
        RateLimiter(rate=-1)
    with pytest.raises(ValueError):
        RateLimiter(rate=1, burst=0)


def test_reset_refills_bucket():
    lim = RateLimiter(rate=1, burst=3)
    lim.try_acquire(3)
    lim.reset()
    assert lim.tokens == pytest.approx(3.0)


def test_attempt_consumes_token_per_call():
    lim = RateLimiter(rate=1000, burst=2, name="http")
    fn = Mock(side_effect=[RuntimeError("x"), "ok"])
    result = attempt(fn, max_attempts=3, base_delay=0.0, jitter=False, limiter=lim)
    assert result == "ok"
    assert fn.call_count == 2
    assert lim.tokens == pytest.approx(0.0, abs=0.2)


def test_attempt_raises_when_bucket_empty_and_no_wait():
    lim = RateLimiter(rate=0, burst=1, name="db")
    lim.try_acquire(1)
    fn = Mock(return_value="never")
    with pytest.raises(RateLimitError) as ei:
        attempt(fn, max_attempts=3, base_delay=0.0, jitter=False, limiter=lim, timeout=0.0)
    assert ei.value.limiter is lim
    fn.assert_not_called()


def test_attempt_rate_limit_as_retry_error():
    lim = RateLimiter(rate=0, burst=1, name="q")
    lim.try_acquire(1)
    with pytest.raises(RetryError) as ei:
        attempt(
            Mock(return_value=1),
            max_attempts=2,
            limiter=lim,
            timeout=0.0,
            reraise_as_retry_error=True,
        )
    assert isinstance(ei.value.last_exception, RateLimitError)


@pytest.mark.asyncio
async def test_async_attempt_respects_limiter():
    lim = RateLimiter(rate=0, burst=1, name="async-api")
    lim.try_acquire(1)
    fn = AsyncMock(return_value="x")
    with pytest.raises(RateLimitError):
        await async_attempt(fn, max_attempts=2, limiter=lim, timeout=0.0, base_delay=0.0, jitter=False)
    fn.assert_not_awaited()
