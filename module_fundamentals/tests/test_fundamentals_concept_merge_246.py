"""#246: the Fundamentals annual YoY fallback after an EDGAR tag switch.

``_ttm_metrics`` compares ``annual[-1]`` with ``annual[-2]`` as RECORDS: a year with no record makes
it span two years. The extraction rule never bridges a missing year, so the gap case stays None.
Synthetic companyfacts, no network.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_fundamentals.pipeline import _ttm_metrics

REV = "Revenues"
SALES = "SalesRevenueNet"


def _fy(year: int, val: float) -> dict:
    end = f"{year}-12-31"
    start = (date.fromisoformat(end) - timedelta(days=364)).isoformat()
    return {"start": start, "end": end, "val": val, "form": "10-K", "fp": "FY",
            "filed": f"{year + 1}-03-01"}


def _growth(**tags: dict) -> float | None:
    facts = {"entityName": "Switch Co", "facts": {"us-gaap": {
        name: {"units": {"USD": [_fy(y, v) for y, v in series.items()]}} for name, series in tags.items()}}}
    return _ttm_metrics([], sec_edgar.extract_annual(facts))["rev_growth_ttm"]


def test_a_gap_below_the_new_tag_gives_no_growth_instead_of_a_two_year_figure():
    old = {y: 100.0 * 1.1 ** (y - 2017) for y in range(2017, 2025)}  # +10% a year, to 2024
    assert _growth(**{SALES: old, REV: {2026: 1000.0}}) is None


def test_restored_adjacent_history_gives_the_annual_yoy():
    assert _growth(**{SALES: {2024: 100.0, 2025: 110.0}, REV: {2026: 143.0}}) == pytest.approx(0.30)
