"""Missing-data helpers shared by the Financials engines (#173).

Convention (same as #159): a missing input is unknown, never 0, and a zero the user typed or the
statements report is a zero. ``x or DEFAULT`` cannot tell the two apart, so the engines go through
these instead.
"""
from __future__ import annotations

from ..config import DEFAULT_TAX_RATE, sector_defaults, to_float


# A measured tax rate is only believable inside this band: a reported "Tax Provision" can be negative (a tax
# benefit) or larger than pretax income (a one-off, a valuation-allowance release), and such a ratio is not the
# firm's tax rate. Outside the band the rate is unknown, so the sector default stands and the report says so.
MEASURED_TAX_RATE_BAND = (0.0, 0.60)


# A missing D&A line is modelled as 0 (EBIT = EBITDA, no add-back); both engines say so in these words (FIN-2).
DNA_ASSUMED_ZERO = "D&A: assumed 0 (not reported)"


def pick(assumptions: dict, key: str, default):
    """The user's value for ``key`` when given (0 included), else ``default``."""
    v = to_float(assumptions.get(key))
    return default if v is None else v


def default_tax_rate(sector: str | None) -> float:
    """The sector's own rate when its band carries one, else the single default.

    ``SECTOR_DEFAULTS`` is READ here, never written: a sector band without a ``tax_rate`` key takes
    ``DEFAULT_TAX_RATE``.
    """
    rate = to_float(sector_defaults(sector).get("tax_rate"))
    return DEFAULT_TAX_RATE if rate is None else rate


def statement_tax_rate(statement: dict) -> float | None:
    """``tax_provision / pretax_income`` from the statements, ``None`` when it is not a believable rate.

    Unknown, not a rate: either line missing; a negative or zero pretax income (the ratio flips or explodes);
    and a ratio outside ``MEASURED_TAX_RATE_BAND`` — a negative provision (a tax benefit) or a provision larger
    than pretax income. The caller then falls back to the sector default.
    """
    pretax = to_float((statement or {}).get("pretax_income"))
    provision = to_float((statement or {}).get("tax_provision"))
    if pretax is None or provision is None or pretax <= 0:
        return None
    rate = provision / pretax
    low, high = MEASURED_TAX_RATE_BAND
    return None if rate < low or rate > high else rate


def effective_tax_rate(assumptions: dict, statement: dict, sector: str | None) -> tuple[float, str]:
    """``(rate, source)``: the user's (0 included), then the statements, then the sector default.

    The statement rung is a stored ``tax_rate`` when the payload has one, else ``tax_provision / pretax_income``
    inside ``MEASURED_TAX_RATE_BAND`` (FIN-3); an implausible ratio is unknown, so it takes the sector default and
    is labelled ``default``. ``source`` is ``assumption`` / ``statements`` / ``default``; the report labels the
    default (FIN-9 B). A rate the user typed is their number, band or no band — a typed 0 is a 0.
    """
    given = to_float((assumptions or {}).get("tax_rate"))
    if given is not None:
        return given, "assumption"
    stored = to_float((statement or {}).get("tax_rate"))
    if stored is not None:
        return stored, "statements"
    measured = statement_tax_rate(statement)
    if measured is not None:
        return measured, "statements"
    return default_tax_rate(sector), "default"


def net_debt(debt: float | None, cash: float | None) -> float | None:
    """Debt minus cash, or None when either is unknown."""
    return None if debt is None or cash is None else debt - cash


def first_known(*values):
    """The first value that is not None (a 0 counts)."""
    for v in values:
        if v is not None:
            return v
    return None
