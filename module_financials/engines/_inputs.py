"""Missing-data helpers shared by the Financials engines (#173).

Convention (same as #159): a missing input is unknown, never 0, and a zero the user typed or the
statements report is a zero. ``x or DEFAULT`` cannot tell the two apart, so the engines go through
these instead.
"""
from __future__ import annotations

from ..config import to_float


# A missing D&A line is modelled as 0 (EBIT = EBITDA, no add-back); both engines say so in these words (FIN-2).
DNA_ASSUMED_ZERO = "D&A: assumed 0 (not reported)"


def pick(assumptions: dict, key: str, default):
    """The user's value for ``key`` when given (0 included), else ``default``."""
    v = to_float(assumptions.get(key))
    return default if v is None else v


def net_debt(debt: float | None, cash: float | None) -> float | None:
    """Debt minus cash, or None when either is unknown."""
    return None if debt is None or cash is None else debt - cash


def first_known(*values):
    """The first value that is not None (a 0 counts)."""
    for v in values:
        if v is not None:
            return v
    return None
