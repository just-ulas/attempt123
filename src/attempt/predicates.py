"""Hazır retry predicate'leri — tekrar tekrar yazılan lambda'ları ortadan kaldırır."""

from __future__ import annotations

from typing import Any

from .retry import RetryPredicate, ResultPredicate


def retry_if_status(*codes: int) -> RetryPredicate:
    """``exc.status_code`` veya ``exc.status`` verilen kodlardan biriyse True.

    Örnek::

        attempt(call, retry_if=retry_if_status(429, 502, 503, 504))
    """
    wanted = set(codes)

    def _pred(exc: BaseException) -> bool:
        code = getattr(exc, "status_code", None)
        if code is None:
            code = getattr(exc, "status", None)
        return code in wanted

    return _pred


def retry_if_result_status(*codes: int) -> ResultPredicate:
    """Dönen nesnenin ``status_code`` / ``status`` alanı verilen kodlardansa True.

    Exception yükseltmeyen HTTP istemcileri için (requests, httpx Response)::

        attempt(
            lambda: session.get(url),
            retry_if_result=retry_if_result_status(429, 502, 503, 504),
            retry_after=extract_retry_after,
        )
    """
    wanted = set(codes)

    def _pred(result: Any) -> bool:
        code = getattr(result, "status_code", None)
        if code is None:
            code = getattr(result, "status", None)
        return code in wanted

    return _pred


def retry_if_message(*needles: str, case_insensitive: bool = True) -> RetryPredicate:
    """Exception mesajında verilen alt dizelerden biri varsa True."""

    def _pred(exc: BaseException) -> bool:
        text = str(exc)
        if case_insensitive:
            lowered = text.lower()
            return any(n.lower() in lowered for n in needles)
        return any(n in text for n in needles)

    return _pred


def retry_if_empty(result: Any) -> bool:
    """None veya uzunluğu 0 olan sonuçları reddet (yeniden dene).

    ``len()`` desteklemeyen değerler kabul edilir (False).
    """
    if result is None:
        return True
    try:
        return len(result) == 0
    except TypeError:
        return False


def retry_if_falsy(result: Any) -> bool:
    """Falsy sonuçları reddet (None, 0, '', [], {})."""
    return not bool(result)


def always(*_args: Any, **_kwargs: Any) -> bool:
    """Her zaman True — test veya 'hepsini dene' senaryoları için."""
    return True


def any_of(*preds: RetryPredicate) -> RetryPredicate:
    """Predicate'lerden herhangi biri True ise True (OR).

    Örnek::

        retry_if=any_of(retry_if_status(429, 503), retry_if_message("timeout"))
    """
    if not preds:
        raise ValueError("any_of en az bir predicate ister")

    def _pred(exc: BaseException) -> bool:
        return any(bool(p(exc)) for p in preds)

    return _pred


def all_of(*preds: RetryPredicate) -> RetryPredicate:
    """Tüm predicate'ler True ise True (AND)."""
    if not preds:
        raise ValueError("all_of en az bir predicate ister")

    def _pred(exc: BaseException) -> bool:
        return all(bool(p(exc)) for p in preds)

    return _pred


def not_(pred: RetryPredicate) -> RetryPredicate:
    """Predicate sonucunu tersine çevir.

    Örnek::

        retry_if=not_(retry_if_status(400, 401, 403, 404))
    """

    def _pred(exc: BaseException) -> bool:
        return not bool(pred(exc))

    return _pred


__all__ = [
    "retry_if_status",
    "retry_if_result_status",
    "retry_if_message",
    "retry_if_empty",
    "retry_if_falsy",
    "always",
    "any_of",
    "all_of",
    "not_",
    "RetryPredicate",
    "ResultPredicate",
]
