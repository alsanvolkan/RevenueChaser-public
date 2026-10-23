"""Typed completeness for one dataset — numbers first, prose derived (#124).

A free-text note cannot be checked. ``Health`` carries the counts, and every
sentence a board shows is derived from those counts, never stored beside them.

``expected is None`` means there is no definable population. Coverage is then
``None`` and renders as ``n/a``, never ``0%``. ``expected == 0`` is the same:
a percentage of nothing is not a number. ``covered > expected`` is not clamped;
the ratio stays above 1 and ``consistency_problems`` names the fact.

Pure module: no store, no network.
"""
from __future__ import annotations

import copy
import functools
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime

_MEMO = threading.local()


@contextmanager
def memo_scope():
    """Inside this scope a function wrapped by :func:`scoped` runs once per distinct call.

    One Settings collect asks the same seven store summaries twice (the row builders, then each dataset's
    ``Health``); this makes the second ask free without a cache that outlives the collect.
    """
    prev = getattr(_MEMO, "cache", None)
    _MEMO.cache = {}
    try:
        yield
    finally:
        _MEMO.cache = prev


def scoped(fn):
    """Memoise ``fn`` while a :func:`memo_scope` is open; outside one it is the plain function."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        cache = getattr(_MEMO, "cache", None)
        if cache is None:
            return fn(*args, **kwargs)
        key = (fn.__module__, fn.__qualname__, repr(args), repr(sorted(kwargs.items())))
        if key not in cache:
            cache[key] = fn(*args, **kwargs)
        return copy.deepcopy(cache[key])

    return wrapper


When = date | datetime | str | None


def _as_date(value: When) -> date | None:
    """Calendar date of a date, datetime, or ISO string; ``None`` if unreadable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip()).date()
    except ValueError:
        return None


@dataclass(frozen=True)
class Health:
    """One dataset's completeness. Prose is derived; it is not a field.

    ``source_share`` maps provider to share of rows. It is stored as given —
    shares that do not sum to 1 are not normalised — and as a copy, so a later
    change to the caller's dict does not move the record.
    """

    covered: int | None
    expected: int | None
    newest: When
    expected_as_of: When
    source_share: dict[str, float] | None = None
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source_share is not None:
            object.__setattr__(self, "source_share", dict(self.source_share))

    @property
    def coverage(self) -> float | None:
        """``covered / expected``. ``None`` when the population is not definable.

        ``expected is None`` (no population) and ``expected == 0`` both return
        ``None``, which must render as ``n/a``, never ``0%``. ``covered is None``
        is also ``None``. A ratio above 1 is kept: clamping would hide
        ``covered > expected``.
        """
        if self.covered is None or self.expected is None or self.expected == 0:
            return None
        return self.covered / self.expected

    def consistency_problems(self) -> tuple[str, ...]:
        """Facts the numbers contradict. Does not echo stored ``problems``.

        ``covered > expected`` is named with both counts, including when
        ``expected == 0`` and the ratio itself cannot be shown. A ``newest`` date after
        ``expected_as_of`` is named with both dates.
        """
        found: list[str] = []
        if self.covered is not None and self.expected is not None and self.covered > self.expected:
            found.append(f"covered {self.covered} exceeds expected {self.expected}")
        newest, ref = _as_date(self.newest), _as_date(self.expected_as_of)
        if newest is not None and ref is not None and newest > ref:
            found.append(f"newest {newest.isoformat()} is after expected_as_of {ref.isoformat()}")
        return tuple(found)


def coverage_text(h: Health) -> str:
    """``n/a`` when ``coverage`` is ``None``, else ``65.0% (4,999 of 7,689)``."""
    ratio = h.coverage
    if ratio is None:
        return "n/a"
    return f"{ratio * 100:.1f}% ({h.covered:,} of {h.expected:,})"


def freshness_text(h: Health, today: When) -> str:
    """``unknown`` when ``newest`` is missing, else ``<N> d old``.

    ``N`` is whole calendar days from ``newest`` to ``expected_as_of`` when that
    is set, otherwise to ``today``. A date, a datetime and an ISO string of the
    same calendar day agree. An unreadable supplied as-of does not fall back to
    ``today``.
    """
    newest = _as_date(h.newest)
    if newest is None:
        return "unknown"
    if h.expected_as_of is not None:
        ref = _as_date(h.expected_as_of)
        if ref is None:
            return "unknown"
    else:
        ref = _as_date(today)
        if ref is None:
            return "unknown"
    age = (ref - newest).days
    if age < 0:
        return f"newest date is {-age} d after the reference date"
    return f"{age} d old"
