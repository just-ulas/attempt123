from __future__ import annotations

import asyncio
import functools
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, List, Literal, Optional, Tuple, Type, TypeVar, Union

from .circuit import CircuitBreaker, CircuitOpenError
from .limiter import RateLimitError, RateLimiter

T = TypeVar("T")
ExceptionType = Union[Type[BaseException], Tuple[Type[BaseException], ...]]
RetryPredicate = Callable[[BaseException], bool]
ResultPredicate = Callable[[Any], bool]
RetryAfterExtractor = Callable[[Any], Optional[float]]
JitterMode = Union[bool, Literal["full", "equal"]]


class RetryAttempt:
    __slots__ = ("number", "exception", "result", "delay")

    def __init__(self, number, exception=None, result=None, delay=None):
        self.number = number
        self.exception = exception
        self.result = result
        self.delay = delay

    def __repr__(self):
        if self.exception is not None:
            detail = f"exc={type(self.exception).__name__}({self.exception!s})"
        else:
            detail = f"result={self.result!r}"
        delay_part = f", delay={self.delay:.4f}" if self.delay is not None else ""
        return f"RetryAttempt(number={self.number}, {detail}{delay_part})"


class RetryError(Exception):
    def __init__(self, last_exception=None, attempts=0, last_result=None, history=None):
        self.last_exception = last_exception
        self.attempts = attempts
        self.last_result = last_result
        self.history = list(history) if history else []
        if last_exception is not None:
            message = f"{attempts} deneme sonunda basarisiz: {type(last_exception).__name__}: {last_exception}"
        else:
            message = f"{attempts} deneme sonunda sonuc kabul edilmedi (last_result={last_result!r})"
        super().__init__(message)


