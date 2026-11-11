"""#166: the rotation jobs say what a UW failure cost them instead of finishing "done" with zeros.

* the range backfill in ``run_rotation_latest`` reports the days it did not write and the ETFs whose
  fetch failed. (The holdings sync's cases went with the holdings job, #642 PR 5.)
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.module_sector_rotation import store as rotation_store

END = date(2026, 8, 14)


def _rows(n, step=1.0, first=100.0):
    start = END - timedelta(days=n - 1)
    return [{"date": (start + timedelta(days=i)).isoformat(), "close": first + i * step} for i in range(n)]


class _UW:
    def __init__(self, spy):
        self.spy, self.calls = spy, []

    def etf_ohlc(self, ticker, days=90, candle_size="1d"):
        self.calls.append(ticker)
        return self.spy if ticker == "SPY" else _rows(70, 1.0)

    def health(self):
        return {"calls": 0, "failed": 0, "failures": [], "auth_error": None}


class TestRangeBackfillMessage:
    def _patch(self, monkeypatch, result):
        monkeypatch.setattr(rotation_store, "backfill_weekly", lambda uw, s, e: result)
        monkeypatch.setattr(runners, "_load_rotation_snapshots", lambda start=None, end=None: [])
        import RevenueChaser.module_sector_rotation.report as rep
        monkeypatch.setattr(rep, "export_rotation_dashboard", lambda *a, **kw: runners.ROTATION_LATEST)

    def test_failed_etfs_and_skipped_days_are_in_the_job_message(self, monkeypatch, rawstore_tmp_db):
        self._patch(monkeypatch, rotation_store.BackfillResult(2, failed=["XLK", "XLF"], skipped=5))
        with pytest.raises(JobMessage) as exc:
            runners.run_rotation_latest(_UW([]), start="2026-07-01", end="2026-08-14")
        assert "5 days not written, ETF fetch failed: XLK, XLF" in exc.value.message

    def test_a_short_spy_history_is_named_when_nothing_failed(self, monkeypatch, rawstore_tmp_db):
        self._patch(monkeypatch, rotation_store.BackfillResult(0, skipped=3))
        with pytest.raises(JobMessage) as exc:
            runners.run_rotation_latest(_UW([]), start="2026-07-01", end="2026-08-14")
        assert "3 days not written" in exc.value.message and "SPY" in exc.value.message

    def test_a_clean_backfill_ends_without_a_message(self, monkeypatch, rawstore_tmp_db):
        self._patch(monkeypatch, rotation_store.BackfillResult(7))
        assert runners.run_rotation_latest(_UW([]), start="2026-07-01", end="2026-08-14") == runners.ROTATION_LATEST

    def test_a_plain_int_from_an_old_backfill_is_still_accepted(self, monkeypatch, rawstore_tmp_db):
        self._patch(monkeypatch, 3)
        assert runners.run_rotation_latest(_UW([]), start="2026-07-01", end="2026-08-14") == runners.ROTATION_LATEST
