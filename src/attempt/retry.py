"""
Üretim kalitesinde retry / attempt yardımcıları.

- Exponential backoff + opsiyonel jitter (equal veya AWS full jitter)
- Maksimum deneme sayısı ve toplam zaman aşımı
- Belirli exception türlerini filtreleme
- Opsiyonel retry_if predicate ile ince taneli exception kontrolü
- Opsiyonel retry_if_result predicate ile sonuç tabanlı yeniden deneme
- Decorator ve fonksiyon arayüzü (sync + async)
- RetryError: son exception + deneme sayısı ile sarmalama
"""

from __future__ import annotations

import asyncio
import functools
import random
import time
from typing import Any, Awaitable, Callable, Literal, Optional, Tuple, Type, TypeVar, Union

T = TypeVar("T")
ExceptionType = Union[Type[BaseException], Tuple[Type[BaseException], ...]]
RetryPredicate = Callable[[BaseException], bool]
ResultPredicate = Callable[[Any], bool]
JitterMode = Union[bool, Literal["full", "equal"]]


class RetryError(Exception):
    """Tüm denemeler tükendiğinde yükseltilir.

    Attributes:
        last_exception: Son başarısız denemedeki exception (varsa).
        attempts: Yapılan toplam deneme sayısı.
        last_result: Son kabul edilmeyen sonuç (retry_if_result ile, varsa).
    """

    def __init__(
        self,
        last_exception: Optional[BaseException] = None,
        attempts: int = 0,
        last_result: Any = None,
    ) -> None:
        self.last_exception = last_exception
        self.attempts = attempts
        self.last_result = last_result
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


def _compute_delay(
    attempt_number: int,
    base_delay: float,
    max_delay: float,
    exponential_base: float,
    jitter: JitterMode,
) -> float:
    """Verilen deneme numarası için bekleme süresini hesapla.

    jitter:
      - False / None: jitter yok, saf exponential
      - True / "equal": ±%25 equal jitter (varsayılan, geriye uyumlu)
      - "full": AWS full jitter — [0, delay] aralığından uniform seçim
        (dağıtık sistemlerde thundering herd için önerilen strateji)
    """
    delay = min(base_delay * (exponential_base ** (attempt_number - 1)), max_delay)
    if delay <= 0:
        return 0.0

    if jitter is False:
        return delay
    if jitter == "full":
        # AWS Architecture Blog: full jitter
        return random.uniform(0.0, delay)
    # True veya "equal" → ±%25 equal jitter
    return delay * (0.75 + random.random() * 0.5)


def _should_retry(exc: BaseException, retry_if: Optional[RetryPredicate]) -> bool:
    """retry_if verilmişse onu kullan; yoksa her zaman True."""
    if retry_if is None:
        return True
    return bool(retry_if(exc))


