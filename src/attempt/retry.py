"""
Üretim kalitesinde retry / attempt yardımcıları.

- Exponential backoff + opsiyonel jitter
- Maksimum deneme sayısı ve toplam zaman aşımı
- Belirli exception türlerini filtreleme
- Opsiyonel retry_if predicate ile ince taneli kontrol
- Decorator ve fonksiyon arayüzü (sync + async)
- RetryError: son exception + deneme sayısı ile sarmalama
"""

from __future__ import annotations

import asyncio
import functools
import random
import time
from typing import Any, Awaitable, Callable, Optional, Tuple, Type, TypeVar, Union

T = TypeVar("T")
ExceptionType = Union[Type[BaseException], Tuple[Type[BaseException], ...]]
RetryPredicate = Callable[[BaseException], bool]


class RetryError(Exception):
    """Tüm denemeler tükendiğinde yükseltilir.

    Attributes:
        last_exception: Son başarısız denemedeki exception.
        attempts: Yapılan toplam deneme sayısı.
    """

    def __init__(self, last_exception: BaseException, attempts: int) -> None:
        self.last_exception = last_exception
        self.attempts = attempts
        message = (
            f"{attempts} deneme sonunda başarısız: "
            f"{type(last_exception).__name__}: {last_exception}"
        )
        super().__init__(message)


def _compute_delay(
    attempt_number: int,
    base_delay: float,
    max_delay: float,
    exponential_base: float,
    jitter: bool,
) -> float:
    """Verilen deneme numarası için bekleme süresini hesapla."""
    delay = min(base_delay * (exponential_base ** (attempt_number - 1)), max_delay)
    if jitter and delay > 0:
        # ±%25 jitter
        delay = delay * (0.75 + random.random() * 0.5)
    return max(0.0, delay)


def _should_retry(exc: BaseException, retry_if: Optional[RetryPredicate]) -> bool:
    """retry_if verilmişse onu kullan; yoksa her zaman True."""
    if retry_if is None:
        return True
    return bool(retry_if(exc))


