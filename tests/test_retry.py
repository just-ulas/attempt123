"""attempt.retry birim testleri."""

import asyncio
import time
from unittest.mock import AsyncMock, Mock

import pytest

from attempt import RetryError, async_attempt, async_retry, attempt, retry


def test_successful_first_try():
    """İlk denemede başarılı olursa hemen dönmeli."""
    fn = Mock(return_value=42)
    result = attempt(fn, max_attempts=3)
    assert result == 42
    assert fn.call_count == 1


def test_retry_until_success():
    """İlk iki deneme başarısız, üçüncüde başarılı."""
    fn = Mock(side_effect=[ValueError("fail"), ValueError("fail"), "ok"])
    result = attempt(fn, max_attempts=5, base_delay=0.01, jitter=False)
    assert result == "ok"
    assert fn.call_count == 3


def test_exhausted_attempts_raises():
    """Tüm denemeler tükenince son exception yükseltilmeli."""
    fn = Mock(side_effect=RuntimeError("always fail"))
    with pytest.raises(RuntimeError, match="always fail"):
        attempt(fn, max_attempts=3, base_delay=0.01, jitter=False)
    assert fn.call_count == 3


def test_only_specified_exceptions_are_retried():
    """Belirtilmeyen exception anında yükseltilmeli."""
    fn = Mock(side_effect=TypeError("wrong type"))
    with pytest.raises(TypeError):
        attempt(fn, max_attempts=5, exceptions=(ValueError,), base_delay=0.01)
    assert fn.call_count == 1


def test_decorator_works():
    """@retry decorator doğru çalışmalı."""
    calls = []

    @retry(max_attempts=3, base_delay=0.01, jitter=False)
    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise ConnectionError("geçici")
        return "success"

    assert flaky() == "success"
    assert len(calls) == 2


def test_on_retry_callback():
    """on_retry callback her yeniden denemede çağrılmalı."""
    events = []

    def on_retry(attempt_num, exc, delay):
        events.append((attempt_num, type(exc).__name__, delay))

    fn = Mock(side_effect=[ValueError("a"), ValueError("b"), "done"])
    result = attempt(
        fn,
        max_attempts=4,
        base_delay=0.05,
        jitter=False,
        on_retry=on_retry,
    )
    assert result == "done"
    assert len(events) == 2
    assert events[0][0] == 1
    assert events[1][0] == 2


def test_max_attempts_validation():
    """max_attempts < 1 geçersiz olmalı."""
    with pytest.raises(ValueError):
        attempt(lambda: None, max_attempts=0)


def test_exponential_backoff_increases():
    """Bekleme süreleri üstel olarak artmalı (jitter kapalı)."""
    delays = []

    def on_retry(attempt_num, exc, delay):
        delays.append(delay)

    fn = Mock(side_effect=[Exception()] * 4)
    with pytest.raises(Exception):
        attempt(
            fn,
            max_attempts=4,
            base_delay=0.1,
            max_delay=10.0,
            exponential_base=2.0,
            jitter=False,
            on_retry=on_retry,
        )

    # 0.1, 0.2, 0.4
    assert len(delays) == 3
    assert delays[0] == pytest.approx(0.1)
    assert delays[1] == pytest.approx(0.2)
    assert delays[2] == pytest.approx(0.4)


def test_timeout_stops_early():
    """Toplam timeout aşıldığında erken çıkmalı."""
    fn = Mock(side_effect=Exception("fail"))
    start = time.monotonic()
    with pytest.raises(Exception):
        attempt(
            fn,
            max_attempts=10,
            base_delay=0.2,
            jitter=False,
            timeout=0.35,
        )
    elapsed = time.monotonic() - start
    # 10 deneme yapmaya çalışmaz; timeout nedeniyle erken kesilir
    assert elapsed < 1.0
    assert fn.call_count < 10


