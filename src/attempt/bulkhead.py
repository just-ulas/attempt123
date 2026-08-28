"""Bulkhead — eşzamanlı çağrı sayısını sınırlayarak bir bağımlılığın
tüm worker/thread/task havuzunu yemesini engeller.

Rate limiter *zaman içinde* hızı keser. Bulkhead *aynı anda* kaç çağrının
uçuşta olabileceğini keser. Yavaş bir upstream, retry ile birleşince
kolayca tüm kapasiteyi kilitler; bulkhead o sızıntıyı izole eder.

``attempt(..., bulkhead=bh)`` her denemede fonksiyonu çalıştırmadan önce
bir slot alır; slot yoksa timeout bütçesi içinde bekler, yetmezse
``BulkheadFullError`` yükselir. Slot, çağrı biter bitmez (başarı / hata)
serbest bırakılır — backoff uykusu slot tutmaz.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional


class BulkheadFullError(Exception):
    """Tüm bulkhead slotları dolu ve bekleme bütçesi yetmedi.

    Attributes:
        retry_after: Tahmini boşalma süresi (yoksa None).
        bulkhead: İlgili Bulkhead (varsa).
    """

    def __init__(
        self,
        message: str = "bulkhead dolu",
        *,
        retry_after: Optional[float] = None,
        bulkhead: Optional["Bulkhead"] = None,
    ) -> None:
        self.retry_after = retry_after
        self.bulkhead = bulkhead
        super().__init__(message)


class Bulkhead:
    """Thread-safe eşzamanlılık tavanı.

    Args:
        max_concurrent: Aynı anda izin verilen uçuştaki çağrı sayısı.
        name: Log / hata mesajı için etiket.
    """

    def __init__(self, max_concurrent: int = 8, name: str = "default") -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent en az 1 olmalıdır")
        self.max_concurrent = int(max_concurrent)
        self.name = name
        self._inflight = 0
        self._cv = threading.Condition()

    @property
    def inflight(self) -> int:
        """Şu anda slot tutan çağrı sayısı."""
        with self._cv:
            return self._inflight

    @property
    def available(self) -> int:
        """Boş slot sayısı."""
        with self._cv:
            return max(0, self.max_concurrent - self._inflight)

    def try_acquire(self) -> bool:
        """Beklemeden slot al. Doluysa False."""
        with self._cv:
            if self._inflight >= self.max_concurrent:
                return False
            self._inflight += 1
            return True

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """Slot al; gerekirse bekle. Timeout dolarsa False.

        ``timeout=0`` try_acquire ile aynıdır. ``timeout=None`` slot
        boşalana kadar bekler.
        """
        if timeout is not None and timeout < 0:
            timeout = 0.0
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while self._inflight >= self.max_concurrent:
                if deadline is None:
                    self._cv.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(timeout=remaining)
            self._inflight += 1
            return True

    async def acquire_async(self, timeout: Optional[float] = None) -> bool:
        """async karşılık — Condition.wait event loop'u bloklamasın diye
        kısa aralıklarla try_acquire dener.
        """
        if timeout is not None and timeout < 0:
            timeout = 0.0
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.try_acquire():
                return True
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(0.01, remaining))
            else:
                await asyncio.sleep(0.01)

    def release(self) -> None:
        """Tutulan bir slotu geri ver. Fazla release sessizce yok sayılır."""
        with self._cv:
            if self._inflight > 0:
                self._inflight -= 1
            self._cv.notify()

    def __enter__(self) -> "Bulkhead":
        if not self.acquire():
            raise BulkheadFullError(
                f"Bulkhead {self.name!r} dolu",
                bulkhead=self,
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def __repr__(self) -> str:
        return (
            f"Bulkhead(name={self.name!r}, max_concurrent={self.max_concurrent}, "
            f"inflight={self.inflight})"
        )


__all__ = ["Bulkhead", "BulkheadFullError"]
