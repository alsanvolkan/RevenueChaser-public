"""#246: what the AG board sees when EDGAR keeps an old tag's history after a tag switch.

``score_ticker`` takes the last two and three NON-NULL revenues as YoY growth and never checks that
the years are adjacent, so the extraction rule (restore only contiguous, one-fiscal-year-apart
history) is what keeps a gap from turning into a wrong growth figure. Synthetic companyfacts, no
network.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_asymmetric_growth.engine import score_ticker

REV = "Revenues"
RFC = "RevenueFromContractWithCustomerExcludingAssessedTax"
SALES = "SalesRevenueNet"


def _fy(year: int, val: float) -> dict:
    end = f"{year}-12-31"
    start = (date.fromisoformat(end) - timedelta(days=364)).isoformat()
    return {"start": start, "end": end, "val": val, "form": "10-K", "fp": "FY",
            "filed": f"{year + 1}-03-01"}


def _facts(**tags: dict) -> dict:
    """``{concept: {year: value}}`` -> companyfacts."""
    return {"entityName": "Switch Co", "facts": {"us-gaap": {
        name: {"units": {"USD": [_fy(y, v) for y, v in series.items()]}} for name, series in tags.items()}}}


def _card(facts: dict) -> dict:
    financials = {"info": {"country": "United States", "exchange": "NMS", "longName": "Switch Co",
                           "industry": "Biotechnology"},
                  "annual": sec_edgar.extract_annual(facts), "market_cap": 5e9, "source": "sec_edgar"}
    tech = {"price": 100.0, "dist_52w_pct": 0.05, "vol_ratio": 1.6, "above_sma50": True}
    return score_ticker("SWCH", financials, tech)


def _sub(card: dict, param: str) -> dict:
    return next(sub for signal in card["signals"] for sub in signal["subs"] if sub["param"] == param)


def _old_tag(years, last: float) -> dict:
    """A 10%-a-year series ending at ``last`` in the last of ``years``."""
    years = list(years)
    return {y: last / 1.1 ** (years[-1] - y) for y in years}


def test_a_gap_below_the_new_tag_gives_no_growth_instead_of_a_two_year_figure():
    # Old tag 2017-2024 (+10% a year), new tag 2026 only: 2025 is in neither. 2026 vs 2024 is not YoY.
    card = _card(_facts(**{SALES: _old_tag(range(2017, 2025), 100.0), REV: {2026: 500.0}}))
    assert card["rev_growth"] is None
    assert _sub(card, "accelerating")["value"] == (None, None)


def test_restored_adjacent_history_gives_real_growth_and_acceleration():
    old = {2024: 100.0, 2025: 110.0}  # +10% in 2025
    card = _card(_facts(**{SALES: old, REV: {2026: 143.0}}))  # +30% in 2026
    assert card["rev_growth"] == pytest.approx(0.30)
    assert _sub(card, "rev_growth")["passed"] is True
    assert _sub(card, "accelerating")["value"] == pytest.approx((0.30, 0.10))
    assert _sub(card, "accelerating")["passed"] is True


def test_a_filled_hole_corrects_the_previous_years_growth():
    # Without the fill the board reads 2023 vs 2021 (+21%) as last year's growth; with it, +10%.
    card = _card(_facts(**{REV: {2021: 100.0, 2023: 121.0, 2024: 133.1}, SALES: {2022: 110.0}}))
    assert _sub(card, "accelerating")["value"] == pytest.approx((0.10, 0.10))
    assert _sub(card, "accelerating")["passed"] is False


def test_a_restored_old_year_no_longer_supplies_a_stale_gross_margin():
    """#150 U8 closes the effect #246 documented: the score reads the newest fiscal year only, so the
    2024 margin (0.50) is not shown while the two newest years (2025, 2026) have no gross profit."""
    facts = _facts(**{SALES: {2023: 100.0, 2024: 120.0}, RFC: {2025: 150.0, 2026: 180.0},
                      "GrossProfit": {2023: 50.0, 2024: 60.0}})
    assert _card(facts)["gross_margin"] is None
