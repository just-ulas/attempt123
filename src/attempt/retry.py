from __future__ import annotations

import asyncio
import functools
import inspect
import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, List, Literal, Optional, Tuple, Type, TypeVar, Union

from .bulkhead import Bulkhead, BulkheadFullError
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


class GiveUpContext:
    """Yeniden denemeler bittiğinde fallback'e verilen bağlam."""

    __slots__ = ("exception", "result", "history", "attempts")

    def __init__(self, exception=None, result=None, history=None, attempts=0):
        self.exception = exception
        self.result = result
        self.history = list(history) if history else []
        self.attempts = attempts

    def __repr__(self):
        exc = type(self.exception).__name__ if self.exception is not None else None
        return (
            f"GiveUpContext(attempts={self.attempts}, exception={exc}, "
            f"result={self.result!r})"
        )


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


def _validate_params(max_attempts, base_delay, max_delay, exponential_base, timeout, attempt_timeout=None):
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
    if attempt_timeout is not None and attempt_timeout <= 0:
        raise ValueError("attempt_timeout pozitif olmalidir")


def _call_sync_with_timeout(func, attempt_timeout):
    """Run func(); if attempt_timeout is set, abort waiting after that many seconds.

    The worker is a daemon thread so a hung call cannot keep the process alive.
    The thread itself cannot be killed from userland; the retry loop moves on.
    """
    if attempt_timeout is None:
        return func()
    box = {}
    done = threading.Event()

    def run():
        try:
            box["value"] = func()
        except BaseException as exc:
            box["exc"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    if not done.wait(attempt_timeout):
        raise TimeoutError(f"Deneme zamanaşımı ({attempt_timeout}s) aşıldı")
    if "exc" in box:
        raise box["exc"]
    return box["value"]


async def _call_async_with_timeout(func, attempt_timeout):
    if attempt_timeout is None:
        return await func()
    try:
        return await asyncio.wait_for(func(), timeout=attempt_timeout)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"Deneme zamanaşımı ({attempt_timeout}s) aşıldı") from exc


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


