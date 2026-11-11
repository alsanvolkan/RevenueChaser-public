"""Review N5 (28 Oct): the SEC EDGAR provider health counts the downloads that worked, not only the ones that failed.

Before, ``_EDGAR_HEALTH.call`` ran only on a failure, so its ``calls`` equalled ``failed`` and the banner read
"3 of 3 SEC EDGAR calls failed" on a run where hundreds of downloads had worked. Now a download that answered is
counted as a call too. A stored row read without a download is no call; a Refresh (``no_fetch``) makes none.
"""
from __future__ import annotations

from RevenueChaser.module_fundamentals import pipeline
from RevenueChaser.module_fundamentals.tests.test_fundamentals_w3_386 import _compute


def test_a_download_that_worked_is_a_call_and_a_failure_is_one_of_them(monkeypatch):
    pipeline._EDGAR_HEALTH.reset()
    _compute(monkeypatch, [], edgar=lambda *a, **k: ([], [], {"available": False, "downloaded": True}))
    _compute(monkeypatch, [], edgar=lambda *a, **k: ([], [], {"available": False, "fetch_failed": True}))
    _compute(monkeypatch, [], edgar=lambda *a, **k: ([], [], {"available": False}))      # stored or no CIK
    h = pipeline._EDGAR_HEALTH.health()
    assert (h["calls"], h["failed"]) == (2, 1)
