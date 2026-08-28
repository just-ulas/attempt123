import asyncio

import pytest

from attempt import GiveUpContext, RetryBudget, async_attempt, async_retry, attempt, retry


def test_fallback_used_after_exhausted_exceptions():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ConnectionError("down")

    result = attempt(
        boom,
        max_attempts=3,
        base_delay=0,
        jitter=False,
        fallback=lambda: "cached",
    )
    assert result == "cached"
    assert calls["n"] == 3


def test_fallback_receives_give_up_context():
    seen = {}

    def boom():
        raise TimeoutError("slow")

    def fb(ctx):
        seen["type"] = type(ctx)
        seen["exc"] = ctx.exception
        seen["attempts"] = ctx.attempts
        seen["history_len"] = len(ctx.history)
        return 42

    result = attempt(boom, max_attempts=2, base_delay=0, jitter=False, fallback=fb)
    assert result == 42
    assert seen["type"] is GiveUpContext
    assert isinstance(seen["exc"], TimeoutError)
    assert seen["attempts"] == 2
    assert seen["history_len"] == 2


def test_fallback_zero_arg_callable():
    def boom():
        raise ValueError("nope")

    assert attempt(boom, max_attempts=1, base_delay=0, fallback=lambda: "ok") == "ok"


def test_no_fallback_still_raises():
    def boom():
        raise RuntimeError("x")

    with pytest.raises(RuntimeError, match="x"):
        attempt(boom, max_attempts=2, base_delay=0, jitter=False)


def test_successful_call_skips_fallback():
    fallback_called = {"n": 0}

    def ok():
        return "live"

    def fb():
        fallback_called["n"] += 1
        return "stale"

    assert attempt(ok, fallback=fb) == "live"
    assert fallback_called["n"] == 0


def test_fallback_on_rejected_result():
    def empty():
        return []

    result = attempt(
        empty,
        max_attempts=2,
        base_delay=0,
        jitter=False,
        retry_if_result=lambda v: v == [],
        fallback=lambda ctx: {"from": ctx.result},
    )
    assert result == {"from": []}


def test_fallback_when_retry_if_says_no():
    def boom():
        raise PermissionError("denied")

    result = attempt(
        boom,
        max_attempts=5,
        base_delay=0,
        retry_if=lambda exc: False,
        fallback=lambda ctx: f"gave-up:{type(ctx.exception).__name__}",
    )
    assert result == "gave-up:PermissionError"


def test_fallback_when_budget_exhausted():
    budget = RetryBudget(window=10.0, retry_ratio=0.0, min_retries=0, name="tight")

    def boom():
        raise ConnectionError("x")

    result = attempt(
        boom,
        max_attempts=5,
        base_delay=0,
        jitter=False,
        budget=budget,
        fallback=lambda: "budget-stale",
    )
    assert result == "budget-stale"


def test_decorator_fallback():
    @retry(max_attempts=2, base_delay=0, jitter=False, fallback=lambda: 7)
    def boom():
        raise OSError("nope")

    assert boom() == 7


@pytest.mark.asyncio
async def test_async_fallback_sync_callable():
    async def boom():
        raise ConnectionError("down")

    result = await async_attempt(
        boom,
        max_attempts=2,
        base_delay=0,
        jitter=False,
        fallback=lambda: "async-cached",
    )
    assert result == "async-cached"


@pytest.mark.asyncio
async def test_async_fallback_awaitable():
    async def boom():
        raise ConnectionError("down")

    async def fb(ctx):
        await asyncio.sleep(0)
        return f"awaited:{ctx.attempts}"

    result = await async_attempt(boom, max_attempts=3, base_delay=0, jitter=False, fallback=fb)
    assert result == "awaited:3"


@pytest.mark.asyncio
async def test_async_retry_decorator_fallback():
    @async_retry(max_attempts=2, base_delay=0, jitter=False, fallback=lambda: "d")
    async def boom():
        raise RuntimeError("x")

    assert await boom() == "d"
