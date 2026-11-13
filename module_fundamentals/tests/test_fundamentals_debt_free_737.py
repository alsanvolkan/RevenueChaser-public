"""#737, avo 13 Nov 2026 ("A: Borçsuz say"): a filer that tags no debt line at all and reports no positive
interest expense is counted debt-free, so its total debt reads 0 instead of unknown.

Before this, such a filer could never clear Quality: the leverage rule and ROIC both need a total debt, the facts
give none (``rcdata/build/fundamentals/facts.py``: no tagged line, no total -- a NULL, never a zero), and a gate
passes only when every rule is known. PANW, ZS, CPRT and MNST are in that group on the stored companyfacts.

Two conditions, both of them (``module_fundamentals/debt.py``). The record must tag **none** of the debt lines
(#239's and #825's fallback balances alike), so a filer with only a current line is out of scope and stays
unknown; and its interest expense must be absent or not positive. The reading is marked ``inferred_none`` and the
leverage rule says so on its label, so it can never be read as a zero the filer filed.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_fundamentals import config, debt, pipeline
from RevenueChaser.module_fundamentals.screeners import INFERRED_DEBT_NOTE, evaluate_quality

# A profitable, cash-rich software filer with nothing borrowed: the shape of PANW's and ZS's balance sheet in the
# stored facts -- cash and equity tagged, not one of the 30 debt lines, and no interest expense line at all.
BASE = {"revenue": 1000.0, "gross_profit": 750.0, "ebit": 200.0, "depreciation": 50.0,
        "cash": 600.0, "total_equity": 900.0, "net_income": 150.0}


def _year(**over) -> list[dict]:
    return [{**BASE, **over}]


def _leverage(q: dict) -> dict:
    gate = evaluate_quality({"net_debt_ebitda": q["net_debt_ebitda"], "net_cash": q.get("net_cash"),
                             "debt_basis": q.get("debt_basis")})
    return next(r for r in gate["rules"] if r["id"] == "net_debt_ebitda")


def test_no_debt_tag_and_no_interest_is_counted_debt_free():
    q = pipeline._quality_metrics(_year())
    assert q["debt_basis"] == debt.INFERRED_NONE
    # Total debt read as 0: net debt is -600, EBITDA 250, and ROIC's invested capital is equity minus cash.
    assert q["net_debt_ebitda"] == pytest.approx((0.0 - 600.0) / 250.0)
    assert q["net_cash"] is True
    assert q["roic"] == pytest.approx(200.0 * (1 - config.TAX_RATE) / (0.0 + 900.0 - 600.0))


def test_the_leverage_rule_becomes_known_and_says_the_zero_was_inferred():
    rule = _leverage(pipeline._quality_metrics(_year()))
    assert rule["passed"] is True
    assert INFERRED_DEBT_NOTE in rule["label"]


def test_a_zero_interest_expense_is_still_debt_free():
    q = pipeline._quality_metrics(_year(interest_expense=0.0))
    assert q["debt_basis"] == debt.INFERRED_NONE


def test_a_positive_interest_expense_takes_the_filer_out_of_scope():
    """It pays interest, so it owes something whatever it tagged: the total stays unknown."""
    q = pipeline._quality_metrics(_year(interest_expense=12.0))
    assert q["debt_basis"] is None and q["net_debt_ebitda"] is None and q["net_cash"] is None
    assert _leverage(q)["passed"] is None


@pytest.mark.parametrize("field", ["lt_debt_current", "debt_current", "short_term_borrowings",
                                   "convertible_debt_current", "line_of_credit_current", "loans_payable_current",
                                   "senior_notes_current", "notes_payable_current", "short_term_bank_loans"])
def test_a_current_line_alone_stays_unknown(field):
    """#239's rule is untouched: a current line alone is not the whole balance, so the size is unreadable. It is
    still a debt line, so the filer is not debt-free either -- it falls between the two and stays unknown."""
    q = pipeline._quality_metrics(_year(**{field: 40.0}))
    assert q["debt_basis"] is None and q["net_debt_ebitda"] is None


@pytest.mark.parametrize("field", ["lt_debt", "lt_debt_total", "senior_notes", "convertible_debt_noncurrent",
                                   "bank_loans_noncurrent", "other_lt_debt", "line_of_credit"])
def test_a_tagged_zero_debt_line_is_not_an_inference(field):
    """A tag carried at zero is the filer's own statement, and the facts layer is the one that reads it (#264,
    #825). If a total still did not come through, this rule must not invent one: the tag exists."""
    q = pipeline._quality_metrics(_year(**{field: 0.0}))
    assert q["debt_basis"] is None and q["net_debt_ebitda"] is None


def test_a_filed_total_is_untouched_and_carries_no_note():
    q = pipeline._quality_metrics(_year(total_debt=500.0))
    assert q["debt_basis"] == debt.FILED
    assert q["net_debt_ebitda"] == pytest.approx((500.0 - 600.0) / 250.0)
    assert INFERRED_DEBT_NOTE not in _leverage(q)["label"]


