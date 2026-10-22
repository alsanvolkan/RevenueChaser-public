"""D03: the SPX snapshot reads the stored ``index_levels_daily`` levels and never fetches candles itself.

The store is the single source of the 8-week candle window (``core/index_levels.py`` writes it from yfinance
``^GSPC`` once a night). A snapshot run must therefore reach no provider for candles: the socket guard in the root
``conftest.py`` would turn such a call into a ``NetworkAccessAttempted`` warning and an empty window.
"""
from __future__ import annotations

import json

import pytest
from datetime import date

from RevenueChaser.core import rawstore
from RevenueChaser.module_spx_gamma import snapshot as snap

from .conftest import as_uw

DAY = "2026-08-14"
CANDLE_DATES = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
                "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]


class _FakeClient:
    """A ``UWClient`` stand-in: every endpoint answers 200 with an empty payload."""

    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path.endswith("/gex-levels"):
            return {"data": {"call_wall": "7900", "gamma_flip": "7820.63", "gamma_magnet": "7900",
                             "put_wall": "7750"}}
        if path.endswith("/greek-exposure/expiry"):
            return {"data": [{"expiry": DAY, "dte": 0}]}
        return {"data": []}


def _seed_levels(dates=CANDLE_DATES) -> None:
    rawstore.write_rows("index_levels_daily", [
        {"symbol": "^GSPC", "session_date": date.fromisoformat(d), "open": 7740.0 + i, "high": 7755.0 + i,
         "low": 7725.0 + i, "close": 7748.0 + i, "volume": 1_000_000 + i, "source": "yfinance",
         "fetched_at": 1.0, "run_id": "seed", "run_at": 1.0}
        for i, d in enumerate(dates)])


@pytest.fixture(autouse=True)
def _market_closed(monkeypatch):
    """Spot goes live during an OPEN session (the owner's choice, 2026-10-22), so every test here pins the
    session shut unless it says otherwise. Without this the file passes or fails by time of day."""
    from RevenueChaser.core import calendar as nyse_calendar
    monkeypatch.setattr(nyse_calendar, "in_us_session_window", lambda *a, **k: False)


def _fetch(tmp_path, **kw):
    kw.setdefault("vix_fetcher", lambda: {"tickers": {}, "vix": 15.5})
    kw.setdefault("cftc_fetcher", lambda: None)
    return snap.fetch_snapshot(as_uw(_FakeClient()), DAY, data_root=tmp_path, **kw)


def test_the_snapshot_builds_its_candle_window_from_the_stored_levels(tmp_path, rawstore_tmp_db):
    _seed_levels()
    raw = _fetch(tmp_path)
    ohlc = json.loads((tmp_path / DAY / "ohlc_8wk.json").read_text(encoding="utf-8"))
    assert [c["date"] for c in ohlc["data"]] == CANDLE_DATES
    assert ohlc["source"].startswith("index_levels_daily")
    assert "yfinance" not in ohlc["source"]
    assert ohlc["data"][-1]["close"] == 7757.0
    assert raw["ohlc"] == ohlc


def test_the_snapshot_spot_comes_from_the_newest_stored_level(tmp_path, rawstore_tmp_db):
    """With the market closed there is no live spot, so the strike window rests on the newest stored close."""
    _seed_levels()
    client = _FakeClient()
    snap.fetch_snapshot(as_uw(client), DAY, data_root=tmp_path, vix_fetcher=lambda: {"tickers": {}, "vix": 15.5},
                        cftc_fetcher=lambda: None)
    spot = [c for c in client.calls if c[0].endswith("/spot-exposures/strike")]
    assert spot and spot[0][1]["min_strike"] == int(7757.0 * 0.75)


def test_an_empty_store_warns_and_writes_an_empty_window_rather_than_reaching_out(tmp_path, rawstore_tmp_db,
                                                                                  monkeypatch):
    """#293 review N-6: nothing asserted that the snapshot reaches no provider for candles. The whole point of
    reading the store is that an empty window costs no request, so count yfinance.Ticker constructions."""
    import yfinance

    built: list = []
    real = yfinance.Ticker

    class _Counted(real):                      # a subclass, so anything that does reach it still works
        def __init__(self, *a, **kw):
            built.append(a[0] if a else None)
            super().__init__(*a, **kw)

    monkeypatch.setattr(yfinance, "Ticker", _Counted)
    raw = _fetch(tmp_path)
    man = json.loads((tmp_path / DAY / "manifest.json").read_text(encoding="utf-8"))
    assert raw["ohlc"]["data"] == []
    assert any("index levels" in w for w in man["warnings"])
    assert built == [], f"the snapshot built yfinance.Ticker for {built} with the market closed"
    assert man["uw_ohlc_attempted"] is False   # no UW index-OHLC request either (#293 review N-3)