def attempt(
    func: Callable[..., T],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    exceptions: ExceptionType = (Exception,),
    retry_if: Optional[RetryPredicate] = None,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    timeout: Optional[float] = None,
    reraise_as_retry_error: bool = False,
) -> T:
    """
    Bir fonksiyonu belirli koşullar altında yeniden dene.

    Args:
        func: Çağrılacak callable (argümansız veya lambda).
        max_attempts: Maksimum deneme sayısı (≥ 1).
        base_delay: İlk bekleme süresi (saniye).
        max_delay: Üst sınır bekleme süresi (saniye).
        exponential_base: Üstel çarpan (genelde 2.0).
        jitter: True ise rastgele ±%25 jitter eklenir.
        exceptions: Yakalanacak exception sınıf(lar)ı.
        retry_if: Opsiyonel predicate. Yakalanan exception için
                  True dönerse yeniden dene, False dönerse hemen yükselt.
                  Örnek: lambda e: getattr(e, "status_code", None) in (429, 503)
        on_retry: Her yeniden denemeden önce çağrılır:
                  on_retry(attempt_number, exception, delay_seconds)
        timeout: Toplam maksimum çalışma süresi (saniye). Aşılırsa
                 son exception yeniden yükseltilir.
        reraise_as_retry_error: True ise başarısızlıkta orijinal exception
                 yerine RetryError yükseltilir (last_exception + attempts).

    Returns:
        func'ın başarılı dönüş değeri.

    Raises:
        Son denemede yükselen exception (veya timeout / RetryError).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts en az 1 olmalıdır")

    start_time = time.monotonic()
    last_exc: Optional[BaseException] = None

    for attempt_num in range(1, max_attempts + 1):
        if timeout is not None and (time.monotonic() - start_time) >= timeout:
            if last_exc is not None:
                if reraise_as_retry_error:
                    raise RetryError(last_exc, attempt_num - 1) from last_exc
                raise last_exc
            raise TimeoutError(f"Toplam timeout ({timeout}s) aşıldı")

        try:
            return func()
        except exceptions as exc:
            last_exc = exc
            if attempt_num >= max_attempts or not _should_retry(exc, retry_if):
                if reraise_as_retry_error:
                    raise RetryError(exc, attempt_num) from exc
                raise

            delay = _compute_delay(
                attempt_num, base_delay, max_delay, exponential_base, jitter
            )

            if on_retry is not None:
                on_retry(attempt_num, exc, delay)

            if delay > 0:
                # Kalan timeout süresini aşmamaya dikkat et
                if timeout is not None:
                    remaining = timeout - (time.monotonic() - start_time)
                    if remaining <= 0:
                        if reraise_as_retry_error:
                            raise RetryError(exc, attempt_num) from exc
                        raise
                    delay = min(delay, remaining)
                time.sleep(delay)

    # Teorik olarak buraya gelinmez
    assert last_exc is not None
    if reraise_as_retry_error:
        raise RetryError(last_exc, max_attempts) from last_exc
    raise last_exc


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    exceptions: ExceptionType = (Exception,),
    retry_if: Optional[RetryPredicate] = None,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    timeout: Optional[float] = None,
    reraise_as_retry_error: bool = False,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Fonksiyonları otomatik olarak yeniden deneyen decorator.

    Örnek:
        @retry(max_attempts=5, base_delay=0.5, exceptions=(ConnectionError,))
        def fetch(url: str) -> str:
            ...
    """

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
    jitter: bool = True,
    exceptions: ExceptionType = (Exception,),
    retry_if: Optional[RetryPredicate] = None,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    timeout: Optional[float] = None,
    reraise_as_retry_error: bool = False,
) -> T:
    """
    Bir async fonksiyonu belirli koşullar altında yeniden dene.

    Args:
        func: Çağrılacak async callable (argümansız veya lambda).
        Diğer parametreler `attempt` ile aynıdır; bekleme `asyncio.sleep` kullanır.

    Returns:
        func'ın başarılı dönüş değeri.

    Raises:
        Son denemede yükselen exception (veya timeout / RetryError).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts en az 1 olmalıdır")

    start_time = time.monotonic()
    last_exc: Optional[BaseException] = None

    for attempt_num in range(1, max_attempts + 1):
        if timeout is not None and (time.monotonic() - start_time) >= timeout:
            if last_exc is not None:
                if reraise_as_retry_error:
                    raise RetryError(last_exc, attempt_num - 1) from last_exc
                raise last_exc
            raise TimeoutError(f"Toplam timeout ({timeout}s) aşıldı")

        try:
            return await func()
        except exceptions as exc:
            last_exc = exc
            if attempt_num >= max_attempts or not _should_retry(exc, retry_if):
                if reraise_as_retry_error:
                    raise RetryError(exc, attempt_num) from exc
                raise

            delay = _compute_delay(
                attempt_num, base_delay, max_delay, exponential_base, jitter
            )

            if on_retry is not None:
                on_retry(attempt_num, exc, delay)

            if delay > 0:
                if timeout is not None:
                    remaining = timeout - (time.monotonic() - start_time)
                    if remaining <= 0:
                        if reraise_as_retry_error:
                            raise RetryError(exc, attempt_num) from exc
                        raise
                    delay = min(delay, remaining)
                await asyncio.sleep(delay)

    assert last_exc is not None
    if reraise_as_retry_error:
        raise RetryError(last_exc, max_attempts) from last_exc
    raise last_exc


def async_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    exceptions: ExceptionType = (Exception,),
    retry_if: Optional[RetryPredicate] = None,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    timeout: Optional[float] = None,
    reraise_as_retry_error: bool = False,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """
    Async fonksiyonları otomatik olarak yeniden deneyen decorator.

    Örnek:
        @async_retry(max_attempts=5, base_delay=0.5, exceptions=(ConnectionError,))
        async def fetch(url: str) -> str:
            ...
    """

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
                on_retry=on_retry,
                timeout=timeout,
                reraise_as_retry_error=reraise_as_retry_error,
            )

        return wrapper

    return decorator
