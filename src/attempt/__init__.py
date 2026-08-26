"""
attempt123 – basit, bağımlılıksız retry / attempt yardımcı kütüphanesi.

Kullanım:
    from attempt import retry, attempt, async_retry, async_attempt, RetryError

    @retry(max_attempts=5)
    def fragile():
        ...

    @async_retry(max_attempts=5)
    async def fragile_async():
        ...

    result = attempt(lambda: do_something(), max_attempts=3)
    result = await async_attempt(lambda: do_something_async(), max_attempts=3)
"""

from .retry import RetryError, async_attempt, async_retry, attempt, retry

__all__ = [
    "retry",
    "attempt",
    "async_retry",
    "async_attempt",
    "RetryError",
]
__version__ = "0.2.0"