def _raise_bulkhead_full(bulkhead, attempt_num, last_result, history, reraise_as_retry_error):
    err = BulkheadFullError(
        f"Bulkhead {bulkhead.name!r} dolu "
        f"(inflight={bulkhead.inflight}/{bulkhead.max_concurrent})",
        bulkhead=bulkhead,
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


def _call_fallback(fallback, ctx):
    try:
        return fallback(ctx)
    except TypeError:
        return fallback()


def _give_up_or_fallback(fallback, attempt_num, last_exc, last_result, history, reraise_as_retry_error):
    if fallback is not None:
        ctx = GiveUpContext(
            exception=last_exc,
            result=last_result,
            history=history,
            attempts=attempt_num,
        )
        return _call_fallback(fallback, ctx)
    _give_up(attempt_num, last_exc, last_result, history, reraise_as_retry_error)


async def _give_up_or_fallback_async(fallback, attempt_num, last_exc, last_result, history, reraise_as_retry_error):
    if fallback is not None:
        ctx = GiveUpContext(
            exception=last_exc,
            result=last_result,
            history=history,
            attempts=attempt_num,
        )
        value = _call_fallback(fallback, ctx)
        if inspect.isawaitable(value):
            return await value
        return value
    _give_up(attempt_num, last_exc, last_result, history, reraise_as_retry_error)


def _overall_timeout_sync(fallback, timeout, attempt_num, last_exc, last_result, history, reraise_as_retry_error):
    done = max(0, attempt_num - 1)
    if last_exc is None and last_result is None:
        last_exc = TimeoutError(f"Toplam timeout ({timeout}s) asildi")
    return _give_up_or_fallback(fallback, done, last_exc, last_result, history, reraise_as_retry_error)


async def _overall_timeout_async(fallback, timeout, attempt_num, last_exc, last_result, history, reraise_as_retry_error):
    done = max(0, attempt_num - 1)
    if last_exc is None and last_result is None:
        last_exc = TimeoutError(f"Toplam timeout ({timeout}s) asildi")
    return await _give_up_or_fallback_async(
        fallback, done, last_exc, last_result, history, reraise_as_retry_error
    )


_RETRY_KW = dict(
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
    attempt_timeout=None,
    reraise_as_retry_error=False,
    circuit=None,
    limiter=None,
    budget=None,
    fallback=None,
    bulkhead=None,
)


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
    attempt_timeout=None,
    reraise_as_retry_error=False,
    circuit=None,
    limiter=None,
    budget=None,
    fallback=None,
    bulkhead=None,
):
    _validate_params(max_attempts, base_delay, max_delay, exponential_base, timeout, attempt_timeout)
    start_time = time.monotonic()
    last_exc = None
    last_result = None
    history = []
    if budget is not None:
        budget.record_request()

    for attempt_num in range(1, max_attempts + 1):
        _raise_if_circuit_open(circuit, attempt_num, last_result, history, reraise_as_retry_error)
        if timeout is not None and (time.monotonic() - start_time) >= timeout:
            return _overall_timeout_sync(
                fallback, timeout, attempt_num, last_exc, last_result, history, reraise_as_retry_error
            )

        if limiter is not None:
            budget_wait = _limiter_wait_budget(timeout, start_time)
            if not limiter.acquire(1.0, timeout=budget_wait):
                _raise_rate_limited(limiter, attempt_num, last_result, history, reraise_as_retry_error)

        acquired = False
        if bulkhead is not None:
            budget_wait = _limiter_wait_budget(timeout, start_time)
            if not bulkhead.acquire(budget_wait):
                _raise_bulkhead_full(bulkhead, attempt_num, last_result, history, reraise_as_retry_error)
            acquired = True

        try:
            try:
                result = _call_sync_with_timeout(func, attempt_timeout)
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
                    return _give_up_or_fallback(
                        fallback, attempt_num, last_exc, last_result, history, reraise_as_retry_error
                    )
            except CircuitOpenError:
                raise
            except RateLimitError:
                raise
            except BulkheadFullError:
                raise
            except exceptions as exc:
                last_exc = exc
                last_result = None
                if circuit is not None:
                    circuit.record_failure()
                history.append(RetryAttempt(attempt_num, exception=exc))
                if attempt_num >= max_attempts or not _should_retry(exc, retry_if):
                    return _give_up_or_fallback(
                        fallback, attempt_num, last_exc, last_result, history, reraise_as_retry_error
                    )
        finally:
            if acquired:
                bulkhead.release()

        if budget is not None and not budget.try_retry():
            return _give_up_or_fallback(
                fallback, attempt_num, last_exc, last_result, history, reraise_as_retry_error
            )

        delay = _resolve_delay(attempt_num, base_delay, max_delay, exponential_base, jitter, last_exc, last_result, retry_after)
        history[-1].delay = delay
        if on_retry is not None:
            on_retry(attempt_num, last_exc, delay)
        if delay > 0:
            if timeout is not None:
                remaining = timeout - (time.monotonic() - start_time)
                if remaining <= 0:
                    return _overall_timeout_sync(
                        fallback, timeout, attempt_num + 1, last_exc, last_result, history, reraise_as_retry_error
                    )
                delay = min(delay, remaining)
            time.sleep(delay)

    return _give_up_or_fallback(
        fallback, max_attempts, last_exc, last_result, history, reraise_as_retry_error
    )


def retry(
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
    attempt_timeout=None,
    reraise_as_retry_error=False,
    circuit=None,
    limiter=None,
    budget=None,
    fallback=None,
    bulkhead=None,
):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return attempt(
                lambda: func(*args, **kwargs),
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                exponential_base=exponential_base,
                jitter=jitter,
                exceptions=exceptions,
                retry_if=retry_if,
                retry_if_result=retry_if_result,
                retry_after=retry_after,
                on_retry=on_retry,
                timeout=timeout,
                attempt_timeout=attempt_timeout,
                reraise_as_retry_error=reraise_as_retry_error,
                circuit=circuit,
                limiter=limiter,
                budget=budget,
                fallback=fallback,
                bulkhead=bulkhead,
            )
        return wrapper
    return decorator


