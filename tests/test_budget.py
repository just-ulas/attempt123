"""Kayar pencereli RetryBudget birim testleri."""

from unittest.mock import AsyncMock, Mock

import pytest

from attempt import RetryBudget, RetryError, async_attempt, attempt


def test_invalid_params():
    with pytest.raises(ValueError):
        RetryBudget(window=0)
    with pytest.raises(ValueError):
        RetryBudget(retry_ratio=-0.1)
    with pytest.raises(ValueError):
        RetryBudget(min_retries=-1)


def test_min_retries_allows_early_retries():
    budget = RetryBudget(window=10.0, retry_ratio=0.0, min_retries=2, name="early")
    budget.record_request()
    assert budget.try_retry() is True
    assert budget.try_retry() is True
    assert budget.try_retry() is False
    assert budget.retry_count == 2


def test_ratio_caps_retries_after_min():
    budget = RetryBudget(window=30.0, retry_ratio=0.2, min_retries=0, name="ratio")
    for _ in range(10):
        budget.record_request()
    granted = sum(1 for _ in range(10) if budget.try_retry())
    assert granted == 2
    assert budget.try_retry() is False


def test_reset_clears_window():
    budget = RetryBudget(min_retries=1, retry_ratio=0.0)
    budget.record_request()
    assert budget.try_retry() is True
    assert budget.try_retry() is False
    budget.reset()
    budget.record_request()
    assert budget.try_retry() is True


def test_attempt_stops_when_budget_exhausted():
    budget = RetryBudget(window=30.0, retry_ratio=0.0, min_retries=0, name="api")
    fn = Mock(side_effect=RuntimeError("down"))
    with pytest.raises(RuntimeError, match="down"):
        attempt(
            fn,
            max_attempts=8,
            base_delay=0.0,
            jitter=False,
            budget=budget,
        )
    assert fn.call_count == 1
    assert budget.retry_count == 0


def test_attempt_uses_min_retries_then_gives_up():
    budget = RetryBudget(window=30.0, retry_ratio=0.0, min_retries=2, name="svc")
    fn = Mock(side_effect=ValueError("x"))
    with pytest.raises(ValueError):
        attempt(
            fn,
            max_attempts=10,
            base_delay=0.0,
            jitter=False,
            budget=budget,
        )
    # 1 ilk deneme + 2 bütçe retry
    assert fn.call_count == 3
    assert budget.retry_count == 2


def test_budget_exhaustion_as_retry_error():
    budget = RetryBudget(retry_ratio=0.0, min_retries=0)
    fn = Mock(side_effect=OSError("nope"))
    with pytest.raises(RetryError) as ei:
        attempt(
            fn,
            max_attempts=5,
            base_delay=0.0,
            jitter=False,
            budget=budget,
            reraise_as_retry_error=True,
        )
    assert ei.value.attempts == 1
    assert isinstance(ei.value.last_exception, OSError)


def test_budget_with_retry_if_result():
    budget = RetryBudget(retry_ratio=0.0, min_retries=1)
    fn = Mock(return_value=[])
    with pytest.raises(RuntimeError, match="kabul edilmedi"):
        attempt(
            fn,
            max_attempts=6,
            base_delay=0.0,
            jitter=False,
            retry_if_result=lambda r: r == [],
            budget=budget,
        )
    assert fn.call_count == 2


def test_shared_budget_across_calls():
    budget = RetryBudget(retry_ratio=0.0, min_retries=1, name="shared")

    def boom():
        raise ConnectionError("x")

    with pytest.raises(ConnectionError):
        attempt(boom, max_attempts=5, base_delay=0.0, jitter=False, budget=budget)
    with pytest.raises(ConnectionError):
        attempt(boom, max_attempts=5, base_delay=0.0, jitter=False, budget=budget)
    # İlk çağrı 1 request + 1 retry; ikinci çağrı 1 request, retry hakkı kalmadı
    assert budget.request_count == 2
    assert budget.retry_count == 1


@pytest.mark.asyncio
async def test_async_attempt_respects_budget():
    budget = RetryBudget(retry_ratio=0.0, min_retries=0, name="async")
    fn = AsyncMock(side_effect=TimeoutError("slow"))
    with pytest.raises(TimeoutError):
        await async_attempt(
            fn,
            max_attempts=4,
            base_delay=0.0,
            jitter=False,
            budget=budget,
        )
    assert fn.await_count == 1
