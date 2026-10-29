"""#166: a failed candle fetch must not become zero relative strength or a thinner cross-section.

``relative_strength_pct`` returns 0.0 when its data is short (that contract stays: it is a pure
helper). The callers are where "no SPY candles" used to turn into a stored RS of 0.0 for every
holding, every ETF and every backfilled day. They now stop instead, and say why.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.module_sector_rotation import engine, store
from RevenueChaser.module_sector_rotation.tests.bars_seed import FIRST, ny, pin_clock, seed
from RevenueChaser.module_sector_rotation.universe import MACRO_SERIES

BASE = date(2026, 8, 14)


def _rows(n, start, step=1.0, first=100.0):
    return [{"date": (start + timedelta(days=i)).isoformat(), "close": first + i * step}
            for i in range(n)]


class FakeUW:
    """The old list-only client surface: ``etf_ohlc`` returns a list, no ``*_result`` methods."""

    def __init__(self, spy=None, etfs=None):
        self.spy = spy if spy is not None else []
        self.etfs = etfs or {}
        self.calls: list[str] = []

    def etf_ohlc(self, ticker, days=90, candle_size="1d"):
        self.calls.append(ticker)
        return self.spy if ticker == "SPY" else self.etfs.get(ticker, [])


class ResultUW(FakeUW):
    """A client with ``etf_ohlc_result``: ``failed`` tickers fail (HTTP ``status``), ``empty`` ones say 'no bars'."""

    def __init__(self, spy=None, etfs=None, failed=(), empty=(), status=500):
        super().__init__(spy, etfs)
        self.failed, self.empty, self.status = set(failed), set(empty), status

    def etf_ohlc_result(self, ticker, days=90, candle_size="1d"):
        self.calls.append(ticker)
        if ticker in self.failed:
            return ProviderResult("failed", [], {"status": self.status, "error": f"HTTP {self.status}"}, "")
        if ticker in self.empty:
            return ProviderResult("ok", [], None, "")
        return ProviderResult("ok", self.spy if ticker == "SPY" else self.etfs.get(ticker, []), None, "")


# ── relative_strength_pct keeps its contract ──────────────────────────────────────────────

def test_relative_strength_pct_still_returns_zero_on_short_data():
    assert engine.relative_strength_pct([1.0, 2.0], [1.0, 2.0], 5) == 0.0
    assert engine.relative_strength_pct([1.0] * 70, [], 5) == 0.0


# ── holdings RS (the prices store since M-ROT-3; the per-holding candle path is gone) ─────

HOLDINGS = [{"ticker": "BIG", "name": "Big", "weight": "5.0"}, {"ticker": "DN", "name": "Down", "weight": "4.0"}]


def _healthy():
    """A client that must not be asked anything: the holdings RS is a store read."""
    start = BASE - timedelta(days=89)
    return FakeUW(spy=_rows(70, start, step=0.0), etfs={"BIG": _rows(70, start, 1.0),
                                                        "DN": _rows(70, start, -1.0, 200.0)})


def test_fetch_all_holdings_reads_d01_once_for_the_whole_job(rawstore_tmp_db, monkeypatch):
    """Since M-ROT-3 (#374) the holdings RS is one D01 read of SPY and every holding, and no UW call."""
    from RevenueChaser.module_sector_rotation.models import ETFRotationSnapshot
    uw = _healthy()
    for t in ("SPY", "BIG", "DN"):
        seed(t, BASE, start=BASE - timedelta(days=150))
    reads, real = [], engine.bar_points
    monkeypatch.setattr(engine, "bar_points", lambda *a, **k: reads.append(a) or real(*a, **k))
    # #303: the holdings come from the store (the holdings job), not from a fetch
    rawstore.write_rows("etf_holdings", [{"ticker": t, "entry": HOLDINGS}
                                         for t in ("AIQ", "ARKQ", "IGV", "ROBO", "SKYY")])
    snaps = [ETFRotationSnapshot(as_of_date=BASE, ticker=t, group_name="theme", close_price=1.0, rs_1w=0, rs_1m=0,
                                 rs_3m=0, rs_ratio=0, rs_momentum=0, rs_ratio_z=0, rs_momentum_z=0,
                                 rrg_quadrant="LEADING") for t in ("AIQ", "ARKQ", "IGV", "ROBO", "SKYY")]
    out = engine.fetch_all_holdings(uw, snaps)
    assert set(out) == {"AIQ", "ARKQ", "IGV", "ROBO", "SKYY"}
    assert len(reads) == 1 and uw.calls == []


def test_fetch_all_holdings_stores_nothing_when_spy_is_missing(rawstore_tmp_db):
    from RevenueChaser.module_sector_rotation.models import ETFRotationSnapshot
    uw = _healthy()
    seed("BIG", BASE, start=BASE - timedelta(days=150))                 # D01 holds the holding, not SPY
    rawstore.write_rows("etf_holdings", [{"ticker": "AIQ", "entry": HOLDINGS}])
    snap = ETFRotationSnapshot(as_of_date=BASE, ticker="AIQ", group_name="theme", close_price=1.0, rs_1w=0, rs_1m=0,
                               rs_3m=0, rs_ratio=0, rs_momentum=0, rs_ratio_z=0, rs_momentum_z=0, rrg_quadrant="LEADING")
    assert engine.fetch_all_holdings(uw, [snap]) == {}


# ── backfills (D01 since M-ROT-2, #374: no fetch, so no fetch failure; the SPY guards stay) ─────────────────────────

ETFS = ("XLK", "XLF", "XLE", "XLV")


@pytest.fixture
def fixed_today(monkeypatch):
    """The latest complete session is ``BASE`` (Friday 2026-08-14, after the close)."""
    pin_clock(monkeypatch, ny(2026, 8, 14, 18))


def _full_d01(spy_start=FIRST, skip=()):
    seed("SPY", BASE, start=spy_start, closes=lambda i: 100.0 + i)
    for t in ETFS:
        if t not in skip:
            seed(t, BASE, closes=lambda i: 100.0 + i * 1.01)


def _stored_dates():
    return store.list_snapshot_dates()


class TestBackfillResultIsAnInt:
    def test_the_return_value_is_still_the_number_of_days_written(self, rawstore_tmp_db, fixed_today):
        _full_d01()
        res = store.backfill_snapshots(None, days=5)
        assert isinstance(res, int) and res == 5 and res == len(_stored_dates())
        assert res.skipped == 0 and res.failed == []


class TestWeeklyBackfillSpyGuard:
    def test_missing_spy_writes_no_zero_rs_rows(self, rawstore_tmp_db, fixed_today):
        for t in ETFS:
            seed(t, BASE)
        res = store.backfill_weekly(None, date(2026, 7, 1), date(2026, 8, 14))
        assert res == 0 and _stored_dates() == []
        assert rawstore.query("SELECT COUNT(*) AS n FROM rotation_snapshots")[0]["n"] == 0
        assert res.skipped == 7
        assert "SPY" in res.message()

    def test_every_day_is_guarded_not_only_the_last_one(self, rawstore_tmp_db, fixed_today):
        # SPY starts in ISO week 2025-W36, the ETFs months earlier: the Wednesdays before 2026-W31 have ETF history but
        # fewer than 48 SPY weeks. A short benchmark would read every RS as 0.0.
        _full_d01(spy_start=date(2025, 9, 1))
        res = store.backfill_weekly(None, date(2026, 6, 1), date(2026, 8, 14))
        assert _stored_dates() == ["2026-07-29", "2026-08-05", "2026-08-12"]
        assert res == 3 and res.skipped == 8
        zero = rawstore.query("SELECT COUNT(*) AS n FROM rotation_snapshots WHERE rs_1m = 0 AND rs_3m = 0")[0]["n"]
        assert zero == 0

    def test_a_healthy_run_is_unchanged(self, rawstore_tmp_db, fixed_today):
        _full_d01()
        uw = FakeUW()
        res = store.backfill_weekly(uw, date(2026, 7, 1), date(2026, 8, 14))
        assert res == 7 and res.skipped == 0 and res.failed == []
        assert len(_stored_dates()) == 7
        assert uw.calls == []                                   # D01 only: the provider is never asked


class TestBackfillD01Gaps:
    def test_an_etf_with_no_bars_is_left_out_not_a_failure(self, rawstore_tmp_db, fixed_today):
        _full_d01(skip={"XLK"})
        res = store.backfill_weekly(None, date(2026, 7, 1), date(2026, 8, 14))
        assert res == 7 and res.failed == [] and res.skipped == 0
        tickers = {r["ticker"] for r in rawstore.query("SELECT DISTINCT ticker FROM rotation_snapshots")}
        assert "XLK" not in tickers and "XLF" in tickers

    def test_the_macro_series_are_never_backfilled(self, rawstore_tmp_db, fixed_today):
        _full_d01()
        store.backfill_weekly(None, date(2026, 7, 1), date(2026, 8, 14))
        tickers = {r["ticker"] for r in rawstore.query("SELECT DISTINCT ticker FROM rotation_snapshots")}
        assert tickers == set(ETFS) and not tickers & set(MACRO_SERIES)

    def test_stored_days_are_not_counted_as_skipped(self, rawstore_tmp_db, fixed_today):
        _full_d01()
        store.backfill_weekly(None, date(2026, 7, 1), date(2026, 8, 14))
        res = store.backfill_weekly(None, date(2026, 7, 1), date(2026, 8, 14))
        assert res == 0 and res.skipped == 0                  # nothing was missing, nothing was lost
        assert len(_stored_dates()) == 7


class TestDailyBackfillGuards:
    def test_a_short_spy_history_writes_nothing(self, rawstore_tmp_db, fixed_today):
        _full_d01(spy_start=BASE - timedelta(days=40))
        res = store.backfill_snapshots(None, days=5)
        assert res == 0 and _stored_dates() == [] and res.skipped == 5

    def test_every_day_is_guarded_not_only_the_last_one(self, rawstore_tmp_db, fixed_today):
        # SPY starts in ISO week 2025-W38: the week of 2026-08-10 (W33) is its 48th, the week before its 47th.
        _full_d01(spy_start=date(2025, 9, 15))
        res = store.backfill_snapshots(None, days=7)
        assert _stored_dates() == ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
        assert res == 5 and res.skipped == 2

    def test_an_etf_with_no_bars_is_not_a_failure(self, rawstore_tmp_db, fixed_today):
        _full_d01(skip={"XLE"})
        res = store.backfill_snapshots(None, days=5)
        assert res == 5 and res.failed == []
