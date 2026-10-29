"""PR 9 (G3): Sector Rotation reads its weekly closes from the prices store and its macro series from
``market_series`` (FRED, Cboe; MOVE and wheat from the new Yahoo source), with no call at Fetch time.

The RRG, the holdings RS and Money Flow read ``rcdata.read.ticker_bars`` (split basis, the old D01 "nominal"); the
11 macro series are ``rcdata.read.market_series`` rows lined up with SPY's weeks, so a Refresh and a Fetch compute
the same map offline. The old D01 tables are not read. Offline, temp stores.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.core import calendar, rawstore
from RevenueChaser.core.rotation_reference import MACRO_SERIES
from RevenueChaser.module_sector_rotation import engine, money_flow
from RevenueChaser.module_sector_rotation.tests.d01_seed import ny, pin_clock, sessions
from RevenueChaser.rcdata.tests import prices_store

SESSION = date(2026, 6, 30)
DAYS = sessions(date(2025, 3, 3), SESSION)


def _bars(ticker, step=0.001, start=100.0):
    return [{"ticker": ticker, "session_date": d, "close": start * (1 + step) ** i, "volume": 1e6 + i}
            for i, d in enumerate(DAYS)]


@pytest.fixture
def world(monkeypatch):
    pin_clock(monkeypatch, ny(2026, 7, 1, 10))
    return monkeypatch


def test_the_rrg_reads_the_prices_store_not_d01(world):
    world.setattr(engine, "MACRO_SERIES", {})
    rawstore.write_rows("price_bars_daily", [{
        "ticker": "SPY", "session_date": d, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "adj_close": None,
        "volume": 1.0, "split_ratio": None, "adj_source": None, "source": "uw", "fetched_at": 0.0, "run_id": "old",
        "run_at": 0.0} for d in DAYS])
    eng = engine.RotationEngine()
    assert eng.process_all() == []                                      # SPY only in the old D01: no map
    prices_store.put_bars(_bars("SPY") + _bars("XLK", 0.002) + _bars("XLE", -0.001), replace=False)
    snaps = {s.ticker: s for s in engine.RotationEngine().process_all()}
    assert {"XLK", "XLE"} <= set(snaps)
    assert snaps["XLK"].rs_1m > 0 > snaps["XLE"].rs_1m
    assert snaps["XLK"].close_price == pytest.approx(_bars("XLK", 0.002)[-1]["close"])


def test_a_split_is_applied_to_the_weekly_closes(world):
    world.setattr(engine, "MACRO_SERIES", {})
    xlk = _bars("XLK", 0.0)
    ex = DAYS[-30]
    for r in xlk:
        if r["session_date"] >= ex:
            r["close"] = 50.0                                            # a 2-for-1: as traded, the price halves
    prices_store.put_bars(_bars("SPY", 0.0) + xlk)
    prices_store.put_action("XLK", ex, "split", 2.0)
    snap = {s.ticker: s for s in engine.RotationEngine().process_all()}["XLK"]
    assert snap.rs_3m == pytest.approx(0.0) and snap.close_price == 50.0


def test_the_macro_series_come_from_market_series_lined_up_with_spys_weeks(world):
    prices_store.put_bars(_bars("SPY"))
    series = {ticker: info["series"] for ticker, info in MACRO_SERIES.items()}
    assert series["US10Y"] == ("DGS10", "fred") and series["VVIX"] == ("VVIX", "cboe")
    assert series["MOVE"] == ("MOVE", "yahoo") and series["WHEATF"] == ("WHEATF", "yahoo")
    rows = []
    for i, d in enumerate(DAYS):
        for sid, src in series.values():
            rows.append({"series_id": sid, "source": src, "obs_date": d, "value": 4.0 + i / 1000})
    rows.append({"series_id": "DGS10", "source": "treasury", "obs_date": DAYS[-1], "value": 99.0})   # not FRED's
    prices_store.put_series(rows)
    eng = engine.RotationEngine()
    snaps = {s.ticker: s for s in eng.process_all()}
    assert set(MACRO_SERIES) <= set(snaps)
    assert snaps["US10Y"].close_price == pytest.approx(4.0 + (len(DAYS) - 1) / 1000)
    assert snaps["MOVE"].close_price == pytest.approx(snaps["US10Y"].close_price)


def test_a_macro_series_that_stopped_is_missing_not_shifted(world):
    world.setattr(engine, "MACRO_SERIES", {"US10Y": MACRO_SERIES["US10Y"]})
    prices_store.put_bars(_bars("SPY"))
    prices_store.put_series([{"series_id": "DGS10", "obs_date": d, "value": 4.0} for d in DAYS[:-20]])
    assert "US10Y" not in {s.ticker for s in engine.RotationEngine().process_all()}


def test_a_macro_series_a_week_behind_spy_carries_its_last_week_and_is_named(world):
    """Review M1 (PR 270): ``market_series`` stores day D on D+1 at the earliest (a raw file per as-of day, FRED's own
    lag, Yahoo's 16:00 cut-off), so on the first session of an ISO week the newest macro value sits in the week before.
    That week carries forward, as a week in between does, and the engine names the series; one that stopped weeks ago
    stays missing."""
    world.setattr(engine, "MACRO_SERIES", {t: MACRO_SERIES[t] for t in ("US10Y", "VVIX")})
    prices_store.put_bars(_bars("SPY"))
    last_week = [d for d in DAYS if d.isocalendar()[1] < SESSION.isocalendar()[1]]
    assert last_week[-1] == date(2026, 6, 26) and SESSION.isocalendar()[1] == date(2026, 6, 29).isocalendar()[1]
    prices_store.put_series([{"series_id": "DGS10", "source": "fred", "obs_date": d, "value": 4.0 + i / 1000}
                             for i, d in enumerate(last_week)]
                            + [{"series_id": "VVIX", "source": "cboe", "obs_date": d, "value": 90.0}
                               for d in DAYS[:-20]])
    eng = engine.RotationEngine()
    snaps = {s.ticker: s for s in eng.process_all()}
    assert "US10Y" in snaps and "VVIX" not in snaps
    assert snaps["US10Y"].close_price == pytest.approx(4.0 + (len(last_week) - 1) / 1000)
    assert eng.carried == ["US10Y"]


def test_money_flow_reads_the_prices_store():
    prices_store.put_bars([{"ticker": "XLK", "session_date": d, "close": 10.0 + i, "volume": 5.0}
                           for i, d in enumerate(DAYS[-40:])])
    rows = money_flow.bar_rows(["XLK", "XLE"], SESSION, days=30)
    assert len(rows) == 30 and {r["ticker"] for r in rows} == {"XLK"}
    assert rows[-1] == {"ticker": "XLK", "date": SESSION.isoformat(), "close": 49.0, "volume": 5.0}


def test_the_stale_line_names_the_prices_store(world):
    world.setattr(engine, "MACRO_SERIES", {})
    short = [r for r in _bars("SPY") if r["session_date"] < SESSION]
    prices_store.put_bars(short + [r for r in _bars("XLK") if r["session_date"] < SESSION])
    eng = engine.RotationEngine()
    eng.process_all()
    assert eng.as_of == calendar.previous_trading_day(SESSION - timedelta(days=1))
    assert "prices store" in eng.stale and "D01" not in eng.stale
