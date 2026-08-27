"""
Uretim kalitesinde retry / attempt yardimcilari.

- Exponential backoff + opsiyonel jitter (equal veya AWS full jitter)
- Circuit breaker entegrasyonu (acik devrede kisa devre)
- Decorator ve fonksiyon arayuzu (sync + async)
"""

from __future__ import annotations

import asyncio
import functools
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, List, Literal, Optional, Tuple, Type, TypeVar, Union

from .circuit import CircuitBreaker, CircuitOpenError

T = TypeVar("T")
ExceptionType = Union[Type[BaseException], Tuple[Type[BaseException], ...]]
RetryPredicate = Callable[[BaseException], bool]
ResultPredicate = Callable[[Any], bool]
RetryAfterExtractor = Callable[[Any], Optional[float]]
JitterMode = Union[bool, Literal["full", "equal"]]


class RetryAttempt:
    __slots__ = ("number", "exception", "result", "delay")

    def __init__(self, number: int, exception: Optional[BaseException] = None, result: Any = None, delay: Optional[float] = None) -> None:
        self.number = number
        self.exception = exception
        self.result = result
        self.delay = delay

    def __repr__(self) -> str:
        if self.exception is not None:
            detail = f"exc={type(self.exception).__name__}({self.exception!s})"
        else:
            detail = f"result={self.result!r}"
        delay_part = f", delay={self.delay:.4f}" if self.delay is not None else ""
        return f"RetryAttempt(number={self.number}, {detail}{delay_part})"


class RetryError(Exception):
    def __init__(self, last_exception: Optional[BaseException] = None, attempts: int = 0, last_result: Any = None, history: Optional[List[RetryAttempt]] = None) -> None:
        self.last_exception = last_exception
        self.attempts = attempts
        self.last_result = last_result
        self.history = list(history) if history else []
        if last_exception is not None:
            message = f"{attempts} deneme sonunda basarisiz: {type(last_exception).__name__}: {last_exception}"
        else:
            message = f"{attempts} deneme sonunda sonuc kabul edilmedi (last_result={last_result!r})"
        super().__init__(message)


def _parse_http_date_delay(value: Any) -> Optional[float]:
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


def extract_retry_after(source: Any) -> Optional[float]:
    if source is None:
        return None
    value: Any = getattr(source, "retry_after", None)
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


def _compute_delay(attempt_number: int, base_delay: float, max_delay: float, exponential_base: float, jitter: JitterMode) -> float:
    delay = min(base_delay * (exponential_base ** (attempt_number - 1)), max_delay)
    if delay <= 0:
        return 0.0
    if jitter is False:
        return delay
    if jitter == "full":
        return random.uniform(0.0, delay)
    return delay * (0.75 + random.random() * 0.5)


def _should_retry(exc: BaseException, retry_if: Optional[RetryPredicate]) -> bool:
    if retry_if is None:
        return True
    return bool(retry_if(exc))


def _should_retry_result(result: Any, retry_if_result: Optional[ResultPredicate]) -> bool:
    if retry_if_result is None:
        return False
    return bool(retry_if_result(result))


def _validate_params(max_attempts: int, base_delay: float, max_delay: float, exponential_base: float, timeout: Optional[float]) -> None:
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


def _resolve_delay(attempt_num: int, base_delay: float, max_delay: float, exponential_base: float, jitter: JitterMode, last_exc: Optional[BaseException], last_result: Any, retry_after: Optional[RetryAfterExtractor]) -> float:
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


def _raise_if_circuit_open(circuit: Optional[CircuitBreaker], attempt_num: int, last_result: Any, history: List[RetryAttempt], reraise_as_retry_error: bool) -> None:
    if circuit is None or circuit.allow():
        return
    wait = circuit.seconds_until_retry()
    err = CircuitOpenError(
        f"CircuitBreaker {circuit.name!r} acik; {wait:.3f}s sonra denenebilir",
        retry_after=wait,
        breaker=circuit,
    )
    done = max(0, attempt_num - 1)
    if reraise_as_retry_error:
        raise RetryError(err, done, last_result, history=history) from err
    raise err
