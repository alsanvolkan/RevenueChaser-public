"""Debt-free inference for the Fundamentals board (#737).

avo's decision, 13 Nov 2026, "A: Borçsuz say": a company that tags **no** debt line at all on its balance sheet
and reports no positive interest expense is counted debt-free, so the leverage rule and ROIC become known instead
of staying unknown for the rest of the board's life.

The inference is **board-side only**. ``rcdata/build/fundamentals/facts.py`` still stores ``total_debt`` as NULL
for these filers: NULL is what the facts say, and a stored 0 would be a claim the XBRL does not make. The board is
where a missing total is turned into a reading, and the reading carries its own basis
(``INFERRED_NONE``) so a card can never show it as a figure the filer filed.

Two conditions, both required:

1. **No debt tag at all.** None of ``facts.DEBT_LINES`` (the lines of #239) and none of
   ``facts.FALLBACK_DEBT_LINES`` (the balances #825 added) is tagged on the record. A filer that tags only a
   current line is therefore **out of scope** and stays unknown: it has debt, and #239's rule that a current line
   alone is not the whole means the size of it is not readable. This rule does not change that.
2. **No positive interest expense.** ``interest_expense`` absent or ``<= 0``. A filer that pays interest owes
   something, whatever it tagged, so a positive figure takes the name out of scope.

**What this cannot see.** ``interest_expense`` reads ``InterestExpense`` and its three neighbours
(``core/providers/sec_edgar.py``). A bank or an insurer writes interest under other labels (deposit interest,
policyholder credits), so one of them could come through here with neither a debt tag nor an interest figure. The
Fundamentals board never scores them: ``config.EXCLUDED_SECTORS`` drops Financial Services and Real Estate before
EDGAR is read (``pipeline._score_row``). That exclusion, not this module, is what keeps them out.
"""
from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def tag_fields() -> tuple[str, ...]:
    """Every debt line the store can carry, each name once, read from the facts layer so there is one list.

    The import is **inside** the function on purpose: ``rcdata.build`` imports every dataset module as its package
    body, so importing it at the top of this file would put the whole builder on the board's import path.
    ``module_fundamentals.events`` defers its rcdata imports for the same reason.
    """
    from RevenueChaser.rcdata.build.fundamentals.facts import DEBT_LINES, FALLBACK_DEBT_LINES
    return tuple(dict.fromkeys(DEBT_LINES + FALLBACK_DEBT_LINES))

FILED = "filed"                  # ``total_debt`` came from the facts (a sum of tagged lines), zero included
INFERRED_NONE = "inferred_none"  # no total in the facts; this module read it as 0


def _float(v):
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def tags_no_debt(rec: dict | None) -> bool:
    """True when the record tags none of ``tag_fields()``. A tagged zero counts as a tag: the filer stated it."""
    if not rec:
        return False
    return all(_float(rec.get(f)) is None for f in tag_fields())


def no_interest(rec: dict | None) -> bool:
    """True when the record reports no interest expense, or reports one that is not positive."""
    if not rec:
        return False
    ie = _float(rec.get("interest_expense"))
    return ie is None or ie <= 0


def debt_free(rec: dict | None) -> bool:
    """Both conditions on one record (the module docstring)."""
    return tags_no_debt(rec) and no_interest(rec)
