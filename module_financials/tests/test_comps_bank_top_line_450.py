"""#450: the bank top line never enters a peer comparison as revenue.

``interest_income_top_line`` is stored next to ``revenue`` in the same annual record. The comps
engine reads ``revenue`` by name (``latest.get("revenue")``), so a bank record that carries only the
new field produces no EV/Revenue multiple: the bank does not enter the peer comparison through a
different definition of the top line. The record here comes from ``extract_annual`` over synthetic
companyfacts, not a hand-built dict, so adding a bank tag to ``CONCEPTS["revenue"]`` would show up
as a revenue value and this test would see it.
"""
from __future__ import annotations

from RevenueChaser.core.providers import sec_edgar as se
from RevenueChaser.core.tests.test_sec_edgar_concept_merge_246 import _facts, _s
from RevenueChaser.module_financials.engines.comps_engine import run_comps

BANK = "interest_income_top_line"
RNOI = "RevenuesNetOfInterestExpense"
IODI = "InterestAndDividendIncomeOperating"
_PEER = {"enterpriseValue": 1000.0, "totalRevenue": 100.0, "ebitda": 50.0, "marketCap": 900.0,
         "netIncomeToCommon": 10.0}


def test_a_bank_with_no_revenue_gets_no_ev_revenue_multiple():
    # Both bank tags are present, so adding EITHER of them to ``CONCEPTS["revenue"]`` shows up here
    # as a revenue value (and an EV/Revenue the bank should not have).
    annual = se.extract_annual(_facts(flow={RNOI: _s([2024, 2025], 10000),
                                           IODI: _s([2024, 2025], 20000),
                                           "NetIncomeLoss": _s([2024, 2025], 500)}))
    assert annual[-1][BANK] == 12025.0   # the net-of-interest-expense tag wins the tie
    assert annual[-1]["revenue"] is None

    res = run_comps({"ticker": "BANK", "annual": annual, "shares_outstanding": 100.0},
                    ["PEER"], fetch_info=lambda _t: _PEER)
    assert "error" not in res
    assert res["multiples"]["ev_revenue"]["p50"] == 10.0   # the peer side does compute the multiple
    assert res["implied"]["ev_revenue"] is None            # the bank's own top line is not revenue
    assert res["implied"]["per_share_ev_revenue"] is None
