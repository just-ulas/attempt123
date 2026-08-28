"""
attempt123 – basit, bağımlılıksız retry / attempt yardımcı kütüphanesi.

Kullanım:
    from attempt import retry, attempt, async_retry, async_attempt, RetryError
    from attempt import extract_retry_after, retry_if_status, retry_if_empty
    from attempt import retry_if_result_status, any_of, all_of, not_
    from attempt import CircuitBreaker, CircuitOpenError
    from attempt import RateLimiter, RateLimitError
    from attempt import RetryBudget, GiveUpContext
    from attempt import Bulkhead, BulkheadFullError

    limiter = RateLimiter(rate=5, burst=10, name="payments")
    breaker = CircuitBreaker(
        failure_threshold=5,
        recovery_timeout=15.0,
        max_half_open=1,
        name="payments",
    )
    budget = RetryBudget(window=10.0, retry_ratio=0.2, min_retries=10, name="payments")
    gate = Bulkhead(max_concurrent=8, name="payments")

    @retry(
        max_attempts=5,
        retry_after=extract_retry_after,
        circuit=breaker,
        limiter=limiter,
        budget=budget,
        bulkhead=gate,
        attempt_timeout=2.0,
        fallback=lambda ctx: cached_quote(ctx),
    )
    def fragile():
        ...

    result = attempt(
        lambda: session.get(url),
        retry_if_result=retry_if_result_status(429, 503),
        retry_after=extract_retry_after,
        circuit=breaker,
        limiter=limiter,
        budget=budget,
        bulkhead=gate,
        attempt_timeout=1.5,
        fallback=lambda: default_payload(),
        reraise_as_retry_error=True,
    )
"""

from .budget import RetryBudget
from .bulkhead import Bulkhead, BulkheadFullError
from .circuit import CircuitBreaker, CircuitOpenError, CircuitState
from .limiter import RateLimitError, RateLimiter
from .predicates import (
    all_of,
    always,
    any_of,
    not_,
    retry_if_empty,
    retry_if_falsy,
    retry_if_message,
    retry_if_result_status,
    retry_if_status,
)
from .retry import (
    GiveUpContext,
    RetryAttempt,
    RetryError,
    async_attempt,
    async_retry,
    attempt,
    extract_retry_after,
    retry,
)

__all__ = [
    "retry",
    "attempt",
    "async_retry",
    "async_attempt",
    "RetryError",
    "RetryAttempt",
    "GiveUpContext",
    "extract_retry_after",
    "retry_if_status",
    "retry_if_result_status",
    "retry_if_message",
    "retry_if_empty",
    "retry_if_falsy",
    "always",
    "any_of",
    "all_of",
    "not_",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "RateLimiter",
    "RateLimitError",
    "RetryBudget",
    "Bulkhead",
    "BulkheadFullError",
]
__version__ = "0.15.0"
