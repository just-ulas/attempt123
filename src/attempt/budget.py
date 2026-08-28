"""Pencere tabanlı retry bütçesi — retry storm'u kaynak seviyesinde keser.

RateLimiter her çağrıyı (ilk deneme dahil) kısıtlar. RetryBudget yalnızca
yeniden denemelere izin verir: bir pencerede en fazla ``retry_ratio`` kadar
retry + düşük trafikte ``min_retries`` tabanı.

Paylaşılan bir bütçe, aynı bağımlılığa giden tüm ``attempt`` çağrılarını
korur. Bütçe dolunca mevcut hata / reddedilmiş sonuç hemen yükseltilir;
ekstra bekleme ve ekstra yük olmaz.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Tuple


class RetryBudget:
    """Kayar pencereli, thread-safe retry bütçesi.

    Args:
        window: İstatistik penceresi (saniye).
        retry_ratio: Penceredeki istek başına izin verilen retry oranı.
            0.2 → 100 istek için en fazla 20 retry.
        min_retries: Düşük trafikte bile pencerede garanti edilen retry sayısı.
        name: Log / hata mesajı için etiket.
    """

    def __init__(
        self,
        window: float = 10.0,
        retry_ratio: float = 0.2,
        min_retries: int = 10,
        name: str = "default",
    ) -> None:
        if window <= 0:
            raise ValueError("window pozitif olmalıdır")
        if retry_ratio < 0:
            raise ValueError("retry_ratio negatif olamaz")
        if min_retries < 0:
            raise ValueError("min_retries negatif olamaz")
        self.window = float(window)
        self.retry_ratio = float(retry_ratio)
        self.min_retries = int(min_retries)
        self.name = name
        self._events: Deque[Tuple[float, str]] = deque()
        self._lock = threading.Lock()

    def _prune_unlocked(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _counts_unlocked(self) -> Tuple[int, int]:
        requests = 0
        retries = 0
        for _, kind in self._events:
            if kind == "request":
                requests += 1
            else:
                retries += 1
        return requests, retries

    def record_request(self) -> None:
        """Bir üst seviye çağrıyı (ilk deneme) kaydet."""
        with self._lock:
            now = time.monotonic()
            self._prune_unlocked(now)
            self._events.append((now, "request"))

    def try_retry(self) -> bool:
        """Bir yeniden denemeye izin varsa kaydet ve True dön."""
        with self._lock:
            now = time.monotonic()
            self._prune_unlocked(now)
            requests, retries = self._counts_unlocked()
            if retries < self.min_retries:
                self._events.append((now, "retry"))
                return True
            allowed = requests * self.retry_ratio
            if retries < allowed:
                self._events.append((now, "retry"))
                return True
            return False

    def remaining_retries(self) -> int:
        """Pencerede hâlâ kullanılabilecek tahmini retry hakkı."""
        with self._lock:
            now = time.monotonic()
            self._prune_unlocked(now)
            requests, retries = self._counts_unlocked()
            allowed = max(self.min_retries, int(requests * self.retry_ratio))
            return max(0, allowed - retries)

    @property
    def request_count(self) -> int:
        with self._lock:
            self._prune_unlocked(time.monotonic())
            return self._counts_unlocked()[0]

    @property
    def retry_count(self) -> int:
        with self._lock:
            self._prune_unlocked(time.monotonic())
            return self._counts_unlocked()[1]

    def reset(self) -> None:
        """Pencereyi boşalt."""
        with self._lock:
            self._events.clear()

    def __repr__(self) -> str:
        return (
            f"RetryBudget(name={self.name!r}, window={self.window}, "
            f"retry_ratio={self.retry_ratio}, min_retries={self.min_retries}, "
            f"requests={self.request_count}, retries={self.retry_count})"
        )


__all__ = ["RetryBudget"]