def test_a_filed_zero_is_not_an_inference():
    """The filer tagged a zero and the facts summed it: that is a reading, not an inference, and the card says
    nothing extra about it."""
    q = pipeline._quality_metrics(_year(total_debt=0.0, lt_debt=0.0))
    assert q["debt_basis"] == debt.FILED
    assert INFERRED_DEBT_NOTE not in _leverage(q)["label"]


def test_every_debt_line_the_store_can_carry_is_checked():
    """The list is the facts layer's, not a copy: a tag added to #239's lines or to #825's fallback ladders is in
    scope here the day it lands, and never has to be added twice."""
    from RevenueChaser.rcdata.build.fundamentals import facts
    fields = debt.tag_fields()
    assert set(fields) == set(facts.DEBT_LINES) | set(facts.FALLBACK_DEBT_LINES)
    assert len(fields) == len(set(fields))


# ── The presence-only fields (measured 13 Nov 2026 on the real store; 6 of 307 were wrong without them) ──


@pytest.mark.parametrize("field, ticker, owed", [
    ("debt_combined_total", "TRN", 5_442.0),       # reached the card as "net cash", ND/EBITDA -0.15
    ("debt_combined_total", "CENX", 548.0),        # reached the card as "net cash", ND/EBITDA -0.71
    ("notes_and_loans_payable", "KBH", 1_693.0),
    ("unsecured_debt", "TLNC", 1.0),
    ("debt_instrument_carrying_amount", "ENB", 45_000.0),
    ("debt_instrument_carrying_amount", "CP", 14_691.0),
])
def test_a_filer_that_tags_a_borrowing_under_a_presence_only_concept_stays_unknown(field, ticker, owed):
    """These six do owe money and tag it, under a concept no total of this project sums. Without this check the
    rule called every one of them debt-free, and TRN and CENX showed "net cash" on the card."""
    q = pipeline._quality_metrics(_year(**{field: owed}))
    assert q["debt_basis"] is None
    assert q["net_debt_ebitda"] is None
    assert _leverage(q)["passed"] is None


@pytest.mark.parametrize("field, ticker, paid", [
    ("interest_expense_borrowings", "GENI", 5.4),     # the label says *borrowings*
    ("interest_expense_other", "KYMR", 4.2),
    ("interest_expense_other", "ROG", 0.4),
])
def test_interest_tagged_under_a_label_the_ladder_does_not_read_still_takes_the_filer_out(field, ticker, paid):
    """Four of the 300 write their interest under one of these two concepts (measured 13 Nov 2026). They are read
    here and nowhere else: ``interest_expense`` feeds the interest-cover and tax-rate engines, whose served
    figures must not move."""
    q = pipeline._quality_metrics(_year(**{field: paid}))
    assert q["debt_basis"] is None and q["net_debt_ebitda"] is None


@pytest.mark.parametrize("field", [f for f in debt.INTEREST_FIELDS if f != "interest_expense"])
def test_a_zero_under_one_of_those_labels_is_still_debt_free(field):
    """The rule is about a positive figure, not about the tag: a zero says it paid nothing."""
    assert pipeline._quality_metrics(_year(**{field: 0.0}))["debt_basis"] == debt.INFERRED_NONE


@pytest.mark.parametrize("field", debt.PRESENCE_ONLY_FIELDS)
def test_a_presence_only_field_tagged_at_zero_is_still_a_tag(field):
    assert pipeline._quality_metrics(_year(**{field: 0.0}))["debt_basis"] is None


def test_a_presence_only_field_enters_no_total_anywhere():
    """They say "there is debt", never "this much debt": neither the facts layer's ladders nor the extractor's own
    ``total_debt`` may carry one, or a per-instrument carrying amount would become a balance."""
    from RevenueChaser.rcdata.build.fundamentals import facts
    assert not set(debt.PRESENCE_ONLY_FIELDS) & (set(facts.DEBT_LINES) | set(facts.FALLBACK_DEBT_LINES))


def test_every_presence_only_field_is_a_field_the_store_can_carry():
    """A typo here would silently switch the check off, which is how the six got through in the first place."""
    from RevenueChaser.core.providers import sec_edgar
    assert set(debt.PRESENCE_ONLY_FIELDS) | set(debt.INTEREST_FIELDS) <= set(sec_edgar.CONCEPTS)
    assert set(debt.PRESENCE_ONLY_FIELDS) <= sec_edgar._DEBT_COMPONENT_FIELDS   # never sums, never a record
    assert set(debt.INTEREST_FIELDS) <= sec_edgar._NEVER_A_RECORD | sec_edgar._LAYER_ONLY_FIELDS


# ── The trailing year (rule 4: the same rule, read on the quarter the balance sheet comes from) ──

FLOWS = ("revenue", "gross_profit", "ebit", "depreciation")


