"""
attempt123 – basit, bağımlılıksız retry / attempt yardımcı kütüphanesi.

Kullanım:
    from attempt import retry, attempt, async_retry, async_attempt, RetryError
    from attempt import extract_retry_after, retry_if_status, retry_if_empty
    from attempt import retry_if_result_status, any_of, all_of, not_

    @retry(max_attempts=5, retry_after=extract_retry_after)
    def fragile():
        ...

    result = attempt(
        lambda: session.get(url),
        retry_if_result=retry_if_result_status(429, 503),
        retry_after=extract_retry_after,
        reraise_as_retry_error=True,
    )
"""

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
]
__version__ = "0.8.0"
