"""
attempt123 – basit, bağımlılıksız retry / attempt yardımcı kütüphanesi.

Kullanım:
    from attempt import retry, attempt

    @retry(max_attempts=5)
    def fragile():
        ...

    result = attempt(lambda: do_something(), max_attempts=3)
"""

from .retry import retry, attempt

__all__ = ["retry", "attempt"]
__version__ = "0.1.0"
