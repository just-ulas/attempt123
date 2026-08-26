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

    # Sonuç tabanlı yeniden deneme:
    result = attempt(
        lambda: fetch_list(),
        retry_if_result=lambda r: r is None or len(r) == 0,
    )

    # AWS full jitter (dağıtık sistemler için):
    @retry(jitter="full", max_attempts=5)
    def call_remote():
        ...
"""

from .retry import RetryError, async_attempt, async_retry, attempt, retry

__all__ = [
    "retry",
    "attempt",
    "async_retry",
    "async_attempt",
    "RetryError",
]
__version__ = "0.5.0"
