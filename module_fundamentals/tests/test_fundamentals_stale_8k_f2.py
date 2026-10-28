"""Review F2 (28 Oct): a filer whose 8-K list the daily sync has not checked lately is a blind spot, not "no event".

Before, only a CIK with no filing row at all was counted as unknown. A CIK with old rows whose sync had stopped (the
``source_checks`` watermark of dataset ``sec_submissions`` left unread) looked clean: a recent 8-K the store never
received could not raise the flag. Now a candidate whose CIK was last checked more than
``config.CORPORATE_EVENT_CHECK_MAX_AGE_DAYS`` ago, or never, is counted as ``stale_submissions`` and the summary line
names it. Display only, like the flag itself.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from RevenueChaser.core import rawstore
from RevenueChaser.module_fundamentals import config, events, freeze
from RevenueChaser.module_fundamentals.report import _summary_html
from RevenueChaser.module_fundamentals.tests.test_fundamentals_events_386 import (
    TODAY, _INSIDE, _filing, _filings, _master)


def _checked(cik, days_ago):
    at = datetime(TODAY.year, TODAY.month, TODAY.day, 12, tzinfo=timezone.utc) - timedelta(days=days_ago)
    rawstore.write_rows("source_checks", [{"dataset": "sec_submissions", "source": "sec_submissions", "key": cik,
                                           "checked_at": at.timestamp(), "run_id": f"r{days_ago}"}])


def test_a_filer_not_checked_lately_is_counted_stale(rawstore_tmp_db):
    max_age = config.CORPORATE_EVENT_CHECK_MAX_AGE_DAYS
    _master(("FRESH", "0000001"), ("OLD", "0000002"), ("NEVER", "0000003"), ("EDGE", "0000004"))
    _filings(_filing("1", "a", _INSIDE, "9.01"), _filing("2", "b", _INSIDE, "9.01"),
             _filing("3", "c", _INSIDE, "9.01"), _filing("4", "d", _INSIDE, "9.01"))
    _checked("1", 1)
    _checked("2", max_age + 30)
    _checked("2", max_age + 1)          # its newest check still too old
    _checked("4", max_age)              # exactly at the line: fresh
    stats: dict = {}
    events.corporate_events(["FRESH", "OLD", "NEVER", "EDGE"], today=TODAY, stats=stats)
    assert stats["stale_submissions"] == 2           # OLD and NEVER
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
