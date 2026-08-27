"""
Üretim kalitesinde retry / attempt yardımcıları.

- Exponential backoff + opsiyonel jitter (equal veya AWS full jitter)
- Maksimum deneme sayısı ve toplam zaman aşımı
- Belirli exception türlerini filtreleme
- Opsiyonel retry_if predicate ile ince taneli exception kontrolü
- Opsiyonel retry_if_result predicate ile sonuç tabanlı yeniden deneme
- HTTP Retry-After (saniye veya RFC 7231 HTTP-date) / exception.retry_after
- Retry-After reddedilen sonuç nesnelerinden de okunur (Response 429 vb.)
- Decorator ve fonksiyon arayüzü (sync + async)
- RetryError: son exception + deneme sayısı + deneme geçmişi
"""

from __future__ import annotations

import asyncio
import functools
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, List, Literal, Optional, Tuple, Type, TypeVar, Union

T = TypeVar("T")
ExceptionType = Union[Type[BaseException], Tuple[Type[BaseException], ...]]
RetryPredicate = Callable[[BaseException], bool]
ResultPredicate = Callable[[Any], bool]
RetryAfterExtractor = Callable[[Any], Optional[float]]
JitterMode = Union[bool, Literal["full", "equal"]]


class RetryAttempt:
    """Tek bir denemenin kaydı."""

    __slots__ = ("number", "exception", "result", "delay")

    def __init__(
        self,
        number: int,
        exception: Optional[BaseException] = None,
        result: Any = None,
        delay: Optional[float] = None,
    ) -> None:
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
    """Tüm denemeler tükendiğinde yükseltilir.

    Attributes:
        last_exception: Son başarısız denemedeki exception (varsa).
        attempts: Yapılan toplam deneme sayısı.
        last_result: Son kabul edilmeyen sonuç (retry_if_result ile, varsa).
        history: Her denemenin RetryAttempt kaydı.
    """

    def __init__(
        self,
        last_exception: Optional[BaseException] = None,
        attempts: int = 0,
        last_result: Any = None,
        history: Optional[List[RetryAttempt]] = None,
    ) -> None:
        self.last_exception = last_exception
        self.attempts = attempts
        self.last_result = last_result
        self.history = list(history) if history else []
        if last_exception is not None:
            message = (
                f"{attempts} deneme sonunda başarısız: "
                f"{type(last_exception).__name__}: {last_exception}"
            )
        else:
            message = (
                f"{attempts} deneme sonunda sonuç kabul edilmedi "
                f"(last_result={last_result!r})"
            )
        super().__init__(message)


def _parse_http_date_delay(value: Any) -> Optional[float]:
    """RFC 7231 HTTP-date → şimdiden itibaren saniye. Parse edilemezse None."""
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
    """Exception veya Response benzeri nesneden önerilen beklemeyi (saniye) çıkar.

    Sıra:
      1. ``source.retry_after`` (sayı, sayıya çevrilebilir string veya HTTP-date)
      2. ``source.headers['Retry-After']`` / ``retry-after``
         - delay-seconds (``Retry-After: 120``)
         - RFC 7231 HTTP-date (``Retry-After: Wed, 21 Oct 2015 07:28:00 GMT``)

    Exception yükseltmeden dönen HTTP 429/503 Response nesnelerinde de çalışır.
    Parse edilemezse None döner; o durumda normal backoff kullanılır.
    Geçmiş bir HTTP-date 0 saniye olarak yorumlanır.
    """
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


def _compute_delay(
    attempt_number: int,
    base_delay: float,
    max_delay: float,
    exponential_base: float,
    jitter: JitterMode,
) -> float:
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


def _validate_params(
    max_attempts: int,
    base_delay: float,
    max_delay: float,
    exponential_base: float,
    timeout: Optional[float],
) -> None:
    if max_attempts < 1:
        raise ValueError("max_attempts en az 1 olmalıdır")
    if base_delay < 0:
        raise ValueError("base_delay negatif olamaz")
    if max_delay < 0:
        raise ValueError("max_delay negatif olamaz")
    if exponential_base < 1:
        raise ValueError("exponential_base en az 1 olmalıdır")
    if timeout is not None and timeout < 0:
        raise ValueError("timeout negatif olamaz")


def _resolve_delay(
    attempt_num: int,
    base_delay: float,
    max_delay: float,
    exponential_base: float,
    jitter: JitterMode,
    last_exc: Optional[BaseException],
    last_result: Any,
    retry_after: Optional[RetryAfterExtractor],
) -> float:
    delay = _compute_delay(
        attempt_num, base_delay, max_delay, exponential_base, jitter
    )
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


