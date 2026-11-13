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
2. **No interest expense above the floor.** ``interest_expense`` absent, negative, or at or below
   ``INTEREST_FLOOR_USD`` once annualised, and the same for the two presence-only labels of ``INTEREST_FIELDS``.
   A filer that pays interest owes something, whatever it tagged -- but a few thousand dollars is not a balance
   sheet, so avo put a floor under it on 13 Nov 2026 and the value was measured (see ``INTEREST_FLOOR_USD``).

**The presence-only fields.** The measurement of 13 Nov 2026 on the real store
(claude-coordinator ``notes/revenuechaser/notes/2026-11-13_1515_737-debt-free-inference-measurement.md``) read
every name this rule would call debt-free against its raw companyfacts and found **6 of 307** that do tag a
borrowing, under a concept neither list carries -- TRN and CENX owed 5,442 m and 548 m and had paid 272.8 m and
36.6 m of interest, and the rule read each of them as net cash. None of the six was ever rendered: a name reaches
a card only by passing a gate, and only CP passes one (Momentum), where it has no EBITDA and its leverage row
reads unknown either way. The defect was real; it was not visible.
``PRESENCE_ONLY_FIELDS`` closes that: the four concepts are read into the record
(``core/providers/sec_edgar.py``) and consulted **here only**. They enter no total, in this layer or any other --
``DebtInstrumentCarryingAmount`` in particular is one instrument's carrying amount and would make a total worse
than the NULL it replaces -- but a filer that tags one of them is not a filer that tagged nothing, so the name
leaves the inference and stays unknown, which is the honest answer.

**The cost, measured.** ORKA tags ``DebtLongtermAndShorttermCombinedAmount`` at **zero**: rule 1 counts a tagged
zero as a tag, so it goes back to unknown although the zero is the filer saying it owes nothing. That is the rule
as written and as #264 / #825 read a zero everywhere else -- 1 name in 2,148, a reading lost rather than a wrong
one printed.

**What is left.** One name of the 289, PCAR, tags no debt at all and reports no interest expense; only
``InterestPaid`` (692.8 m) betrays it, and 49 of the 307 paid some interest (MNST 5.3 m, CPRT 1.9 m), so a rule on
interest *paid* needs a threshold, a measured reason and avo's decision. It is deliberately not invented here, and
PCAR passes no gate, so its reading reaches no card.

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
                        "debt_instrument_carrying_amount",
                        # Three current-debt spellings ``facts.FALLBACK_CURRENT_DEBT`` does not carry. A current
                        # line alone is unreadable as a size (#239), so these take a name out of the inference the
                        # same way, where no floor can reach it: TR, CUE and DUOT leaked through this hole.
                        )  # MUTANT 3

#: Interest labels the ``interest_expense`` ladder does not carry, read for the same reason and in the same way.
#: They are not added to ``interest_expense`` itself: that field feeds the interest-cover and tax-rate engines
#: (#645) and widening its ladder would move figures those boards already serve.
INTEREST_FIELDS = ("interest_expense", "interest_expense_borrowings", "interest_expense_other")

#: avo, 13 Nov 2026 ("taban koy", and on the value: "ben nereden bileyim mantikli bir sey secelim"): an interest
#: expense at or below this, **annualised**, is not evidence of debt. Measured the same day on the real store
#: (claude-coordinator ``notes/revenuechaser/notes/2026-11-13_1752_737-interest-floor-measurement.md``), over the
#: 126 names that tag no debt at all and are kept out by interest alone:
#:
#: * **The distribution gives no anchor.** Annualised interest / revenue runs from 0.0005 % to 659 %, median
#:   0.12 %, and the widest step at the low end is x2.1 with 3 names below it. There is no cliff to put the floor
#:   on, so it is chosen by headroom instead: the sweep is clean at 100 k and 150 k and the first real borrower
#:   (CUE, ``OtherLoansPayableCurrent`` 0.7 m) leaks at 200 k. 100 k is 2x headroom.
#: * **Absolute, not a ratio, because of the 18.** 18 of the 126 report no revenue at all in the period that
#:   blocks them (pre-revenue biotech: ACHV, CAPR, MAZE, RVMD, VKTX and the rest) -- exactly the population whose
#:   interest line is a few thousand dollars. A ratio floor can never judge them; an absolute one judges them on
#:   the same terms as everyone else.
#: * **What it costs.** 19 names come back (UMAC 1,472, ASST 4,656, MF 697, WEYS, WFCF, STOK, EDU, WMK and the
#:   rest), 4 of them newly pass Quality, and **none of the 19 carries a debt balance this module cannot read.**
#:   Against it: QUBT pays 65,000 on 682,000 of revenue -- 9.5 %, which a ratio floor would catch and this one
#:   does not. It tags no debt and has no unread balance, so it is a reading, not a wrong one.
INTEREST_FLOOR_USD = 100_000.0

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


def no_interest(rec: dict | None, *, quarterly: bool = False) -> bool:
    """True when no ``INTEREST_FIELDS`` label reports interest above ``INTEREST_FLOOR_USD``, annualised.

    ``quarterly`` says the record covers three months, so its figure is multiplied by four before the comparison:
    the floor is one number for the whole board and a quarter's line is not a year's. A negative or absent figure
    passes as it always did.
    """
    if not rec:
        return False
    scale = 1  # MUTANT 1
    return all((ie := _float(rec.get(f))) is None or ie * scale < INTEREST_FLOOR_USD for f in INTEREST_FIELDS)  # MUTANT 2


def debt_free(rec: dict | None, *, quarterly: bool = False) -> bool:
    """Both conditions on one record (the module docstring). ``quarterly`` annualises the interest figure."""
    return tags_no_debt(rec) and no_interest(rec, quarterly=quarterly)
