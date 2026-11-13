"""Debt-free inference for the Fundamentals board (#737).

avo's decision, 13 Nov 2026, "A: Borçsuz say": a company that tags **no** debt line at all on its balance sheet
and reports no positive interest expense is counted debt-free, so the leverage rule and ROIC become known instead
of staying unknown for the rest of the board's life.

The inference is **board-side only**. ``rcdata/build/fundamentals/facts.py`` still stores ``total_debt`` as NULL
for these filers: NULL is what the facts say, and a stored 0 would be a claim the XBRL does not make. The board is
where a missing total is turned into a reading, and the reading carries its own basis
(``INFERRED_NONE``) so a card can never show it as a figure the filer filed.

Two conditions, both required:

1. **No debt tag at all.** None of ``facts.DEBT_LINES`` (the lines of #239), none of
   ``facts.FALLBACK_DEBT_LINES`` (the balances #825 added) and none of ``PRESENCE_ONLY_FIELDS`` is tagged on the
   record. A filer that tags only a current line is therefore **out of scope** and stays unknown: it has debt, and
   #239's rule that a current line alone is not the whole means the size of it is not readable. This rule does not
   change that.
2. **No positive interest expense.** ``interest_expense`` absent or ``<= 0``, and the same for the two
   presence-only labels of ``INTEREST_FIELDS``. A filer that pays interest owes something, whatever it tagged, so
   a positive figure takes the name out of scope.

**The presence-only fields.** The measurement of 13 Nov 2026 on the real store
(claude-coordinator ``notes/revenuechaser/notes/2026-11-13_1515_737-debt-free-inference-measurement.md``) read
every name this rule would call debt-free against its raw companyfacts and found **6 of 307** that do tag a
borrowing, under a concept neither list carries. Two of them, TRN and CENX, reached a card as "net cash" with a
negative net debt / EBITDA while the filer owed 5,442 m and 548 m and had paid 272.8 m and 36.6 m of interest.
``PRESENCE_ONLY_FIELDS`` closes that: the four concepts are read into the record
(``core/providers/sec_edgar.py``) and consulted **here only**. They enter no total, in this layer or any other --
``DebtInstrumentCarryingAmount`` in particular is one instrument's carrying amount and would make a total worse
than the NULL it replaces -- but a filer that tags one of them is not a filer that tagged nothing, so the name
leaves the inference and stays unknown, which is the honest answer.

**The cost, measured.** ORKA tags ``DebtLongtermAndShorttermCombinedAmount`` at **zero**: rule 1 counts a tagged
zero as a tag, so it goes back to unknown although the zero is the filer saying it owes nothing. That is the rule
as written and as #264 / #825 read a zero everywhere else -- 1 name in 2,148, a reading lost rather than a wrong
one printed.

**What is left.** One name of the 300, PCAR, tags no debt at all and reports no interest expense; only
``InterestPaid`` (692.8 m) betrays it, and 49 of the 307 paid some interest (MNST 5.3 m, CPRT 1.9 m), so a rule on
interest *paid* needs a threshold, a measured reason and avo's decision. It is deliberately not invented here, and
PCAR's reading does not reach a card (no EBITDA, so the leverage rule stays unknown either way).

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


#: Fields that say "this filer tagged a borrowing" and nothing more. They are summed nowhere -- not in
#: ``facts._debt_lines``, not in ``sec_edgar.extract_annual`` -- and exist so ``tags_no_debt`` can tell a filer
#: that tagged nothing from one that tagged debt this project does not add up (the module docstring).
PRESENCE_ONLY_FIELDS = ("notes_and_loans_payable", "debt_combined_total", "unsecured_debt",
                        "debt_instrument_carrying_amount")

#: Interest labels the ``interest_expense`` ladder does not carry, read for the same reason and in the same way.
#: They are not added to ``interest_expense`` itself: that field feeds the interest-cover and tax-rate engines
#: (#645) and widening its ladder would move figures those boards already serve.
INTEREST_FIELDS = ("interest_expense", "interest_expense_borrowings", "interest_expense_other")

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
    """True when the record tags none of ``tag_fields()`` and none of ``PRESENCE_ONLY_FIELDS``.

    A tagged zero counts as a tag: the filer stated it.
    """
    if not rec:
        return False
    return all(_float(rec.get(f)) is None for f in tag_fields() + PRESENCE_ONLY_FIELDS)


def no_interest(rec: dict | None) -> bool:
    """True when the record reports no interest expense under any of ``INTEREST_FIELDS``, or none that is positive."""
    if not rec:
        return False
    return all((ie := _float(rec.get(f))) is None or ie <= 0 for f in INTEREST_FIELDS)


def debt_free(rec: dict | None) -> bool:
    """Both conditions on one record (the module docstring)."""
    return tags_no_debt(rec) and no_interest(rec)
