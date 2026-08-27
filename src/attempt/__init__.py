"""
attempt123 – basit, bağımlılıksız retry / attempt yardımcı kütüphanesi.

Kullanım:
    from attempt import retry, attempt, async_retry, async_attempt, RetryError
    from attempt import extract_retry_after, retry_if_status, retry_if_empty

    @retry(max_attempts=5, retry_after=extract_retry_after)
    def fragile():
        ...

    result = attempt(
        lambda: fetch_list(),
        retry_if_result=retry_if_empty,
        reraise_as_retry_error=True,
    )
"""

from .predicates import retry_if_empty, retry_if_falsy, retry_if_message, retry_if_status
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
    "retry_if_message",
    "retry_if_empty",
    "retry_if_falsy",
]
__version__ = "0.6.0"