def _fy(fy: int, vals: dict, balance: dict) -> dict:
    rec = {"fiscal_year": str(fy), **vals, **balance}
    rec["period_starts"] = {f: f"{fy}-01-01" for f in vals}
    rec["period_ends"] = {f: f"{fy}-12-31" for f in (*vals, *balance)}
    return rec


def _q(end: str, ytd: dict, balance: dict, start: str, months: int = 6) -> dict:
    return {"period": end, **balance,
            "ytd": {f: {"start": start, "months": months, "val": v} for f, v in ytd.items()}}


def _ttm_pair(year_over: dict | None = None, quarter_over: dict | None = None):
    flows = {"revenue": 1000.0, "gross_profit": 750.0, "ebit": 200.0, "depreciation": 50.0}
    annual = [_fy(2024, {k: v * 0.9 for k, v in flows.items()},
                  {"cash": 500.0, "total_equity": 800.0, **(year_over or {})}),
              _fy(2025, flows, {"cash": 600.0, "total_equity": 900.0, **(year_over or {})})]
    quarterly = [_q("2025-06-30", {k: v * 0.45 for k, v in flows.items()},
                    {"cash": 550.0, "total_equity": 850.0}, "2025-01-01"),
                 _q("2026-06-30", {k: v * 0.55 for k, v in flows.items()},
                    {"cash": 700.0, "total_equity": 1000.0, **(quarter_over or {})}, "2026-01-01")]
    return annual, quarterly


def test_the_trailing_year_is_counted_debt_free_too():
    annual, quarterly = _ttm_pair()
    q = pipeline._quality_metrics(annual, quarterly)
    assert q["quality_basis"] == "TTM" and q["quality_period_end"] == "2026-06-30"
    assert q["debt_basis"] == debt.INFERRED_NONE
    assert q["net_cash"] is True and q["net_debt_ebitda"] is not None


def test_a_debt_tag_on_the_quarter_stops_the_trailing_year_inference():
    """The debt tags are a balance sheet's, so the trailing year reads the quarter's, not the year's."""
    annual, quarterly = _ttm_pair(quarter_over={"senior_notes": 300.0})
    q = pipeline._quality_metrics(annual, quarterly)
    assert q["quality_basis"] == "TTM" and q["debt_basis"] is None and q["net_debt_ebitda"] is None


def test_interest_on_the_year_stops_the_trailing_year_inference():
    """A quarter that simply did not tag interest says nothing about the year that did: both have to be clean."""
    annual, quarterly = _ttm_pair(year_over={"interest_expense": 20.0})
    q = pipeline._quality_metrics(annual, quarterly)
    assert q["quality_basis"] == "TTM" and q["debt_basis"] is None and q["net_debt_ebitda"] is None


@pytest.mark.parametrize("field, ticker, owed", [
    ("short_term_bank_loans", "EXPD", 30.3),
    ("notes_payable_current", "IOVA", 1.0),
    ("senior_notes_current", "GMED", 0.0),
])
def test_a_debt_tag_on_the_anchor_year_stops_the_trailing_year_inference(field, ticker, owed):
    """The other half of rule 4, found by the 13 Nov measurement: 7 of the 300 tagged debt on the annual record
    and nothing on the later quarter, so the TTM path -- which reads the quarter -- called them debt-free anyway.
    A quarter that did not repeat the tag does not say the borrowing was repaid."""
    annual, quarterly = _ttm_pair(year_over={field: owed})
    q = pipeline._quality_metrics(annual, quarterly)
    assert q["quality_basis"] == "TTM"
    assert q["debt_basis"] is None and q["net_debt_ebitda"] is None


def test_a_later_quarter_that_tags_debt_stops_the_fiscal_year_inference():
    """The guard that makes the two bases agree. The board shows one leverage figure per name; when the quarters
    cannot give every figure the year gives, it falls back to the year. Without this, a filer that borrowed after
    its year end would fall back to a year the facts call debt-free and the card would say so."""
    annual = [_fy(2025, {"revenue": 1000.0, "gross_profit": 750.0, "ebit": 200.0, "depreciation": 50.0},
                  {"cash": 600.0, "total_equity": 900.0})]
    # A quarter after the year with a borrowing on it, and no revenue year to date, so no trailing year is built.
    quarterly = [{"period": "2026-06-30", "cash": 400.0, "total_equity": 950.0, "senior_notes": 300.0}]
    q = pipeline._quality_metrics(annual, quarterly)
    assert q["quality_basis"] == "FY"
    assert q["debt_basis"] is None and q["net_debt_ebitda"] is None and q["net_cash"] is None


def test_a_later_quarter_that_tags_nothing_leaves_the_inference_standing():
    annual = [_fy(2025, {"revenue": 1000.0, "gross_profit": 750.0, "ebit": 200.0, "depreciation": 50.0},
                  {"cash": 600.0, "total_equity": 900.0})]
    quarterly = [{"period": "2026-06-30", "cash": 400.0, "total_equity": 950.0}]
    q = pipeline._quality_metrics(annual, quarterly)
    assert q["quality_basis"] == "FY" and q["debt_basis"] == debt.INFERRED_NONE
