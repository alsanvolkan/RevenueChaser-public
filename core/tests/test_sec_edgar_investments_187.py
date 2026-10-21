"""#187: EDGAR short-term investments next to cash, and the shared ``liquid_assets`` helper.

Synthetic companyfacts only, no network. Balance-sheet facts are instants: they carry
no ``start``. The same tests run for annual records (keyed by fiscal year) and for
quarterly records (keyed by the quarter end).
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import edgar_bulk
from RevenueChaser.core.providers import sec_edgar as se

_ENDS = {"annual": ("2022-12-31", "2023-12-31"), "quarterly": ("2023-03-31", "2023-06-30")}
_CASH = "CashAndCashEquivalentsAtCarryingValue"

STI = "short_term_investments"


def _instant(end: str, val: float, kind: str) -> dict:
    fp = "FY" if kind == "annual" else "Q2"
    return {"end": end, "val": val, "form": "10-K" if kind == "annual" else "10-Q",
            "fp": fp, "filed": f"{int(end[:4]) + 1}-02-01" if kind == "annual" else f"{end[:4]}-08-01"}


def _facts(kind: str, **concepts) -> dict:
    """``{concept: {end: val}}`` -> companyfacts with balance-sheet instants (no ``start``)."""
    return {"entityName": "Synthetic Co", "facts": {"us-gaap": {
        name: {"units": {"USD": [_instant(e, v, kind) for e, v in series.items()]}}
        for name, series in concepts.items()}}}


def _recs(kind: str, facts: dict) -> dict:
    if kind == "annual":
        return {r["fiscal_year"]: r for r in se.extract_annual(facts)}
    return {r["period"]: r for r in se.extract_quarterly(facts)}


def _key(kind: str, end: str) -> str:
    return end[:4] if kind == "annual" else end


@pytest.fixture(params=["annual", "quarterly"])
def kind(request):
    return request.param


@pytest.fixture
def ends(kind):
    return _ENDS[kind]


def _latest(kind, facts, ends):
    return _recs(kind, facts)[_key(kind, ends[1])]


# ── totals ──────────────────────────────────────────────────────────────────

def test_a_filer_with_no_securities_has_no_sti_and_liquidity_equals_cash(kind, ends):
    from RevenueChaser.core.liquidity import liquid_assets
    rec = _latest(kind, _facts(kind, **{_CASH: {ends[1]: 100.0}}), ends)
    assert rec[STI] is None
    assert "short_term_investments_basis" not in rec
    assert liquid_assets(rec) == (100.0, "cash_only")


@pytest.mark.parametrize("tag", ["ShortTermInvestments", "MarketableSecuritiesCurrent"])
def test_each_total_tag_alone_is_the_sti(kind, ends, tag):
    rec = _latest(kind, _facts(kind, **{_CASH: {ends[1]: 100.0}, tag: {ends[1]: 40.0}}), ends)
    assert rec[STI] == 40.0
    assert rec["short_term_investments_basis"] == "total"


def test_two_total_tags_in_one_period_are_never_summed(kind, ends):
    facts = _facts(kind, **{_CASH: {ends[1]: 100.0}, "ShortTermInvestments": {ends[1]: 40.0},
                            "MarketableSecuritiesCurrent": {ends[1]: 90.0}})
    assert _latest(kind, facts, ends)[STI] == 40.0  # ShortTermInvestments comes first (#236 order)


def test_a_total_beats_the_parts(kind, ends):
    facts = _facts(kind, **{_CASH: {ends[1]: 100.0}, "ShortTermInvestments": {ends[1]: 40.0},
                            "AvailableForSaleSecuritiesDebtSecuritiesCurrent": {ends[1]: 25.0},
                            "HeldToMaturitySecuritiesCurrent": {ends[1]: 10.0}})
    rec = _latest(kind, facts, ends)
    assert rec[STI] == 40.0
    assert rec["short_term_investments_basis"] == "total"


# ── parts ───────────────────────────────────────────────────────────────────

def test_with_no_total_afs_and_htm_are_summed(kind, ends):
    facts = _facts(kind, **{_CASH: {ends[1]: 100.0},
                            "AvailableForSaleSecuritiesDebtSecuritiesCurrent": {ends[1]: 25.0},
                            "HeldToMaturitySecuritiesCurrent": {ends[1]: 10.0}})
    rec = _latest(kind, facts, ends)
    assert rec[STI] == 35.0
    assert rec["short_term_investments_basis"] == "parts"


def test_afs_debt_and_afs_total_are_counted_once(kind, ends):
    facts = _facts(kind, **{_CASH: {ends[1]: 100.0},
                            "AvailableForSaleSecuritiesDebtSecuritiesCurrent": {ends[1]: 25.0},
                            "AvailableForSaleSecuritiesCurrent": {ends[1]: 30.0},
                            "HeldToMaturitySecuritiesCurrent": {ends[1]: 10.0}})
    assert _latest(kind, facts, ends)[STI] == 35.0  # AFS debt (25) + HTM (10), not 25 + 30 + 10


@pytest.mark.parametrize("tag, expected", [
    ("AvailableForSaleSecuritiesDebtSecuritiesCurrent", 25.0),
    ("AvailableForSaleSecuritiesCurrent", 25.0),
    ("HeldToMaturitySecuritiesCurrent", 25.0),
    ("DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLossCurrent", 25.0),
])
def test_each_part_tag_alone_is_counted(kind, ends, tag, expected):
    rec = _latest(kind, _facts(kind, **{_CASH: {ends[1]: 100.0}, tag: {ends[1]: 25.0}}), ends)
    assert rec[STI] == expected
    assert rec["short_term_investments_basis"] == "parts"


def test_both_htm_tags_in_one_period_are_counted_once(kind, ends):
    facts = _facts(kind, **{_CASH: {ends[1]: 100.0},
                            "HeldToMaturitySecuritiesCurrent": {ends[1]: 10.0},
                            "DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLossCurrent": {ends[1]: 9.0}})
    assert _latest(kind, facts, ends)[STI] == 10.0


def test_a_post_cecl_htm_tag_in_the_newer_period_and_the_old_tag_in_the_older_one(kind, ends):
    old, new = ends
    facts = _facts(kind, **{_CASH: {old: 100.0, new: 100.0},
                            "HeldToMaturitySecuritiesCurrent": {old: 10.0},
                            "DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLossCurrent": {new: 12.0}})
    recs = _recs(kind, facts)
    assert recs[_key(kind, old)][STI] == 10.0
    assert recs[_key(kind, new)][STI] == 12.0


# ── per period, not per series ──────────────────────────────────────────────

def test_a_tag_change_between_periods_keeps_both_periods(kind, ends):
    old, new = ends
    facts = _facts(kind, **{_CASH: {old: 100.0, new: 100.0},
                            "ShortTermInvestments": {old: 40.0},
                            "MarketableSecuritiesCurrent": {new: 55.0}})
    recs = _recs(kind, facts)
    assert recs[_key(kind, old)][STI] == 40.0  # a per-series pick would keep only the newer tag
    assert recs[_key(kind, new)][STI] == 55.0


def test_a_noncurrent_tag_change_between_periods_keeps_both_periods(kind, ends):
    old, new = ends
    facts = _facts(kind, **{_CASH: {old: 100.0, new: 100.0},
                            "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent": {old: 60.0},
                            "MarketableSecuritiesNoncurrent": {new: 70.0}})
    recs = _recs(kind, facts)
    assert recs[_key(kind, old)]["marketable_securities_noncurrent"] == 60.0  # a per-series pick drops it
    assert recs[_key(kind, new)]["marketable_securities_noncurrent"] == 70.0


# ── noncurrent and long-term investments are stored, never liquidity ────────

def test_noncurrent_securities_and_long_term_investments_have_their_own_fields(kind, ends):
    from RevenueChaser.core.liquidity import liquid_assets
    facts = _facts(kind, **{_CASH: {ends[1]: 100.0}, "ShortTermInvestments": {ends[1]: 40.0},
                            "MarketableSecuritiesNoncurrent": {ends[1]: 70.0},
                            "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent": {ends[1]: 60.0},
                            "LongTermInvestments": {ends[1]: 500.0}})
    rec = _latest(kind, facts, ends)
    assert rec["marketable_securities_noncurrent"] == 70.0  # first tag wins, per period
    assert rec["long_term_investments"] == 500.0
    assert rec[STI] == 40.0
    assert liquid_assets(rec) == (140.0, "cash+sti")  # neither noncurrent figure is counted


def test_the_noncurrent_alternate_is_used_when_the_first_tag_is_missing(kind, ends):
    facts = _facts(kind, **{_CASH: {ends[1]: 100.0},
                            "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent": {ends[1]: 60.0}})
    assert _latest(kind, facts, ends)["marketable_securities_noncurrent"] == 60.0


# ── guards ──────────────────────────────────────────────────────────────────

def test_a_negative_total_gives_none_and_a_suspect_flag(kind, ends):
    rec = _latest(kind, _facts(kind, **{_CASH: {ends[1]: 100.0},
                                        "ShortTermInvestments": {ends[1]: -40.0}}), ends)
    assert rec[STI] is None
    assert rec["short_term_investments_suspect"] is True
    assert "short_term_investments_basis" not in rec


def test_a_negative_part_gives_none_and_a_suspect_flag(kind, ends):
    rec = _latest(kind, _facts(kind, **{_CASH: {ends[1]: 100.0},
                                        "AvailableForSaleSecuritiesDebtSecuritiesCurrent": {ends[1]: 25.0},
                                        "HeldToMaturitySecuritiesCurrent": {ends[1]: -10.0}}), ends)
    assert rec[STI] is None
    assert rec["short_term_investments_suspect"] is True


def test_a_non_negative_sti_has_no_suspect_flag(kind, ends):
    rec = _latest(kind, _facts(kind, **{_CASH: {ends[1]: 100.0},
                                        "ShortTermInvestments": {ends[1]: 0.0}}), ends)
    assert rec[STI] == 0.0
    assert "short_term_investments_suspect" not in rec


def test_a_record_with_sti_but_no_cash_has_no_sti(kind, ends):
    facts = _facts(kind, **{"Assets": {ends[1]: 1000.0}, "ShortTermInvestments": {ends[1]: 40.0}})
    rec = _latest(kind, facts, ends)  # Assets gives the record; there is no cash to pair the STI with
    assert rec[STI] is None


def test_an_sti_total_ending_on_another_date_than_cash_is_dropped():
    # A 52/53-week filer: two fiscal years end in one calendar year, so they share a record.
    facts = _facts("annual", **{_CASH: {"2023-12-31": 100.0}, "ShortTermInvestments": {"2023-12-30": 40.0}})
    assert _recs("annual", facts)["2023"][STI] is None


def test_a_first_total_on_another_date_is_not_replaced_by_the_second_total():
    # The first total tag present wins its period; if it ends off the cash date the period gets no
    # investments (understated liquidity), it does not fall back to the next total tag.
    facts = _facts("annual", **{_CASH: {"2023-12-31": 100.0}, "ShortTermInvestments": {"2023-12-30": 40.0},
                                "MarketableSecuritiesCurrent": {"2023-12-31": 55.0}})
    rec = _recs("annual", facts)["2023"]
    assert rec[STI] is None
    assert "short_term_investments_basis" not in rec
    assert "short_term_investments_suspect" not in rec


def test_a_first_total_on_another_date_is_not_replaced_by_the_parts():
    facts = _facts("annual", **{_CASH: {"2023-12-31": 100.0}, "ShortTermInvestments": {"2023-12-30": 40.0},
                                "AvailableForSaleSecuritiesDebtSecuritiesCurrent": {"2023-12-31": 10.0}})
    rec = _recs("annual", facts)["2023"]
    assert rec[STI] is None
    assert "short_term_investments_basis" not in rec


def test_a_part_ending_on_another_date_than_cash_is_left_out():
    facts = _facts("annual", **{_CASH: {"2023-12-31": 100.0},
                                "AvailableForSaleSecuritiesDebtSecuritiesCurrent": {"2023-12-30": 25.0},
                                "HeldToMaturitySecuritiesCurrent": {"2023-12-31": 10.0}})
    rec = _recs("annual", facts)["2023"]
    assert rec[STI] == 10.0  # only the part that ends on the cash date
    assert rec["short_term_investments_basis"] == "parts"


def test_when_every_part_ends_on_another_date_there_is_no_sti():
    facts = _facts("annual", **{_CASH: {"2023-12-31": 100.0},
                                "AvailableForSaleSecuritiesDebtSecuritiesCurrent": {"2023-12-30": 25.0}})
    assert _recs("annual", facts)["2023"][STI] is None


# ── no new field adds a record ──────────────────────────────────────────────

@pytest.mark.parametrize("concept", ["MarketableSecuritiesNoncurrent", "LongTermInvestments"])
def test_a_period_with_only_investment_fields_adds_no_record(kind, ends, concept):
    old, new = ends
    facts = _facts(kind, **{_CASH: {old: 100.0}, concept: {new: 70.0}})
    assert set(_recs(kind, facts)) == {_key(kind, old)}  # the later, investments-only period is no record


def test_a_period_with_only_sti_facts_adds_no_record(kind, ends):
    old, new = ends
    facts = _facts(kind, **{_CASH: {old: 100.0}, "ShortTermInvestments": {new: 40.0}})
    assert set(_recs(kind, facts)) == {_key(kind, old)}


# ── bulk concepts ───────────────────────────────────────────────────────────

def test_every_investment_tag_is_in_the_bulk_concepts():
    tags = ["ShortTermInvestments", "MarketableSecuritiesCurrent",
            "AvailableForSaleSecuritiesDebtSecuritiesCurrent", "AvailableForSaleSecuritiesCurrent",
            "HeldToMaturitySecuritiesCurrent",
            "DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLossCurrent",
            "MarketableSecuritiesNoncurrent", "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
            "LongTermInvestments"]
    assert [t for t in tags if t not in edgar_bulk.USED_CONCEPTS] == []


# ── liquid_assets ───────────────────────────────────────────────────────────

def test_liquid_assets_adds_sti_to_cash():
    from RevenueChaser.core.liquidity import liquid_assets
    assert liquid_assets({"cash": 79.1, STI: 62.8}) == (pytest.approx(141.9), "cash+sti")


def test_a_missing_sti_counts_as_zero_when_cash_is_present():
    from RevenueChaser.core.liquidity import liquid_assets
    assert liquid_assets({"cash": 100.0, STI: None}) == (100.0, "cash_only")
    assert liquid_assets({"cash": 100.0}) == (100.0, "cash_only")  # a yfinance-shaped record has no STI key


def test_sti_never_stands_in_for_a_missing_cash():
    from RevenueChaser.core.liquidity import liquid_assets
    assert liquid_assets({"cash": None, STI: 40.0}) == (None, None)
    assert liquid_assets({STI: 40.0}) == (None, None)


def test_liquid_assets_of_no_record_is_unknown():
    from RevenueChaser.core.liquidity import liquid_assets
    assert liquid_assets(None) == (None, None)


def test_liquid_assets_never_counts_noncurrent_or_long_term_investments():
    from RevenueChaser.core.liquidity import liquid_assets
    rec = {"cash": 100.0, STI: 40.0, "marketable_securities_noncurrent": 70.0, "long_term_investments": 500.0}
    assert liquid_assets(rec) == (140.0, "cash+sti")


def test_a_negative_stored_sti_is_not_counted():
    from RevenueChaser.core.liquidity import liquid_assets
    assert liquid_assets({"cash": 100.0, STI: -5.0}) == (100.0, "cash_only")
