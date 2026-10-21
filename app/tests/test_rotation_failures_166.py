"""#166: the rotation jobs say what a UW failure cost them instead of finishing "done" with zeros.

* the holdings sync fetches SPY once per job and keeps each theme ETF's stored RS (naming them) when
  SPY candles are missing;
* the range backfill in ``run_rotation_latest`` reports the days it did not write and the ETFs whose
  fetch failed.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import rawstore
from RevenueChaser.module_sector_rotation import store as rotation_store
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS

THEMES = [t for t, i in ROTATION_ETFS.items() if i.get("group") == "theme"]
HOLDINGS = [{"ticker": "BIG", "name": "Big", "weight": "5.0"}, {"ticker": "DN", "name": "Down", "weight": "4.0"}]
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


@pytest.fixture
def holdings(monkeypatch):
    import RevenueChaser.module_sector_rotation.etf_holdings as eh
    monkeypatch.setattr(eh, "get_etf_holdings", lambda t, uw: HOLDINGS)


class TestHoldingsSync:
    def test_spy_is_fetched_once_for_the_whole_job(self, rawstore_tmp_db, holdings):
        uw = _UW(_rows(70, 0.0))
        with pytest.raises(JobMessage) as exc:
            runners.run_etf_holdings_sync(uw)
        assert uw.calls.count("SPY") == 1
        assert f"RS computed for {len(THEMES)} theme ETFs" in exc.value.message
        assert rawstore.get("rotation_holdings", THEMES[0])["rows"]

    def test_missing_spy_keeps_the_stored_rs_and_names_the_theme_etfs(self, rawstore_tmp_db, holdings):
        stored = [{"ticker": "OLD", "rs_1m": 1.5}]
        rotation_store.save_holdings_cache(THEMES[0], stored)
        uw = _UW([])
        with pytest.raises(JobMessage) as exc:
            runners.run_etf_holdings_sync(uw)
        msg = exc.value.message
        assert f"Kept stored RS for {len(THEMES)} theme ETF(s): SPY candles missing" in msg
        assert THEMES[0] in msg and THEMES[-1] in msg
        assert "RS computed for 0 theme ETFs" in msg
        assert rawstore.get("rotation_holdings", THEMES[0])["rows"] == stored      # not overwritten by zeros
        assert rawstore.get("rotation_holdings", THEMES[1]) is None                # and nothing zero-filled
        assert uw.calls.count("SPY") == 1                                          # one fetch, not one per ETF

    def test_holdings_are_still_synced_when_spy_is_missing(self, rawstore_tmp_db, holdings):
        with pytest.raises(JobMessage):
            runners.run_etf_holdings_sync(_UW([]))
        assert rawstore.get("etf_holdings", "XLK")["entry"] == HOLDINGS

    def test_no_client_means_no_spy_fetch_and_no_note(self, rawstore_tmp_db, holdings):
        with pytest.raises(JobMessage) as exc:
            runners.run_etf_holdings_sync(uw=None)
        assert "SPY" not in exc.value.message


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
