"""``uw/earnings``: the EPS a company reported, as UW serves it, in its own table.

It is not a ``fundamental_fact`` row on purpose: that table's check refuses a non-preliminary row without an
accession, and ``preliminary`` is what every served view reads as "not the SEC's, do not show". avo, 5 Nov 2026:
UW's reported EPS is a column of its own, source-labelled, never mixed with the SEC's GAAP EPS.
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.rcdata.build.fundamentals import uw_earnings

CIK, TICKER = "789019", "MSFT"
ANSWER = [
    {"report_type": "quarterly", "fiscal_date_ending": "2025-09-30", "report_date": "2025-10-29",
     "reported_eps": "3.72", "estimated_eps": "3.66", "surprise_percentage": "1.64"},
    {"report_type": "quarterly", "fiscal_date_ending": "2025-06-30", "report_date": "2025-07-30",
     "reported_eps": "3.65", "estimated_eps": "3.37", "surprise_percentage": "8.31"},
    {"report_type": "annual", "fiscal_date_ending": "2025-06-30", "report_date": "2025-07-30",
     "reported_eps": "13.64", "estimated_eps": "13.4", "surprise_percentage": "1.79"},
    {"report_type": "quarterly", "fiscal_date_ending": None, "reported_eps": "1.0"},
    {"report_type": "quarterly", "fiscal_date_ending": "2025-03-31", "reported_eps": None},
]


def test_the_quarterly_rows_become_one_row_per_period():
    rows, counts = uw_earnings.rows_of(ANSWER, CIK, TICKER)
    assert [r["period_end"] for r in rows] == [date(2025, 6, 30), date(2025, 9, 30)]   # oldest first
    newest = rows[-1]
    assert newest == {"cik": CIK, "ticker": TICKER, "period_end": date(2025, 9, 30),
                      "report_date": date(2025, 10, 29), "eps_reported": 3.72, "eps_estimate": 3.66,
                      "surprise_pct": 1.64, "source": uw_earnings.NAME}
    assert counts == {"not_quarterly": 1, "no_period_end": 1, "no_reported_eps": 1}


def test_the_surprise_is_uws_own_ratio_never_recomputed():
    """Recomputing it from two numbers of different bases is how a wrong surprise is made."""
    rows, _ = uw_earnings.rows_of([{"report_type": "quarterly", "fiscal_date_ending": "2025-09-30",
                                    "reported_eps": "2.0", "estimated_eps": "1.0",
                                    "surprise_percentage": "7.5"}], CIK, TICKER)
    assert rows[0]["surprise_pct"] == 7.5                      # not (2.0 - 1.0) / 1.0 * 100


def test_an_annual_row_sharing_a_quarters_date_never_wins():
    """The review's S3: an annual and a fourth-quarter row share a fiscal date, so ``report_type`` is read first."""
    rows, counts = uw_earnings.rows_of(
        [{"report_type": "annual", "fiscal_date_ending": "2025-06-30", "reported_eps": "13.64"},
         {"report_type": "quarterly", "fiscal_date_ending": "2025-06-30", "reported_eps": "3.65"}], CIK, TICKER)
    assert [r["eps_reported"] for r in rows] == [3.65] and counts == {"not_quarterly": 1}


def test_the_answer_may_be_a_data_object_or_a_bare_list():
    wrapped, _ = uw_earnings.rows_of({"data": ANSWER}, CIK, TICKER)
    bare, _ = uw_earnings.rows_of(ANSWER, CIK, TICKER)
    assert wrapped == bare and wrapped


def test_a_percent_sign_and_a_dash_are_read_as_uw_writes_them():
    rows, _ = uw_earnings.rows_of([{"report_type": "quarterly", "fiscal_date_ending": "2025-09-30",
                                    "reported_eps": "3.72", "estimated_eps": "-",
                                    "surprise_percentage": "1.64%"}], CIK, TICKER)
    assert rows[0]["eps_estimate"] is None and rows[0]["surprise_pct"] == 1.64


def test_the_served_table_is_its_own_and_the_schema_says_so():
    from RevenueChaser.rcdata.build.fundamentals import tables
    assert "earnings_reported" in tables.SCHEMAS
    assert [c for c, _ in tables.SCHEMAS["earnings_reported"]][:3] == ["cik", "ticker", "period_end"]
    # eps_reported is never a fundamental_fact field: that table is the SEC's values only.
    from RevenueChaser.rcdata.build.fundamentals import facts
    assert "eps_reported" not in facts.FIELDS
