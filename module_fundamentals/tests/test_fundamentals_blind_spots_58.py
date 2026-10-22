"""#58: the F5 blind-spot counters (``no_submissions``, ``no_cik``) are shown on the board, not only recorded.

A card with no amber corporate-event line means either "no qualifying 8-K" or "the store cannot answer for this
ticker". The two counters are the only thing that tells the board which, so the summary row states them next to
the stale-price counts. A payload stored before the counters existed (no keys) shows no line: unknown is not zero.
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
    assert "No filings on record: 12" in html
    assert "no CIK: 3" in html


def test_a_zero_counter_is_still_stated():
    html = fu_report._summary_html(SUMMARY, {"no_submissions": 0, "no_cik": 0})
    assert "No filings on record: 0" in html and "no CIK: 0" in html


def test_a_payload_without_the_counters_states_nothing():
    for dq in (None, {}, {"stale_ohlc": 4, "spy_stale": False}):
        html = fu_report._summary_html(SUMMARY, dq)
        assert "No filings on record" not in html and "no CIK" not in html


def test_the_page_carries_the_line_from_payload_data_quality():
    page = fu_report.render_fundamental_dashboard(_payload({"no_submissions": 300, "no_cik": 7}))
    assert "No filings on record: 300" in page and "no CIK: 7" in page
    assert "No filings on record" not in fu_report.render_fundamental_dashboard(_payload(None))
