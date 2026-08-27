"""Token-bucket rate limiter — retry storm ve thundering herd'u keser.

``attempt(..., limiter=limiter)`` her denemeden önce bir token alır.
Token yoksa bekleme süresi timeout'a sığmazsa ``RateLimitError`` yükselir.
Aynı limiter örneğini birden fazla çağrı / thread / task paylaşabilir.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional


class RateLimitError(Exception):
    """Limiter token vermedi (timeout doldu veya rate=0).

    Attributes:
        retry_after: Sonraki token'a kalan saniye (yoksa None).
        limiter: İlgili RateLimiter (varsa).
    """

    def __init__(
        self,
        message: str = "rate limit aşıldı",
        *,
        retry_after: Optional[float] = None,
        limiter: Optional["RateLimiter"] = None,
    ) -> None:
        self.retry_after = retry_after
        self.limiter = limiter
        super().__init__(message)


class RateLimiter:
    """Thread-safe token-bucket.

    Args:
        rate: Saniyede üretilen token sayısı (sürekli refill).
        burst: Kovadaki maksimum token (ani yük kapasitesi).
        name: Log / hata mesajı için etiket.
    """

    def __init__(
        self,
        rate: float = 10.0,
        burst: Optional[float] = None,
        name: str = "default",
    ) -> None:
        if rate < 0:
            raise ValueError("rate negatif olamaz")
        capacity = float(burst) if burst is not None else max(1.0, float(rate) if rate > 0 else 1.0)
        if capacity <= 0:
            raise ValueError("burst pozitif olmalıdır")
        self.rate = float(rate)
        self.burst = capacity
        self.name = name
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    @property
    def tokens(self) -> float:
        """Şu anki (refill edilmiş) token bakiyesi."""
        with self._lock:
            self._refill_unlocked()
            return self._tokens

    def seconds_until_available(self, amount: float = 1.0) -> float:
        """``amount`` token için beklenecek saniye. Hemen varsa 0."""
        if amount <= 0:
            return 0.0
        with self._lock:
            self._refill_unlocked()
            if self._tokens >= amount:
                return 0.0
            if self.rate <= 0:
                return float("inf")
            return (amount - self._tokens) / self.rate

    def try_acquire(self, amount: float = 1.0) -> bool:
        """Beklemeden token al. Yetersizse False."""
        if amount <= 0:
            return True
        with self._lock:
            self._refill_unlocked()
            if self._tokens < amount:
                return False
            self._tokens -= amount
            return True

    def acquire(self, amount: float = 1.0, timeout: Optional[float] = None) -> bool:
        """Token al; gerekirse uyu. Timeout dolarsa False.

        ``timeout=0`` try_acquire ile aynıdır. ``timeout=None`` sınırsız bekler
        (rate=0 ve yetersiz bakiyede False döner).
        """
        if amount <= 0:
            return True
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            if self.try_acquire(amount):
                return True
            wait = self.seconds_until_available(amount)
            if wait == float("inf"):
                return False
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            if wait > 0:
                time.sleep(wait)

    async def acquire_async(self, amount: float = 1.0, timeout: Optional[float] = None) -> bool:
        """async karşılık — event loop'u bloklamaz."""
        if amount <= 0:
            return True
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            if self.try_acquire(amount):
                return True
            wait = self.seconds_until_available(amount)
            if wait == float("inf"):
                return False
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            if wait > 0:
                await asyncio.sleep(wait)

    def reset(self) -> None:
        """Kovayı tekrar dolu hale getir."""
        with self._lock:
            self._tokens = self.burst
            self._last = time.monotonic()

    def _refill_unlocked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        if elapsed > 0 and self.rate > 0:
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate)

    def __repr__(self) -> str:
        return (
            f"RateLimiter(name={self.name!r}, rate={self.rate}, "
            f"burst={self.burst}, tokens={self.tokens:.3f})"
        )


__all__ = ["RateLimiter", "RateLimitError"]
