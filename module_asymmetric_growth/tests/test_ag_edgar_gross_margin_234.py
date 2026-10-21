"""#234: an AG card gets its gross-margin sub from EDGAR revenue − cost of revenue.

Fixture filer: tags ``Revenues`` and ``CostOfRevenue`` but never ``GrossProfit``
(META, ABBV, TMO are real examples). Synthetic companyfacts, no network.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_asymmetric_growth.engine import score_ticker

YEARS = ("2021", "2022", "2023")


def _facts(cost_concept: str = "CostOfRevenue", costs=(480.0, 500.0, 540.0)) -> dict:
    def series(vals):
        return {"units": {"USD": [
            {"start": f"{y}-01-01", "end": f"{y}-12-31", "val": v, "form": "10-K",
             "fp": "FY", "filed": f"{int(y) + 1}-02-01"} for y, v in zip(YEARS, vals)]}}

    return {"entityName": "No GrossProfit Co", "facts": {"us-gaap": {
        "Revenues": series((800.0, 850.0, 1080.0)),
        cost_concept: series(costs),
        "NetCashProvidedByUsedInOperatingActivities": series((120.0, 150.0, 250.0)),
        "PaymentsToAcquirePropertyPlantAndEquipment": series((20.0, 25.0, 40.0)),
    }}}


def _card(facts: dict) -> dict:
    financials = {"info": {"country": "United States", "exchange": "NMS", "longName": "Fixture Co",
                           "industry": "Software"},
                  "annual": sec_edgar.extract_annual(facts), "market_cap": 5e9, "source": "sec_edgar"}
    tech = {"price": 100.0, "dist_52w_pct": 0.05, "vol_ratio": 1.6, "above_sma50": True}
    return score_ticker("FIXT", financials, tech)


def _gross_margin_sub(card: dict) -> dict:
    growth = next(s for s in card["signals"] if s["key"] == "growth")
    return next(s for s in growth["subs"] if s["param"] == "gross_margin")


def test_derived_gross_margin_passes_the_40_percent_sub():
    card = _card(_facts())  # (1080 - 540) / 1080 = 50%
    sub = _gross_margin_sub(card)
    assert card["gross_margin"] == pytest.approx(0.5)
    assert sub["value"] == pytest.approx(0.5)
    assert sub["passed"] is True


def test_derived_gross_margin_below_40_percent_does_not_pass():
    card = _card(_facts(costs=(480.0, 500.0, 700.0)))  # (1080 - 700) / 1080 = 35.2%
    sub = _gross_margin_sub(card)
    assert sub["value"] == pytest.approx(380.0 / 1080.0)
    assert sub["passed"] is False


def test_a_filer_tagging_only_an_excluded_cost_concept_stays_unknown():
    card = _card(_facts(cost_concept="CostsAndExpenses"))  # includes opex: never a cost of revenue
    sub = _gross_margin_sub(card)
    assert card["gross_margin"] is None
    assert sub["value"] is None
    assert sub["passed"] is None   # unknown, not a fail (#151)