def test_a_window_that_ends_before_the_last_complete_session_warns(monkeypatch):
    """#293 review MUST: the only guard was "no rows at all". A window that merely ends early — the nightly
    --index-levels step missed a session — passed silently with a healthy source label, which is the default
    same-day Fetch path. It must say so."""
    from RevenueChaser.core import calendar as nyse_calendar
    from RevenueChaser.module_spx_gamma import candles

    rows = [{"date": d, "close": 1.0} for d in CANDLE_DATES]
    monkeypatch.setattr(nyse_calendar, "latest_complete_session", lambda *a, **k: date(2026, 8, 18))
    expected, last = candles.window_lag(rows, "2026-08-18")
    assert (expected, last) == ("2026-08-18", "2026-08-14")


def test_a_window_up_to_the_last_complete_session_does_not_warn(monkeypatch):
    """The same-day Fetch is legitimate: the store holds complete sessions only (rule B), so ending on the last
    complete session is correct and must stay silent."""
    from RevenueChaser.core import calendar as nyse_calendar
    from RevenueChaser.module_spx_gamma import candles

    rows = [{"date": d, "close": 1.0} for d in CANDLE_DATES]
    monkeypatch.setattr(nyse_calendar, "latest_complete_session", lambda *a, **k: date(2026, 8, 14))
    assert candles.window_lag(rows, "2026-08-17") == (None, None)


def test_the_window_is_sorted_whatever_order_the_store_returns(monkeypatch):
    """#293 review S-1: spot is candles[-1]["close"] and price_window_sessions does not re-sort, so order is
    load-bearing. Base sorted explicitly; after the switch it rested on an ORDER BY in another module, which a
    test seeding ascending rows could not catch."""
    from RevenueChaser.core import index_levels
    from RevenueChaser.module_spx_gamma import candles

    shuffled = [{"date": d, "close": float(i)} for i, d in enumerate(reversed(CANDLE_DATES))]
    monkeypatch.setattr(index_levels, "levels_between", lambda *a, **k: list(shuffled))
    out = candles.stored_spx_candles(DAY)
    assert [r["date"] for r in out] == sorted(CANDLE_DATES)
    assert out[-1]["date"] == max(CANDLE_DATES), "spot would read the wrong session"


def test_live_spot_is_read_only_while_the_session_is_open(monkeypatch):
    """The owner's choice, 2026-10-22: the 8-week window stays a store read, but spot goes live during an open
    session, because it sets the +/-25% strike window and the board's current_level."""
    from RevenueChaser.core import calendar as nyse_calendar
    from RevenueChaser.module_spx_gamma import candles

    monkeypatch.setattr(nyse_calendar, "in_us_session_window", lambda *a, **k: False)
    assert candles.live_spot() == (None, None), "a closed market must cost no request"


def test_a_failed_live_spot_reports_its_reason(monkeypatch):
    """A failed live read must never be silent: the caller falls back to the last complete close and says so."""
    import yfinance

    from RevenueChaser.core import calendar as nyse_calendar
    from RevenueChaser.module_spx_gamma import candles

    monkeypatch.setattr(nyse_calendar, "in_us_session_window", lambda *a, **k: True)

    def boom(*a, **kw):
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(yfinance, "Ticker", boom)
    spot, error = candles.live_spot()
    assert spot is None and "yfinance down" in error


def test_the_board_prefers_the_recorded_live_spot(monkeypatch):
    """normalize recomputed current_level from the candles, which would have thrown the live spot away."""
    from RevenueChaser.module_spx_gamma import normalize

    candles_rows = [{"date": d, "close": 100.0} for d in CANDLE_DATES]
    assert normalize.current_level(candles_rows) == 100.0
    raw = {"ohlc": {"source": "index_levels_daily (^GSPC)", "data": candles_rows},
           "spot": {"source": "yfinance (live)", "value": 123.45}}
    recorded = (raw.get("spot") or {}).get("value")
    assert (recorded if recorded is not None else normalize.current_level(candles_rows)) == 123.45


def test_an_open_session_spends_exactly_one_request_on_spot(tmp_path, rawstore_tmp_db, monkeypatch):
    """The window stays a store read; spot is the one live call, and it moves the strike window with it."""
    import yfinance

    from RevenueChaser.core import calendar as nyse_calendar

    _seed_levels()
    monkeypatch.setattr(nyse_calendar, "in_us_session_window", lambda *a, **k: True)
    built: list = []
    # snapshot.py imports the name, so the binding to patch is the one in that module
    monkeypatch.setattr(snap, "live_spot", lambda *a, **k: (built.append(1), (8000.0, None))[1])
    client = _FakeClient()
    snap.fetch_snapshot(as_uw(client), DAY, data_root=tmp_path, vix_fetcher=lambda: {"tickers": {}, "vix": 15.5},
                        cftc_fetcher=lambda: None)
    assert len(built) == 1, "spot must cost exactly one live read per snapshot"
    spot_calls = [c for c in client.calls if c[0].endswith("/spot-exposures/strike")]
    assert spot_calls and spot_calls[0][1]["min_strike"] == int(8000.0 * 0.75), "the live spot must move the window"
    assert yfinance is not None
