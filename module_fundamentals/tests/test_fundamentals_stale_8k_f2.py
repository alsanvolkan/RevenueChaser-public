"""Review F2 (28 Oct): a filer whose 8-K list the daily sync has not checked lately is a blind spot, not "no event".

Before, only a CIK with no filing row at all was counted as unknown. A CIK with old rows whose sync had stopped (its
filing list not read again) looked clean: a recent 8-K the store never
received could not raise the flag. Now a candidate whose CIK was last checked more than
``config.CORPORATE_EVENT_CHECK_MAX_AGE_DAYS`` ago is counted as ``stale_submissions`` and the summary line
names it. Display only, like the flag itself.
"""
from __future__ import annotations

from datetime import timedelta

from RevenueChaser.module_fundamentals import config, events, freeze
from RevenueChaser.module_fundamentals.report import _summary_html
from RevenueChaser.module_fundamentals.tests.test_fundamentals_events_386 import (
    TODAY, _INSIDE, _filing, _master)
from RevenueChaser.rcdata.tests import fund_store


def test_a_filer_not_checked_lately_is_counted_stale(rawstore_tmp_db):
    """On the layer a CIK's check is the day its filing list was read (``submissions_as_of``, #642 PR 6)."""
    max_age = config.CORPORATE_EVENT_CHECK_MAX_AGE_DAYS
    _master(("FRESH", "0000001"), ("OLD", "0000002"), ("EDGE", "0000004"))
    for cik, days_ago in (("1", 1), ("2", max_age + 1), ("4", max_age)):     # EDGE: exactly at the line, fresh
        fund_store.put_filings([_filing(cik, f"a{cik}", _INSIDE, "9.01")], listed_on=TODAY - timedelta(days=days_ago))
    stats: dict = {}
    events.corporate_events(["FRESH", "OLD", "EDGE"], today=TODAY, stats=stats)
    assert stats["stale_submissions"] == 1           # OLD
    assert stats["no_submissions"] == 0


def test_a_cik_with_no_filing_row_is_not_also_stale(rawstore_tmp_db):
    _master(("NONE", "0000009"))
    stats: dict = {}
    events.corporate_events(["NONE"], today=TODAY, stats=stats)
    assert stats == {"no_submissions": 1, "no_cik": 0, "stale_submissions": 0}


def test_the_summary_names_the_stale_ones():
    html = _summary_html({"top_n": 5}, {"stale_submissions": 3})
    assert "3 with an 8-K list not checked in the last" in html


def test_the_window_is_display_only():
    assert "CORPORATE_EVENT_CHECK_MAX_AGE_DAYS" not in freeze._HASHED
