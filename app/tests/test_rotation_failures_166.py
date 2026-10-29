"""#166: the rotation jobs say what a UW failure cost them instead of finishing "done" with zeros.

* the holdings sync reads SPY and every holding once per job from the prices store and keeps each theme ETF's
  stored RS (naming them) when the store has too few SPY sessions;
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
from RevenueChaser.module_sector_rotation.tests.bars_seed import ny, pin_clock, seed
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS

THEMES = [t for t, i in ROTATION_ETFS.items() if i.get("group") == "theme"]
HOLDINGS = [{"ticker": "BIG", "name": "Big", "weight": "5.0"}, {"ticker": "DN", "name": "Down", "weight": "4.0"}]
END = date(2026, 8, 14)


def _rows(n, step=1.0, first=100.0):
    start = END - timedelta(days=n - 1)
    return [{"date": (start + timedelta(days=i)).isoformat(), "close": first + i * step} for i in range(n)]


class _RefusingUW:
    """A client that fails the test if it is asked for a candle: the job must read the store."""

    def __init__(self):
        self.calls = []

    def etf_ohlc(self, ticker, days=90, candle_size="1d"):
        self.calls.append(ticker)
        raise AssertionError(f"the holdings job must not ask UW for {ticker} candles")

    def health(self):
        return {"calls": 0, "failed": 0, "failures": [], "auth_error": None}


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
    def test_holdings_are_still_synced_when_the_store_has_no_bars(self, rawstore_tmp_db, holdings):
        with pytest.raises(JobMessage):
            runners.run_etf_holdings_sync(_UW([]))
        assert rawstore.get("etf_holdings", "XLK")["entry"] == HOLDINGS

    def test_the_rs_no_longer_depends_on_a_client(self, rawstore_tmp_db, holdings, monkeypatch):
        """With no client at all the holdings still get their RS, because it is a store read now."""
        pin_clock(monkeypatch, ny(2026, 8, 14, 18))
        for t in ("SPY", "BIG", "DN"):
            seed(t, END, start=END - timedelta(days=200))
        with pytest.raises(JobMessage) as exc:
            runners.run_etf_holdings_sync(uw=None)
        assert f"RS computed for {len(THEMES)} theme ETFs" in exc.value.message


class TestHoldingsSyncReadsThePricesStore:
    """The cut (PR: ties to the old data system): the job's RS enrichment reads the prices store, like the Rotation
    Fetch already does (``engine.fetch_all_holdings``, M-ROT-3), instead of asking UW for a candle per holding."""

    def test_the_rs_is_computed_from_the_store_with_no_bar_request(self, rawstore_tmp_db, holdings, monkeypatch):
        pin_clock(monkeypatch, ny(2026, 8, 14, 18))                 # latest complete session = END
        for t in ("SPY", "BIG", "DN"):
            seed(t, END, start=END - timedelta(days=200))
        uw = _RefusingUW()
        with pytest.raises(JobMessage) as exc:
            runners.run_etf_holdings_sync(uw)
        assert uw.calls == []                                        # not one candle asked
        assert f"RS computed for {len(THEMES)} theme ETFs" in exc.value.message
        assert rawstore.get("rotation_holdings", THEMES[0])["rows"]

    def test_a_store_without_enough_spy_keeps_the_stored_rs_and_names_the_etfs(self, rawstore_tmp_db, holdings,
                                                                               monkeypatch):
        pin_clock(monkeypatch, ny(2026, 8, 14, 18))
        stored = [{"ticker": "OLD", "rs_1m": 1.5}]
        rotation_store.save_holdings_cache(THEMES[0], stored)
        seed("BIG", END, start=END - timedelta(days=200))            # the holding is there, SPY is not
        uw = _RefusingUW()
        with pytest.raises(JobMessage) as exc:
            runners.run_etf_holdings_sync(uw)
        msg = exc.value.message
        assert f"Kept stored RS for {len(THEMES)} theme ETF(s)" in msg and THEMES[0] in msg
        assert "RS computed for 0 theme ETFs" in msg
        assert rawstore.get("rotation_holdings", THEMES[0])["rows"] == stored     # not overwritten by zeros
        assert uw.calls == []


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
