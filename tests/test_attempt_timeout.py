import asyncio
import time

import pytest

from attempt import CircuitBreaker, GiveUpContext, async_attempt, async_retry, attempt, retry


def test_attempt_timeout_retries_hung_call():
    calls = {"n": 0}

    def hang():
        calls["n"] += 1
        if calls["n"] < 3:
            time.sleep(0.4)
        return "ok"

    started = time.monotonic()
    result = attempt(
        hang,
        max_attempts=3,
        base_delay=0,
        jitter=False,
        attempt_timeout=0.08,
    )
    elapsed = time.monotonic() - started
    assert result == "ok"
    assert calls["n"] == 3
    assert elapsed < 0.6


def test_attempt_timeout_gives_up_as_timeout_error():
    def hang():
        time.sleep(0.3)
        return "late"

    with pytest.raises(TimeoutError, match="Deneme"):
        attempt(hang, max_attempts=2, base_delay=0, jitter=False, attempt_timeout=0.05)


def test_attempt_timeout_uses_fallback():
    def hang():
        time.sleep(0.3)
        return "late"

    result = attempt(
        hang,
        max_attempts=2,
        base_delay=0,
        jitter=False,
        attempt_timeout=0.05,
        fallback=lambda ctx: f"stale:{type(ctx.exception).__name__}:{ctx.attempts}",
    )
    assert result == "stale:TimeoutError:2"


def test_attempt_timeout_records_circuit_failure():
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=30.0, name="hung")

    def hang():
        time.sleep(0.25)
        return "late"

    with pytest.raises(TimeoutError):
        attempt(
            hang,
            max_attempts=2,
            base_delay=0,
            jitter=False,
            attempt_timeout=0.04,
            circuit=breaker,
        )
    assert breaker.state == "open"


def test_fast_call_unaffected_by_attempt_timeout():
    assert attempt(lambda: 7, attempt_timeout=1.0) == 7


def test_invalid_attempt_timeout():
    with pytest.raises(ValueError, match="attempt_timeout"):
        attempt(lambda: 1, attempt_timeout=0)
    with pytest.raises(ValueError, match="attempt_timeout"):
        attempt(lambda: 1, attempt_timeout=-1)


def test_decorator_attempt_timeout():
    @retry(max_attempts=2, base_delay=0, jitter=False, attempt_timeout=0.05, fallback=lambda: "d")
    def hang():
        time.sleep(0.3)
        return "late"

    assert hang() == "d"


def test_overall_timeout_uses_fallback():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ConnectionError("x")

    result = attempt(
        boom,
        max_attempts=8,
        base_delay=0.2,
        jitter=False,
        timeout=0.05,
        fallback=lambda ctx: ("deadline", ctx.attempts, type(ctx.exception).__name__),
    )
    assert result[0] == "deadline"
    assert result[2] == "ConnectionError"
    assert calls["n"] >= 1


@pytest.mark.asyncio
async def test_async_attempt_timeout_cancels_and_retries():
    calls = {"n": 0}
    cancelled = {"n": 0}

    async def hang():
        calls["n"] += 1
        if calls["n"] < 3:
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                cancelled["n"] += 1
                raise
        return "ok"

    result = await async_attempt(
        hang,
        max_attempts=3,
        base_delay=0,
        jitter=False,
        attempt_timeout=0.05,
    )
    assert result == "ok"
    assert calls["n"] == 3
    assert cancelled["n"] == 2


@pytest.mark.asyncio
async def test_async_attempt_timeout_fallback():
    async def hang():
        await asyncio.sleep(1.0)
        return "late"

    result = await async_attempt(
        hang,
        max_attempts=2,
        base_delay=0,
        jitter=False,
        attempt_timeout=0.04,
        fallback=lambda ctx: ctx.attempts,
    )
    assert result == 2
    assert isinstance  # keep import used via GiveUpContext in other tests
    assert GiveUpContext  # noqa: B018 — imported for type presence


@pytest.mark.asyncio
async def test_async_retry_decorator_attempt_timeout():
    @async_retry(max_attempts=2, base_delay=0, jitter=False, attempt_timeout=0.04, fallback=lambda: "d")
    async def hang():
        await asyncio.sleep(1.0)
        return "late"

    assert await hang() == "d"
