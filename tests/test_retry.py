"""attempt.retry birim testleri."""

import asyncio
import time
from unittest.mock import AsyncMock, Mock

import pytest

from attempt import RetryError, async_attempt, async_retry, attempt, retry
from attempt.retry import _compute_delay


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
        events.append((attempt_num, type(exc).__name__ if exc else None, delay))

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


def test_retry_if_result_retries_until_acceptable():
    """retry_if_result True dönerse sonucu reddedip yeniden denemeli."""
    fn = Mock(side_effect=[[], [], ["item"]])
    result = attempt(
        fn,
        max_attempts=5,
        base_delay=0.01,
        jitter=False,
        retry_if_result=lambda r: r is None or len(r) == 0,
    )
    assert result == ["item"]
    assert fn.call_count == 3


def test_retry_if_result_exhausted_raises():
    """Sonuç hiç kabul edilmezse RuntimeError / RetryError yükseltilmeli."""
    fn = Mock(return_value=[])
    with pytest.raises(RuntimeError, match="kabul edilmedi"):
        attempt(
            fn,
            max_attempts=3,
            base_delay=0.01,
            jitter=False,
            retry_if_result=lambda r: len(r) == 0,
        )
    assert fn.call_count == 3

    fn2 = Mock(return_value={"error": "busy"})
    with pytest.raises(RetryError) as ei:
        attempt(
            fn2,
            max_attempts=2,
            base_delay=0.01,
            jitter=False,
            retry_if_result=lambda r: r.get("error") is not None,
            reraise_as_retry_error=True,
        )
    assert ei.value.attempts == 2
    assert ei.value.last_exception is None
    assert ei.value.last_result == {"error": "busy"}


def test_retry_if_result_accepts_first_good():
    """İlk sonuç kabul edilirse hiç yeniden denememeli."""
    fn = Mock(return_value=[1, 2, 3])
    result = attempt(
        fn,
        max_attempts=5,
        retry_if_result=lambda r: len(r) == 0,
    )
    assert result == [1, 2, 3]
    assert fn.call_count == 1


def test_retry_if_result_with_decorator():
    """@retry üzerinde retry_if_result çalışmalı."""
    calls = []

    @retry(
        max_attempts=4,
        base_delay=0.01,
        jitter=False,
        retry_if_result=lambda r: r is None,
    )
    def flaky():
        calls.append(1)
        if len(calls) < 2:
            return None
        return "ok"

    assert flaky() == "ok"
    assert len(calls) == 2


def test_full_jitter_stays_within_bounds():
    """jitter='full' ile gecikme [0, computed_delay] aralığında olmalı."""
    # attempt 1 → base=1.0 → max possible = 1.0
    # attempt 2 → 2.0, attempt 3 → 4.0
    for attempt_num, expected_cap in [(1, 1.0), (2, 2.0), (3, 4.0)]:
        samples = [
            _compute_delay(attempt_num, 1.0, 60.0, 2.0, "full")
            for _ in range(50)
        ]
        assert all(0.0 <= d <= expected_cap + 1e-9 for d in samples)
        # En az bir örnek cap'e yakın olmalı (istatistiksel olarak çok olası)
        assert max(samples) > expected_cap * 0.1


def test_full_jitter_via_attempt_api():
    """attempt(..., jitter='full') on_retry üzerinden geçerli delay üretmeli."""
    delays = []

    def on_retry(attempt_num, exc, delay):
        delays.append(delay)

    fn = Mock(side_effect=[Exception()] * 3)
    with pytest.raises(Exception):
        attempt(
            fn,
            max_attempts=3,
            base_delay=0.2,
            max_delay=10.0,
            exponential_base=2.0,
            jitter="full",
            on_retry=on_retry,
        )

    assert len(delays) == 2
    # attempt 1 → [0, 0.2], attempt 2 → [0, 0.4]
    assert 0.0 <= delays[0] <= 0.2 + 1e-9
    assert 0.0 <= delays[1] <= 0.4 + 1e-9


def test_equal_jitter_alias():
    """jitter='equal' True ile aynı davranışı göstermeli (aralık kontrolü)."""
    samples_true = [
        _compute_delay(1, 1.0, 60.0, 2.0, True) for _ in range(30)
    ]
    samples_equal = [
        _compute_delay(1, 1.0, 60.0, 2.0, "equal") for _ in range(30)
    ]
    # Equal jitter: delay * [0.75, 1.25] → [0.75, 1.25]
    assert all(0.75 <= d <= 1.25 + 1e-9 for d in samples_true)
    assert all(0.75 <= d <= 1.25 + 1e-9 for d in samples_equal)


def test_no_jitter():
    """jitter=False iken delay tam olarak exponential olmalı."""
    assert _compute_delay(1, 1.0, 60.0, 2.0, False) == 1.0
    assert _compute_delay(2, 1.0, 60.0, 2.0, False) == 2.0
    assert _compute_delay(3, 1.0, 60.0, 2.0, False) == 4.0


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


@pytest.mark.asyncio
async def test_async_retry_if_result():
    """Async: retry_if_result ile sonuç reddedilip yeniden denenmeli."""
    fn = AsyncMock(side_effect=[{"ok": False}, {"ok": False}, {"ok": True, "data": 1}])
    result = await async_attempt(
        fn,
        max_attempts=5,
        base_delay=0.01,
        jitter=False,
        retry_if_result=lambda r: not r.get("ok"),
    )
    assert result == {"ok": True, "data": 1}
    assert fn.await_count == 3


@pytest.mark.asyncio
async def test_async_full_jitter():
    """Async path'te de jitter='full' çalışmalı."""
    delays = []

    def on_retry(attempt_num, exc, delay):
        delays.append(delay)

    fn = AsyncMock(side_effect=[RuntimeError("x"), RuntimeError("y"), "ok"])
    result = await async_attempt(
        fn,
        max_attempts=5,
        base_delay=0.05,
        jitter="full",
        on_retry=on_retry,
    )
    assert result == "ok"
    assert len(delays) == 2
    assert all(0.0 <= d for d in delays)
