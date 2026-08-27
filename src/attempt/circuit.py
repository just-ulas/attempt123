"""Basit, bağımlılıksız circuit breaker.

Kapalı → ardışık hatalar eşiği → açık (çağrıları kes) → recovery_timeout
sonra yarı-açık (probe) → başarı eşiği → tekrar kapalı.

``attempt(..., circuit=breaker)`` ile retry döngüsüne bağlanır; açık devre
``CircuitOpenError`` yükseltir (``retry_after`` alanı recovery süresini taşır).
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Optional


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Devre açıkken çağrı kısa devre edildi.

    Attributes:
        retry_after: Half-open'a kalan saniye (yoksa None).
        breaker: İlgili CircuitBreaker (varsa).
    """

    def __init__(
        self,
        message: str = "circuit breaker açık",
        *,
        retry_after: Optional[float] = None,
        breaker: Optional["CircuitBreaker"] = None,
    ) -> None:
        self.retry_after = retry_after
        self.breaker = breaker
        super().__init__(message)


class CircuitBreaker:
    """Eşik tabanlı circuit breaker.

    Args:
        failure_threshold: Açmak için gereken ardışık hata sayısı.
        recovery_timeout: Açık kaldıktan sonra half-open'a geçiş (saniye).
        success_threshold: Half-open'da kapanmak için gereken ardışık başarı.
        name: Log / hata mesajı için etiket.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        success_threshold: int = 1,
        name: str = "default",
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold en az 1 olmalıdır")
        if recovery_timeout < 0:
            raise ValueError("recovery_timeout negatif olamaz")
        if success_threshold < 1:
            raise ValueError("success_threshold en az 1 olmalıdır")
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        self.name = name
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._half_open_successes = 0
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        self._maybe_transition_to_half_open()
        return self._state.value

    @property
    def failure_count(self) -> int:
        return self._failures

    def seconds_until_retry(self) -> float:
        """Açıkken half-open'a kalan süre; aksi halde 0."""
        self._maybe_transition_to_half_open()
        if self._state != CircuitState.OPEN:
            return 0.0
        remaining = self.recovery_timeout - (time.monotonic() - self._opened_at)
        return max(0.0, remaining)

    def allow(self) -> bool:
        """Bu anda bir çağrıya izin var mı? Açıkken False."""
        self._maybe_transition_to_half_open()
        return self._state != CircuitState.OPEN

    def record_success(self) -> None:
        self._maybe_transition_to_half_open()
        if self._state == CircuitState.HALF_OPEN:
            self._half_open_successes += 1
            if self._half_open_successes >= self.success_threshold:
                self._close()
            return
        self._failures = 0

    def record_failure(self) -> None:
        self._maybe_transition_to_half_open()
        if self._state == CircuitState.HALF_OPEN:
            self._trip()
            return
        if self._state == CircuitState.OPEN:
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._trip()

    def reset(self) -> None:
        """Manuel olarak kapalı duruma al."""
        self._close()
        self._opened_at = 0.0

    def _maybe_transition_to_half_open(self) -> None:
        if self._state != CircuitState.OPEN:
            return
        if (time.monotonic() - self._opened_at) >= self.recovery_timeout:
            self._state = CircuitState.HALF_OPEN
            self._half_open_successes = 0

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._half_open_successes = 0

    def _close(self) -> None:
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._half_open_successes = 0

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(name={self.name!r}, state={self.state}, "
            f"failures={self._failures})"
        )


__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
]
