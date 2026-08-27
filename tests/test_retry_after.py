"""Retry-After, deneme geçmişi, parametre doğrulama ve hazır predicate testleri."""

from unittest.mock import AsyncMock, Mock

import pytest

from attempt import (
    RetryAttempt,
    RetryError,
    async_attempt,
    attempt,
    extract_retry_after,
    retry_if_empty,
    retry_if_message,
    retry_if_status,
)


def test_param_validation():
    with pytest.raises(ValueError, match="base_delay"):
        attempt(lambda: None, base_delay=-1)
    with pytest.raises(ValueError, match="exponential_base"):
        attempt(lambda: None, exponential_base=0.5)
    with pytest.raises(ValueError, match="timeout"):
        attempt(lambda: None, timeout=-0.1)


def test_extract_retry_after_attr_and_headers():
    class RateLimit(Exception):
        def __init__(self, retry_after=None, headers=None):
            super().__init__("rate limited")
            self.retry_after = retry_after
            self.headers = headers

    assert extract_retry_after(RateLimit(retry_after=2.5)) == 2.5
    assert extract_retry_after(RateLimit(headers={"Retry-After": "3"})) == 3.0
    assert extract_retry_after(RateLimit(headers={"retry-after": "1.5"})) == 1.5
    assert extract_retry_after(RateLimit(retry_after="not-a-number")) is None
    assert extract_retry_after(ValueError("x")) is None


def test_retry_after_overrides_backoff():
    delays = []

    class RateLimit(Exception):
        def __init__(self):
            super().__init__("429")
            self.retry_after = 0.05

    def on_retry(attempt_num, exc, delay):
        delays.append(delay)

    fn = Mock(side_effect=[RateLimit(), "ok"])
    result = attempt(
        fn,
        max_attempts=3,
        base_delay=10.0,
        max_delay=60.0,
        jitter=False,
        retry_after=extract_retry_after,
        on_retry=on_retry,
    )
    assert result == "ok"
    assert len(delays) == 1
    assert delays[0] == pytest.approx(0.05)


def test_retry_after_capped_by_max_delay():
    delays = []

    class RateLimit(Exception):
        retry_after = 100.0

    def on_retry(attempt_num, exc, delay):
        delays.append(delay)

    fn = Mock(side_effect=[RateLimit(), "ok"])
    result = attempt(
        fn,
        max_attempts=3,
        base_delay=0.01,
        max_delay=0.02,
        jitter=False,
        retry_after=extract_retry_after,
        on_retry=on_retry,
    )
    assert result == "ok"
    assert delays[0] == pytest.approx(0.02)


def test_retry_error_includes_history():
    fn = Mock(side_effect=[ValueError("a"), ValueError("b"), ValueError("c")])
    with pytest.raises(RetryError) as ei:
        attempt(
            fn,
            max_attempts=3,
            base_delay=0.01,
            jitter=False,
            reraise_as_retry_error=True,
        )
    err = ei.value
    assert err.attempts == 3
    assert len(err.history) == 3
    assert all(isinstance(h, RetryAttempt) for h in err.history)
    assert [h.number for h in err.history] == [1, 2, 3]
    assert all(isinstance(h.exception, ValueError) for h in err.history)
    assert err.history[0].delay == pytest.approx(0.01)
    assert err.history[2].delay is None


def test_retry_if_status_helper():
    class HttpError(Exception):
        def __init__(self, status_code: int):
            self.status_code = status_code
            super().__init__(f"HTTP {status_code}")

    fn = Mock(side_effect=[HttpError(503), HttpError(429), "ok"])
    result = attempt(
        fn,
        max_attempts=5,
        base_delay=0.01,
        jitter=False,
        retry_if=retry_if_status(429, 503),
    )
    assert result == "ok"
    assert fn.call_count == 3

    fn2 = Mock(side_effect=HttpError(400))
    with pytest.raises(HttpError):
        attempt(
            fn2,
            max_attempts=4,
            base_delay=0.01,
            jitter=False,
            retry_if=retry_if_status(429, 503),
        )
    assert fn2.call_count == 1


def test_retry_if_empty_helper():
    fn = Mock(side_effect=[None, [], [1]])
    result = attempt(
        fn,
        max_attempts=4,
        base_delay=0.01,
        jitter=False,
        retry_if_result=retry_if_empty,
    )
    assert result == [1]
    assert fn.call_count == 3


def test_retry_if_message_helper():
    fn = Mock(side_effect=[RuntimeError("temporary glitch"), "ok"])
    result = attempt(
        fn,
        max_attempts=3,
        base_delay=0.01,
        jitter=False,
        retry_if=retry_if_message("temporary", "timeout"),
    )
    assert result == "ok"


@pytest.mark.asyncio
async def test_async_retry_after_and_history():
    class RateLimit(Exception):
        retry_after = 0.01

    fn = AsyncMock(side_effect=[RateLimit(), RateLimit()])
    with pytest.raises(RetryError) as ei:
        await async_attempt(
            fn,
            max_attempts=2,
            base_delay=5.0,
            max_delay=5.0,
            jitter=False,
            retry_after=extract_retry_after,
            reraise_as_retry_error=True,
        )
    err = ei.value
    assert err.attempts == 2
    assert len(err.history) == 2
    assert err.history[0].delay == pytest.approx(0.01)
