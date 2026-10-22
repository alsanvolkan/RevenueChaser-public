"""D03: the SPX snapshot reads the stored ``index_levels_daily`` levels and never fetches candles itself.

The store is the single source of the 8-week candle window (``core/index_levels.py`` writes it from yfinance
``^GSPC`` once a night). A snapshot run must therefore reach no provider for candles: the socket guard in the root
``conftest.py`` would turn such a call into a ``NetworkAccessAttempted`` warning and an empty window.
"""
from __future__ import annotations

import json
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
    _seed_levels()
    client = _FakeClient()
    snap.fetch_snapshot(as_uw(client), DAY, data_root=tmp_path, vix_fetcher=lambda: {"tickers": {}, "vix": 15.5},
                        cftc_fetcher=lambda: None)
    spot = [c for c in client.calls if c[0].endswith("/spot-exposures/strike")]
    assert spot and spot[0][1]["min_strike"] == int(7757.0 * 0.75)


def test_an_empty_store_warns_and_writes_an_empty_window_rather_than_reaching_out(tmp_path, rawstore_tmp_db):
    raw = _fetch(tmp_path)
    man = json.loads((tmp_path / DAY / "manifest.json").read_text(encoding="utf-8"))
    assert raw["ohlc"]["data"] == []
    assert any("index levels" in w for w in man["warnings"])


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
