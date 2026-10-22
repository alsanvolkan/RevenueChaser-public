"""#58: the F5 blind-spot counters (``no_submissions``, ``no_cik``) are shown on the board, not only recorded.

A card with no amber corporate-event line means either "no qualifying 8-K" or "the store cannot answer for this
ticker". The two counters are the only thing that tells the board which, so the summary row states them next to
the stale-price counts. A payload stored before the counters existed, or a clean run (0 and 0), shows no line.
"""
from __future__ import annotations

from RevenueChaser.module_fundamentals import report as fu_report

SUMMARY = {"regime": "RISK_ON", "filtered_universe": 1500, "passing": {"rule_of_40": 40}, "top_n": 10}


def _payload(data_quality):
    payload = {"candidates": [], "summary": dict(SUMMARY), "universe_total": 1500}
    if data_quality is not None:
        payload["data_quality"] = data_quality
    return payload


def test_the_summary_states_both_counters():
    html = fu_report._summary_html(SUMMARY, {"no_submissions": 12, "no_cik": 3})
    assert "No corporate-event check of the 10 shown" in html
    assert "12 with no filings on record" in html and "3 with no CIK" in html


def test_only_the_counter_above_zero_is_named():
    html = fu_report._summary_html(SUMMARY, {"no_submissions": 5, "no_cik": 0})
    assert "5 with no filings on record" in html and "with no CIK" not in html
    html = fu_report._summary_html(SUMMARY, {"no_submissions": 0, "no_cik": 2})
    assert "2 with no CIK" in html and "no filings on record" not in html


def test_zero_counters_state_nothing():
    html = fu_report._summary_html(SUMMARY, {"no_submissions": 0, "no_cik": 0})
    assert "corporate-event check" not in html


def test_a_payload_without_the_counters_states_nothing():
    for dq in (None, {}, {"stale_ohlc": 4, "spy_stale": False}):
        html = fu_report._summary_html(SUMMARY, dq)
        assert "corporate-event check" not in html


def test_the_page_carries_the_line_from_payload_data_quality():
    page = fu_report.render_fundamental_dashboard(_payload({"no_submissions": 300, "no_cik": 7}))
    assert "300 with no filings on record" in page and "7 with no CIK" in page
    assert "corporate-event check" not in fu_report.render_fundamental_dashboard(_payload(None))