def _should_retry_result(result: Any, retry_if_result: Optional[ResultPredicate]) -> bool:
    """retry_if_result True dönerse sonucu reddet ve yeniden dene."""
    if retry_if_result is None:
        return False
    return bool(retry_if_result(result))


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
        jitter: Jitter stratejisi.
            - True / "equal": ±%25 equal jitter (varsayılan).
            - "full": AWS full jitter — [0, delay] uniform (thundering herd için ideal).
            - False: jitter yok.
        exceptions: Yakalanacak exception sınıf(lar)ı.
        retry_if: Opsiyonel predicate. Yakalanan exception için
                  True dönerse yeniden dene, False dönerse hemen yükselt.
                  Örnek: lambda e: getattr(e, "status_code", None) in (429, 503)
        retry_if_result: Opsiyonel predicate. Başarılı dönüş değeri için
                  True dönerse sonucu reddet ve yeniden dene.
                  Örnek: lambda r: r is None or r == []
                  veya lambda r: getattr(r, "get", lambda k: None)("error")
        on_retry: Her yeniden denemeden önce çağrılır:
                  on_retry(attempt_number, exception_or_None, delay_seconds)
                  Sonuç reddedildiğinde exception_or_None None olur.
        timeout: Toplam maksimum çalışma süresi (saniye). Aşılırsa
                 son exception yeniden yükseltilir.
        reraise_as_retry_error: True ise başarısızlıkta orijinal exception
                 yerine RetryError yükseltilir (last_exception + attempts).

    Returns:
        func'ın başarılı ve kabul edilen dönüş değeri.

    Raises:
        Son denemede yükselen exception (veya timeout / RetryError).
        Sonuç reddedilip denemeler tükenirse RetryError (reraise_as_retry_error
        True ise) veya RuntimeError.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts en az 1 olmalıdır")

    start_time = time.monotonic()
    last_exc: Optional[BaseException] = None
    last_result: Any = None

    for attempt_num in range(1, max_attempts + 1):
        if timeout is not None and (time.monotonic() - start_time) >= timeout:
            if last_exc is not None:
                if reraise_as_retry_error:
                    raise RetryError(last_exc, attempt_num - 1) from last_exc
                raise last_exc
            if last_result is not None or retry_if_result is not None:
                if reraise_as_retry_error:
                    raise RetryError(None, attempt_num - 1, last_result)
                raise RuntimeError(
                    f"Toplam timeout ({timeout}s) aşıldı; sonuç kabul edilmedi"
                )
            raise TimeoutError(f"Toplam timeout ({timeout}s) aşıldı")

        try:
            result = func()
            if not _should_retry_result(result, retry_if_result):
                return result
            # Sonuç reddedildi → yeniden dene
            last_result = result
            last_exc = None
            if attempt_num >= max_attempts:
                if reraise_as_retry_error:
                    raise RetryError(None, attempt_num, last_result)
                raise RuntimeError(
                    f"{attempt_num} deneme sonunda sonuç kabul edilmedi: {last_result!r}"
                )
        except exceptions as exc:
            last_exc = exc
            last_result = None
            if attempt_num >= max_attempts or not _should_retry(exc, retry_if):
                if reraise_as_retry_error:
                    raise RetryError(exc, attempt_num) from exc
                raise

        delay = _compute_delay(
            attempt_num, base_delay, max_delay, exponential_base, jitter
        )

        if on_retry is not None:
            on_retry(attempt_num, last_exc, delay)  # type: ignore[arg-type]

        if delay > 0:
            # Kalan timeout süresini aşmamaya dikkat et
            if timeout is not None:
                remaining = timeout - (time.monotonic() - start_time)
                if remaining <= 0:
                    if last_exc is not None:
                        if reraise_as_retry_error:
                            raise RetryError(last_exc, attempt_num) from last_exc
                        raise last_exc
                    if reraise_as_retry_error:
                        raise RetryError(None, attempt_num, last_result)
                    raise RuntimeError("Timeout aşıldı; sonuç kabul edilmedi")
                delay = min(delay, remaining)
            time.sleep(delay)

    # Teorik olarak buraya gelinmez
    if last_exc is not None:
        if reraise_as_retry_error:
            raise RetryError(last_exc, max_attempts) from last_exc
        raise last_exc
    if reraise_as_retry_error:
        raise RetryError(None, max_attempts, last_result)
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

        @retry(retry_if_result=lambda r: r is None or len(r) == 0)
        def get_items() -> list:
            ...

        @retry(jitter="full", max_attempts=5)  # AWS full jitter
        def call_api():
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
                retry_if_result=retry_if_result,
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
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    timeout: Optional[float] = None,
    reraise_as_retry_error: bool = False,
) -> T:
    """
    Bir async fonksiyonu belirli koşullar altında yeniden dene.

    Args:
        func: Çağrılacak async callable (argümansız veya lambda).
        Diğer parametreler `attempt` ile aynıdır; bekleme `asyncio.sleep` kullanır.
        jitter="full" desteklenir.
        retry_if_result desteklenir.

    Returns:
        func'ın başarılı ve kabul edilen dönüş değeri.

    Raises:
        Son denemede yükselen exception (veya timeout / RetryError).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts en az 1 olmalıdır")

    start_time = time.monotonic()
    last_exc: Optional[BaseException] = None
    last_result: Any = None

    for attempt_num in range(1, max_attempts + 1):
        if timeout is not None and (time.monotonic() - start_time) >= timeout:
            if last_exc is not None:
                if reraise_as_retry_error:
                    raise RetryError(last_exc, attempt_num - 1) from last_exc
                raise last_exc
            if last_result is not None or retry_if_result is not None:
                if reraise_as_retry_error:
                    raise RetryError(None, attempt_num - 1, last_result)
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
            if attempt_num >= max_attempts:
                if reraise_as_retry_error:
                    raise RetryError(None, attempt_num, last_result)
                raise RuntimeError(
                    f"{attempt_num} deneme sonunda sonuç kabul edilmedi: {last_result!r}"
                )
        except exceptions as exc:
            last_exc = exc
            last_result = None
            if attempt_num >= max_attempts or not _should_retry(exc, retry_if):
                if reraise_as_retry_error:
                    raise RetryError(exc, attempt_num) from exc
                raise

        delay = _compute_delay(
            attempt_num, base_delay, max_delay, exponential_base, jitter
        )

        if on_retry is not None:
            on_retry(attempt_num, last_exc, delay)  # type: ignore[arg-type]

        if delay > 0:
            if timeout is not None:
                remaining = timeout - (time.monotonic() - start_time)
                if remaining <= 0:
                    if last_exc is not None:
                        if reraise_as_retry_error:
                            raise RetryError(last_exc, attempt_num) from last_exc
                        raise last_exc
                    if reraise_as_retry_error:
                        raise RetryError(None, attempt_num, last_result)
                    raise RuntimeError("Timeout aşıldı; sonuç kabul edilmedi")
                delay = min(delay, remaining)
            await asyncio.sleep(delay)

    if last_exc is not None:
        if reraise_as_retry_error:
            raise RetryError(last_exc, max_attempts) from last_exc
        raise last_exc
    if reraise_as_retry_error:
        raise RetryError(None, max_attempts, last_result)
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

        @async_retry(retry_if_result=lambda r: not r.get("ok"))
        async def call_api() -> dict:
            ...

        @async_retry(jitter="full")
        async def distributed_call():
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
                retry_if_result=retry_if_result,
                on_retry=on_retry,
                timeout=timeout,
                reraise_as_retry_error=reraise_as_retry_error,
            )

        return wrapper

    return decorator