def attempt(
    func: Callable[..., T],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: JitterMode = True,
    exceptions: ExceptionType = (Exception,),
    retry_if: Optional[RetryPredicate] = None,
    retry_if_result: Optional[ResultPredicate] = None,
    retry_after: Optional[RetryAfterExtractor] = None,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    timeout: Optional[float] = None,
    reraise_as_retry_error: bool = False,
) -> T:
    _validate_params(max_attempts, base_delay, max_delay, exponential_base, timeout)
    start_time = time.monotonic()
    last_exc: Optional[BaseException] = None
    last_result: Any = None
    history: List[RetryAttempt] = []

    for attempt_num in range(1, max_attempts + 1):
        if timeout is not None and (time.monotonic() - start_time) >= timeout:
            if last_exc is not None:
                if reraise_as_retry_error:
                    raise RetryError(last_exc, attempt_num - 1, history=history) from last_exc
                raise last_exc
            if last_result is not None or retry_if_result is not None:
                if reraise_as_retry_error:
                    raise RetryError(None, attempt_num - 1, last_result, history=history)
                raise RuntimeError(
                    f"Toplam timeout ({timeout}s) aşıldı; sonuç kabul edilmedi"
                )
            raise TimeoutError(f"Toplam timeout ({timeout}s) aşıldı")

        try:
            result = func()
            if not _should_retry_result(result, retry_if_result):
                return result
            last_result = result
            last_exc = None
            history.append(RetryAttempt(attempt_num, result=result))
            if attempt_num >= max_attempts:
                if reraise_as_retry_error:
                    raise RetryError(None, attempt_num, last_result, history=history)
                raise RuntimeError(
                    f"{attempt_num} deneme sonunda sonuç kabul edilmedi: {last_result!r}"
                )
        except exceptions as exc:
            last_exc = exc
            last_result = None
            history.append(RetryAttempt(attempt_num, exception=exc))
            if attempt_num >= max_attempts or not _should_retry(exc, retry_if):
                if reraise_as_retry_error:
                    raise RetryError(exc, attempt_num, history=history) from exc
                raise

        delay = _resolve_delay(
            attempt_num,
            base_delay,
            max_delay,
            exponential_base,
            jitter,
            last_exc,
            last_result,
            retry_after,
        )
        history[-1].delay = delay

        if on_retry is not None:
            on_retry(attempt_num, last_exc, delay)  # type: ignore[arg-type]

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
                    raise RuntimeError("Timeout aşıldı; sonuç kabul edilmedi")
                delay = min(delay, remaining)
            time.sleep(delay)

    if last_exc is not None:
        if reraise_as_retry_error:
            raise RetryError(last_exc, max_attempts, history=history) from last_exc
        raise last_exc
    if reraise_as_retry_error:
        raise RetryError(None, max_attempts, last_result, history=history)
    raise RuntimeError(f"{max_attempts} deneme sonunda sonuç kabul edilmedi")


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: JitterMode = True,
    exceptions: ExceptionType = (Exception,),
    retry_if: Optional[RetryPredicate] = None,
    retry_if_result: Optional[ResultPredicate] = None,
    retry_after: Optional[RetryAfterExtractor] = None,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    timeout: Optional[float] = None,
    reraise_as_retry_error: bool = False,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
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
                reraise_as_retry_error=reraise_as_retry_error,
            )
        return wrapper
    return decorator


async def async_attempt(
    func: Callable[..., Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: JitterMode = True,
    exceptions: ExceptionType = (Exception,),
    retry_if: Optional[RetryPredicate] = None,
    retry_if_result: Optional[ResultPredicate] = None,
    retry_after: Optional[RetryAfterExtractor] = None,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    timeout: Optional[float] = None,
    reraise_as_retry_error: bool = False,
) -> T:
    _validate_params(max_attempts, base_delay, max_delay, exponential_base, timeout)
    start_time = time.monotonic()
    last_exc: Optional[BaseException] = None
    last_result: Any = None
    history: List[RetryAttempt] = []

    for attempt_num in range(1, max_attempts + 1):
        if timeout is not None and (time.monotonic() - start_time) >= timeout:
            if last_exc is not None:
                if reraise_as_retry_error:
                    raise RetryError(last_exc, attempt_num - 1, history=history) from last_exc
                raise last_exc
            if last_result is not None or retry_if_result is not None:
                if reraise_as_retry_error:
                    raise RetryError(None, attempt_num - 1, last_result, history=history)
                raise RuntimeError(
                    f"Toplam timeout ({timeout}s) aşıldı; sonuç kabul edilmedi"
                )
            raise TimeoutError(f"Toplam timeout ({timeout}s) aşıldı")

        try:
            result = await func()
            if not _should_retry_result(result, retry_if_result):
                return result
            last_result = result
            last_exc = None
            history.append(RetryAttempt(attempt_num, result=result))
            if attempt_num >= max_attempts:
                if reraise_as_retry_error:
                    raise RetryError(None, attempt_num, last_result, history=history)
                raise RuntimeError(
                    f"{attempt_num} deneme sonunda sonuç kabul edilmedi: {last_result!r}"
                )
        except exceptions as exc:
            last_exc = exc
            last_result = None
            history.append(RetryAttempt(attempt_num, exception=exc))
            if attempt_num >= max_attempts or not _should_retry(exc, retry_if):
                if reraise_as_retry_error:
                    raise RetryError(exc, attempt_num, history=history) from exc
                raise

        delay = _resolve_delay(
            attempt_num,
            base_delay,
            max_delay,
            exponential_base,
            jitter,
            last_exc,
            last_result,
            retry_after,
        )
        history[-1].delay = delay

        if on_retry is not None:
            on_retry(attempt_num, last_exc, delay)  # type: ignore[arg-type]

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
                    raise RuntimeError("Timeout aşıldı; sonuç kabul edilmedi")
                delay = min(delay, remaining)
            await asyncio.sleep(delay)

    if last_exc is not None:
        if reraise_as_retry_error:
            raise RetryError(last_exc, max_attempts, history=history) from last_exc
        raise last_exc
    if reraise_as_retry_error:
        raise RetryError(None, max_attempts, last_result, history=history)
    raise RuntimeError(f"{max_attempts} deneme sonunda sonuç kabul edilmedi")


def async_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: JitterMode = True,
    exceptions: ExceptionType = (Exception,),
    retry_if: Optional[RetryPredicate] = None,
    retry_if_result: Optional[ResultPredicate] = None,
    retry_after: Optional[RetryAfterExtractor] = None,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    timeout: Optional[float] = None,
    reraise_as_retry_error: bool = False,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
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
                reraise_as_retry_error=reraise_as_retry_error,
            )
        return wrapper
    return decorator
