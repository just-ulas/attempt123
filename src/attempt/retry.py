"""
Üretim kalitesinde retry / attempt yardımcıları.

- Exponential backoff + opsiyonel jitter
- Maksimum deneme sayısı ve toplam zaman aşımı
- Belirli exception türlerini filtreleme
- Decorator ve fonksiyon arayüzü
"""

from __future__ import annotations

import functools
import random
import time
from typing import Any, Callable, Optional, Tuple, Type, TypeVar, Union

T = TypeVar("T")
ExceptionType = Union[Type[BaseException], Tuple[Type[BaseException], ...]]


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


def attempt(
    func: Callable[..., T],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    exceptions: ExceptionType = (Exception,),
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    timeout: Optional[float] = None,
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
        on_retry: Her yeniden denemeden önce çağrılır:
                  on_retry(attempt_number, exception, delay_seconds)
        timeout: Toplam maksimum çalışma süresi (saniye). Aşılırsa
                 son exception yeniden yükseltilir.

    Returns:
        func'ın başarılı dönüş değeri.

    Raises:
        Son denemede yükselen exception (veya timeout durumunda).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts en az 1 olmalıdır")

    start_time = time.monotonic()
    last_exc: Optional[BaseException] = None

    for attempt_num in range(1, max_attempts + 1):
        if timeout is not None and (time.monotonic() - start_time) >= timeout:
            if last_exc is not None:
                raise last_exc
            raise TimeoutError(f"Toplam timeout ({timeout}s) aşıldı")

        try:
            return func()
        except exceptions as exc:
            last_exc = exc
            if attempt_num >= max_attempts:
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
                        raise
                    delay = min(delay, remaining)
                time.sleep(delay)

    # Teorik olarak buraya gelinmez
    assert last_exc is not None
    raise last_exc


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    exceptions: ExceptionType = (Exception,),
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    timeout: Optional[float] = None,
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
                on_retry=on_retry,
                timeout=timeout,
            )

        return wrapper

    return decorator
