"""Yahoo-shaped annual records are judged by their period end too (Asymmetric Growth review SF-2, PR 6).

EDGAR records carry a ``period_ends`` dict; Yahoo's (``providers/yfinance``) carry one ``period_end``. The staleness
gate (470 days) and the adjacent-year rule read only the dict, so for a name scored from Yahoo they never applied,
while the board's Rules panel says they do. ``fiscal_year.period_end`` now reads the singular key as well.
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.core import fiscal_year as fy


def _yahoo(year: int, end: str | None, revenue: float = 100.0) -> dict:
    return {"fiscal_year": str(year), "period_end": end, "revenue": revenue}


def test_a_yahoo_record_has_a_period_end():
    assert fy.period_end(_yahoo(2024, "2024-12-31")) == date(2024, 12, 31)
    assert fy.period_end(_yahoo(2024, None)) is None


def test_a_stale_yahoo_filer_is_stale():
    annual = [_yahoo(2022, "2022-12-31"), _yahoo(2023, "2023-12-31")]
    assert fy.latest_is_stale(annual, date(2025, 6, 1))
    assert not fy.latest_is_stale(annual, date(2024, 6, 1))


def test_two_yahoo_years_more_than_a_year_apart_are_no_growth_pair():
    annual = [_yahoo(2021, "2021-12-31"), _yahoo(2022, "2023-12-31")]        # labels adjacent, ends two years apart
    assert fy.prior(annual, 1) is None
    assert fy.prior([_yahoo(2022, "2022-12-31"), _yahoo(2023, "2023-12-31")], 1) == 0


def test_an_edgar_record_still_reads_its_revenue_end_first():
    rec = {"fiscal_year": 2024, "period_end": "2020-01-01",
           "period_ends": {"revenue": "2024-09-28", "assets": "2024-09-30"}}
    assert fy.period_end(rec) == date(2024, 9, 28)