async def async_attempt(
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
    attempt_timeout=None,
    reraise_as_retry_error=False,
    circuit=None,
    limiter=None,
    budget=None,
    fallback=None,
    bulkhead=None,
):
    _validate_params(max_attempts, base_delay, max_delay, exponential_base, timeout, attempt_timeout)
    start_time = time.monotonic()
    last_exc = None
    last_result = None
    history = []
    if budget is not None:
        budget.record_request()

    for attempt_num in range(1, max_attempts + 1):
        _raise_if_circuit_open(circuit, attempt_num, last_result, history, reraise_as_retry_error)
        if timeout is not None and (time.monotonic() - start_time) >= timeout:
            return await _overall_timeout_async(
                fallback, timeout, attempt_num, last_exc, last_result, history, reraise_as_retry_error
            )

        if limiter is not None:
            budget_wait = _limiter_wait_budget(timeout, start_time)
            if not await limiter.acquire_async(1.0, timeout=budget_wait):
                _raise_rate_limited(limiter, attempt_num, last_result, history, reraise_as_retry_error)

        acquired = False
        if bulkhead is not None:
            budget_wait = _limiter_wait_budget(timeout, start_time)
            if not await bulkhead.acquire_async(budget_wait):
                _raise_bulkhead_full(bulkhead, attempt_num, last_result, history, reraise_as_retry_error)
            acquired = True

        try:
            try:
                result = await _call_async_with_timeout(func, attempt_timeout)
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
                    return await _give_up_or_fallback_async(
                        fallback, attempt_num, last_exc, last_result, history, reraise_as_retry_error
                    )
            except CircuitOpenError:
                raise
            except RateLimitError:
                raise
            except BulkheadFullError:
                raise
            except exceptions as exc:
                last_exc = exc
                last_result = None
                if circuit is not None:
                    circuit.record_failure()
                history.append(RetryAttempt(attempt_num, exception=exc))
                if attempt_num >= max_attempts or not _should_retry(exc, retry_if):
                    return await _give_up_or_fallback_async(
                        fallback, attempt_num, last_exc, last_result, history, reraise_as_retry_error
                    )
        finally:
            if acquired:
                bulkhead.release()

        if budget is not None and not budget.try_retry():
            return await _give_up_or_fallback_async(
                fallback, attempt_num, last_exc, last_result, history, reraise_as_retry_error
            )

        delay = _resolve_delay(attempt_num, base_delay, max_delay, exponential_base, jitter, last_exc, last_result, retry_after)
        history[-1].delay = delay
        if on_retry is not None:
            on_retry(attempt_num, last_exc, delay)
        if delay > 0:
            if timeout is not None:
                remaining = timeout - (time.monotonic() - start_time)
                if remaining <= 0:
                    return await _overall_timeout_async(
                        fallback, timeout, attempt_num + 1, last_exc, last_result, history, reraise_as_retry_error
                    )
                delay = min(delay, remaining)
            await asyncio.sleep(delay)

    return await _give_up_or_fallback_async(
        fallback, max_attempts, last_exc, last_result, history, reraise_as_retry_error
    )


def async_retry(
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
    attempt_timeout=None,
    reraise_as_retry_error=False,
    circuit=None,
    limiter=None,
    budget=None,
    fallback=None,
    bulkhead=None,
):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await async_attempt(
                lambda: func(*args, **kwargs),
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                exponential_base=exponential_base,
                jitter=jitter,
                exceptions=exceptions,
                retry_if=retry_if,
                retry_if_result=retry_if_result,
                retry_after=retry_after,
                on_retry=on_retry,
                timeout=timeout,
                attempt_timeout=attempt_timeout,
                reraise_as_retry_error=reraise_as_retry_error,
                circuit=circuit,
                limiter=limiter,
                budget=budget,
                fallback=fallback,
                bulkhead=bulkhead,
            )
        return wrapper
    return decorator