def _parse_http_date_delay(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    if delta != delta:
        return None
    return max(0.0, delta)


def extract_retry_after(source):
    if source is None:
        return None
    value = getattr(source, "retry_after", None)
    if value is None:
        headers = getattr(source, "headers", None)
        if headers is not None:
            getter = getattr(headers, "get", None)
            if callable(getter):
                value = getter("Retry-After")
                if value is None:
                    value = getter("retry-after")
            elif isinstance(headers, dict):
                value = headers.get("Retry-After", headers.get("retry-after"))
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = _parse_http_date_delay(value)
        if seconds is None:
            return None
    if seconds != seconds:
        return None
    return max(0.0, seconds)


def _compute_delay(attempt_number, base_delay, max_delay, exponential_base, jitter):
    delay = min(base_delay * (exponential_base ** (attempt_number - 1)), max_delay)
    if delay <= 0:
        return 0.0
    if jitter is False:
        return delay
    if jitter == "full":
        return random.uniform(0.0, delay)
    return delay * (0.75 + random.random() * 0.5)


def _should_retry(exc, retry_if):
    if retry_if is None:
        return True
    return bool(retry_if(exc))


def _should_retry_result(result, retry_if_result):
    if retry_if_result is None:
        return False
    return bool(retry_if_result(result))


def _validate_params(max_attempts, base_delay, max_delay, exponential_base, timeout):
    if max_attempts < 1:
        raise ValueError("max_attempts en az 1 olmalidir")
    if base_delay < 0:
        raise ValueError("base_delay negatif olamaz")
    if max_delay < 0:
        raise ValueError("max_delay negatif olamaz")
    if exponential_base < 1:
        raise ValueError("exponential_base en az 1 olmalidir")
    if timeout is not None and timeout < 0:
        raise ValueError("timeout negatif olamaz")


def _resolve_delay(attempt_num, base_delay, max_delay, exponential_base, jitter, last_exc, last_result, retry_after):
    delay = _compute_delay(attempt_num, base_delay, max_delay, exponential_base, jitter)
    if retry_after is not None:
        source = last_exc if last_exc is not None else last_result
        if source is not None:
            hinted = retry_after(source)
            if hinted is not None:
                try:
                    delay = min(max(0.0, float(hinted)), max_delay)
                except (TypeError, ValueError):
                    pass
    return delay


def _raise_if_circuit_open(circuit, attempt_num, last_result, history, reraise_as_retry_error):
    if circuit is None or circuit.allow():
        return
    wait = circuit.seconds_until_retry()
    if circuit.state == "half_open":
        err = CircuitOpenError(
            f"CircuitBreaker {circuit.name!r} half-open probe limiti dolu "
            f"(max_half_open={circuit.max_half_open})",
            retry_after=wait if wait > 0 else None,
            breaker=circuit,
        )
    else:
        err = CircuitOpenError(
            f"CircuitBreaker {circuit.name!r} acik; {wait:.3f}s sonra denenebilir",
            retry_after=wait,
            breaker=circuit,
        )
    done = max(0, attempt_num - 1)
    if reraise_as_retry_error:
        raise RetryError(err, done, last_result, history=history) from err
    raise err


def _limiter_wait_budget(timeout, start_time):
    if timeout is None:
        return None
    return max(0.0, timeout - (time.monotonic() - start_time))


def _raise_rate_limited(limiter, attempt_num, last_result, history, reraise_as_retry_error):
    wait = limiter.seconds_until_available(1.0)
    wait_display = wait if wait != float("inf") else None
    err = RateLimitError(
        f"RateLimiter {limiter.name!r} token vermedi"
        + (f"; {wait:.3f}s sonra denenebilir" if wait_display is not None else ""),
        retry_after=wait_display,
        limiter=limiter,
    )
    done = max(0, attempt_num - 1)
    if reraise_as_retry_error:
        raise RetryError(err, done, last_result, history=history) from err
    raise err


def _give_up(attempt_num, last_exc, last_result, history, reraise_as_retry_error):
    if last_exc is not None:
        if reraise_as_retry_error:
            raise RetryError(last_exc, attempt_num, history=history) from last_exc
        raise last_exc
    if reraise_as_retry_error:
        raise RetryError(None, attempt_num, last_result, history=history)
    raise RuntimeError(f"{attempt_num} deneme sonunda sonuc kabul edilmedi: {last_result!r}")


def attempt(
    func,
    *,
    max_attempts=3,
    base_delay=1.0,
    max_delay=60.0,
    exponential_base=2.0,
    jitter=True,
    exceptions=(Exception,),
    retry_if=None,
    retry_if_result=None,
    retry_after=None,
    on_retry=None,
    timeout=None,
    reraise_as_retry_error=False,
    circuit=None,
    limiter=None,
    budget=None,
):
    _validate_params(max_attempts, base_delay, max_delay, exponential_base, timeout)
    start_time = time.monotonic()
    last_exc = None
    last_result = None
    history = []
    if budget is not None:
        budget.record_request()

    for attempt_num in range(1, max_attempts + 1):
        _raise_if_circuit_open(circuit, attempt_num, last_result, history, reraise_as_retry_error)
        if timeout is not None and (time.monotonic() - start_time) >= timeout:
            if last_exc is not None:
                if reraise_as_retry_error:
                    raise RetryError(last_exc, attempt_num - 1, history=history) from last_exc
                raise last_exc
            if last_result is not None or retry_if_result is not None:
                if reraise_as_retry_error:
                    raise RetryError(None, attempt_num - 1, last_result, history=history)
                raise RuntimeError(f"Toplam timeout ({timeout}s) asildi; sonuc kabul edilmedi")
            raise TimeoutError(f"Toplam timeout ({timeout}s) asildi")

        if limiter is not None:
            budget_wait = _limiter_wait_budget(timeout, start_time)
            if not limiter.acquire(1.0, timeout=budget_wait):
                _raise_rate_limited(limiter, attempt_num, last_result, history, reraise_as_retry_error)

        try:
            result = func()
            if not _should_retry_result(result, retry_if_result):
                if circuit is not None:
                    circuit.record_success()
                return result
            last_result = result
            last_exc = None
            if circuit is not None:
                circuit.record_failure()
            history.append(RetryAttempt(attempt_num, result=result))
            if attempt_num >= max_attempts:
                if reraise_as_retry_error:
                    raise RetryError(None, attempt_num, last_result, history=history)
                raise RuntimeError(f"{attempt_num} deneme sonunda sonuc kabul edilmedi: {last_result!r}")
        except CircuitOpenError:
            raise
        except RateLimitError:
            raise
        except exceptions as exc:
            last_exc = exc
            last_result = None
            if circuit is not None:
                circuit.record_failure()
            history.append(RetryAttempt(attempt_num, exception=exc))
            if attempt_num >= max_attempts or not _should_retry(exc, retry_if):
                if reraise_as_retry_error:
                    raise RetryError(exc, attempt_num, history=history) from exc
                raise

        if budget is not None and not budget.try_retry():
            _give_up(attempt_num, last_exc, last_result, history, reraise_as_retry_error)

        delay = _resolve_delay(attempt_num, base_delay, max_delay, exponential_base, jitter, last_exc, last_result, retry_after)
        history[-1].delay = delay
        if on_retry is not None:
            on_retry(attempt_num, last_exc, delay)
        if delay > 0:
            if timeout is not None:
                remaining = timeout - (time.monotonic() - start_time)
                if remaining <= 0:
                    if last_exc is not None:
                        if reraise_as_retry_error:
                            raise RetryError(last_exc, attempt_num, history=history) from last_exc
                        raise last_exc
                    if reraise_as_retry_error:
                        raise RetryError(None, attempt_num, last_result, history=history)
                    raise RuntimeError("Timeout asildi; sonuc kabul edilmedi")
                delay = min(delay, remaining)
            time.sleep(delay)

    if last_exc is not None:
        if reraise_as_retry_error:
            raise RetryError(last_exc, max_attempts, history=history) from last_exc
        raise last_exc
    if reraise_as_retry_error:
        raise RetryError(None, max_attempts, last_result, history=history)
    raise RuntimeError(f"{max_attempts} deneme sonunda sonuc kabul edilmedi")


def retry(max_attempts=3, base_delay=1.0, max_delay=60.0, exponential_base=2.0, jitter=True, exceptions=(Exception,), retry_if=None, retry_if_result=None, retry_after=None, on_retry=None, timeout=None, reraise_as_retry_error=False, circuit=None, limiter=None, budget=None):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return attempt(lambda: func(*args, **kwargs), max_attempts=max_attempts, base_delay=base_delay, max_delay=max_delay, exponential_base=exponential_base, jitter=jitter, exceptions=exceptions, retry_if=retry_if, retry_if_result=retry_if_result, retry_after=retry_after, on_retry=on_retry, timeout=timeout, reraise_as_retry_error=reraise_as_retry_error, circuit=circuit, limiter=limiter, budget=budget)
        return wrapper
    return decorator


async def async_attempt(func, *, max_attempts=3, base_delay=1.0, max_delay=60.0, exponential_base=2.0, jitter=True, exceptions=(Exception,), retry_if=None, retry_if_result=None, retry_after=None, on_retry=None, timeout=None, reraise_as_retry_error=False, circuit=None, limiter=None, budget=None):
    _validate_params(max_attempts, base_delay, max_delay, exponential_base, timeout)
    start_time = time.monotonic()
    last_exc = None
    last_result = None
    history = []
    if budget is not None:
        budget.record_request()

    for attempt_num in range(1, max_attempts + 1):
        _raise_if_circuit_open(circuit, attempt_num, last_result, history, reraise_as_retry_error)
        if timeout is not None and (time.monotonic() - start_time) >= timeout:
            if last_exc is not None:
                if reraise_as_retry_error:
                    raise RetryError(last_exc, attempt_num - 1, history=history) from last_exc
                raise last_exc
            if last_result is not None or retry_if_result is not None:
                if reraise_as_retry_error:
                    raise RetryError(None, attempt_num - 1, last_result, history=history)
                raise RuntimeError(f"Toplam timeout ({timeout}s) asildi; sonuc kabul edilmedi")
            raise TimeoutError(f"Toplam timeout ({timeout}s) asildi")

        if limiter is not None:
            budget_wait = _limiter_wait_budget(timeout, start_time)
            if not await limiter.acquire_async(1.0, timeout=budget_wait):
                _raise_rate_limited(limiter, attempt_num, last_result, history, reraise_as_retry_error)

        try:
            result = await func()
            if not _should_retry_result(result, retry_if_result):
                if circuit is not None:
                    circuit.record_success()
                return result
            last_result = result
            last_exc = None
            if circuit is not None:
                circuit.record_failure()
            history.append(RetryAttempt(attempt_num, result=result))
            if attempt_num >= max_attempts:
                if reraise_as_retry_error:
                    raise RetryError(None, attempt_num, last_result, history=history)
                raise RuntimeError(f"{attempt_num} deneme sonunda sonuc kabul edilmedi: {last_result!r}")
        except CircuitOpenError:
            raise
        except RateLimitError:
            raise
        except exceptions as exc:
            last_exc = exc
            last_result = None
            if circuit is not None:
                circuit.record_failure()
            history.append(RetryAttempt(attempt_num, exception=exc))
            if attempt_num >= max_attempts or not _should_retry(exc, retry_if):
                if reraise_as_retry_error:
                    raise RetryError(exc, attempt_num, history=history) from exc
                raise

        if budget is not None and not budget.try_retry():
            _give_up(attempt_num, last_exc, last_result, history, reraise_as_retry_error)

        delay = _resolve_delay(attempt_num, base_delay, max_delay, exponential_base, jitter, last_exc, last_result, retry_after)
        history[-1].delay = delay
        if on_retry is not None:
            on_retry(attempt_num, last_exc, delay)
        if delay > 0:
            if timeout is not None:
                remaining = timeout - (time.monotonic() - start_time)
                if remaining <= 0:
                    if last_exc is not None:
                        if reraise_as_retry_error:
                            raise RetryError(last_exc, attempt_num, history=history) from last_exc
                        raise last_exc
                    if reraise_as_retry_error:
                        raise RetryError(None, attempt_num, last_result, history=history)
                    raise RuntimeError("Timeout asildi; sonuc kabul edilmedi")
                delay = min(delay, remaining)
            await asyncio.sleep(delay)

    if last_exc is not None:
        if reraise_as_retry_error:
            raise RetryError(last_exc, max_attempts, history=history) from last_exc
        raise last_exc
    if reraise_as_retry_error:
        raise RetryError(None, max_attempts, last_result, history=history)
    raise RuntimeError(f"{max_attempts} deneme sonunda sonuc kabul edilmedi")


def async_retry(max_attempts=3, base_delay=1.0, max_delay=60.0, exponential_base=2.0, jitter=True, exceptions=(Exception,), retry_if=None, retry_if_result=None, retry_after=None, on_retry=None, timeout=None, reraise_as_retry_error=False, circuit=None, limiter=None, budget=None):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await async_attempt(lambda: func(*args, **kwargs), max_attempts=max_attempts, base_delay=base_delay, max_delay=max_delay, exponential_base=exponential_base, jitter=jitter, exceptions=exceptions, retry_if=retry_if, retry_if_result=retry_if_result, retry_after=retry_after, on_retry=on_retry, timeout=timeout, reraise_as_retry_error=reraise_as_retry_error, circuit=circuit, limiter=limiter, budget=budget)
        return wrapper
    return decorator