def test_reraise_as_retry_error():
    """reraise_as_retry_error=True iken RetryError yükseltilmeli."""
    fn = Mock(side_effect=ValueError("kalıcı hata"))
    with pytest.raises(RetryError) as exc_info:
        attempt(
            fn,
            max_attempts=3,
            base_delay=0.01,
            jitter=False,
            reraise_as_retry_error=True,
        )
    err = exc_info.value
    assert err.attempts == 3
    assert isinstance(err.last_exception, ValueError)
    assert "kalıcı hata" in str(err.last_exception)
    assert fn.call_count == 3


def test_retry_if_skips_non_matching():
    """retry_if False dönerse hemen yükseltmeli, yeniden denememeli."""

    class HttpError(Exception):
        def __init__(self, status_code: int):
            self.status_code = status_code
            super().__init__(f"HTTP {status_code}")

    fn = Mock(side_effect=HttpError(400))
    with pytest.raises(HttpError):
        attempt(
            fn,
            max_attempts=5,
            base_delay=0.01,
            jitter=False,
            retry_if=lambda e: getattr(e, "status_code", None) in (429, 503),
        )
    assert fn.call_count == 1


def test_retry_if_allows_matching():
    """retry_if True dönerse normal şekilde yeniden denemeli."""

    class HttpError(Exception):
        def __init__(self, status_code: int):
            self.status_code = status_code
            super().__init__(f"HTTP {status_code}")

    fn = Mock(side_effect=[HttpError(503), HttpError(503), "ok"])
    result = attempt(
        fn,
        max_attempts=5,
        base_delay=0.01,
        jitter=False,
        retry_if=lambda e: getattr(e, "status_code", None) in (429, 503),
    )
    assert result == "ok"
    assert fn.call_count == 3


def test_retry_if_with_decorator():
    """@retry decorator üzerinde retry_if çalışmalı."""
    calls = []

    @retry(
        max_attempts=4,
        base_delay=0.01,
        jitter=False,
        retry_if=lambda e: "geçici" in str(e),
    )
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("geçici hata")
        return "done"

    assert flaky() == "done"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_async_successful_first_try():
    """Async: ilk denemede başarılı."""
    fn = AsyncMock(return_value=99)
    result = await async_attempt(fn, max_attempts=3)
    assert result == 99
    assert fn.await_count == 1


@pytest.mark.asyncio
async def test_async_retry_until_success():
    """Async: birkaç başarısız denemeden sonra başarı."""
    fn = AsyncMock(side_effect=[ConnectionError("x"), ConnectionError("y"), "ok"])
    result = await async_attempt(fn, max_attempts=5, base_delay=0.01, jitter=False)
    assert result == "ok"
    assert fn.await_count == 3


@pytest.mark.asyncio
async def test_async_exhausted_raises():
    """Async: denemeler tükenince exception yükseltilmeli."""
    fn = AsyncMock(side_effect=RuntimeError("fail"))
    with pytest.raises(RuntimeError, match="fail"):
        await async_attempt(fn, max_attempts=2, base_delay=0.01, jitter=False)
    assert fn.await_count == 2


@pytest.mark.asyncio
async def test_async_decorator_works():
    """@async_retry decorator doğru çalışmalı."""
    calls = []

    @async_retry(max_attempts=3, base_delay=0.01, jitter=False)
    async def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise TimeoutError("geçici")
        return "async-success"

    assert await flaky() == "async-success"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_async_reraise_as_retry_error():
    """Async + RetryError sarmalama."""
    fn = AsyncMock(side_effect=OSError("disk"))
    with pytest.raises(RetryError) as exc_info:
        await async_attempt(
            fn,
            max_attempts=2,
            base_delay=0.01,
            jitter=False,
            reraise_as_retry_error=True,
        )
    assert exc_info.value.attempts == 2
    assert isinstance(exc_info.value.last_exception, OSError)


@pytest.mark.asyncio
async def test_async_retry_if():
    """Async: retry_if False dönerse hemen yükseltmeli."""
    fn = AsyncMock(side_effect=ValueError("kalıcı"))
    with pytest.raises(ValueError, match="kalıcı"):
        await async_attempt(
            fn,
            max_attempts=5,
            base_delay=0.01,
            jitter=False,
            retry_if=lambda e: "geçici" in str(e),
        )
    assert fn.await_count == 1
