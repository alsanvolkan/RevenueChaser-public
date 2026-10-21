"""One cached-info fallback for the boards that refetch provider data (Swing #259, AG #260).

A refetch that fails or answers nothing must never lose the last good value: ``refetch_or_keep`` returns the
stored value marked ``_stale`` (a copy, never written back), or ``None`` when nothing is stored. The caller
writes the store only on a ``"fresh"`` answer. Pure: no store, no network.
"""
from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)


def has_fields(info) -> bool:
    """True when ``info`` carries at least one real value (not None, blank or empty).

    ``trailingPegRatio`` does not count: yfinance adds it to the stub it returns on a hidden HTTP error,
    and for a liquid name it can carry a real number (yfinance quote.py).
    """
    return isinstance(info, dict) and any(v not in (None, "", [], {})
                                          for k, v in info.items() if k != "trailingPegRatio")


def refetch_or_keep(stored: dict | None, fetch: Callable[[], dict],
                    is_answer: Callable[[dict], bool], *,
                    catch: tuple[type[BaseException], ...] = (Exception,),
                    label: str = "", log_level: int = logging.DEBUG) -> tuple[dict | None, str]:
    """Refetch once; keep ``stored`` (marked ``_stale``) when the refetch has no answer.

    ``"fresh"``: ``is_answer(fresh)`` — return ``fresh``; the caller may save it.
    ``"stale"``: no answer and ``stored`` is a dict — return ``dict(stored, _stale=True)`` (a copy, the store
    is untouched).
    ``"none"``:  no answer and nothing stored — return ``None``.
    A ``fetch`` that raises one of ``catch`` counts as no answer (logged at ``log_level`` with ``label``); any
    other exception propagates, so a caller that narrows ``catch`` keeps its own error accounting.
    """
    try:
        fresh = fetch()
    except catch as exc:  # one provider failure must not lose the cached value
        logger.log(log_level, "refetch failed%s: %s", f" for {label}" if label else "", exc)
        fresh = None
    if is_answer(fresh):
        return fresh, "fresh"
    if isinstance(stored, dict):
        return dict(stored, _stale=True), "stale"
    return None, "none"
