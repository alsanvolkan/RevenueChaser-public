"""U1 (#402): a replay of the 2026-10-14 night, when Swing and Fundamentals did not freeze.

What happened (#402 comment, 2026-10-14 22:05Z): RC-Daily-Bars wrote the 14 Oct bars into D01 (SPY included). Then,
in RC-Screeners-Daily, Yahoo refused the session's crumb (HTTP 401 "Invalid Crumb", 21:29Z) and every later yfinance
refetch failed, SPY's at 21:32Z. Both boards were built on a stale SPY baseline and the freeze refused them: "SPY's
bars were stale, so the board's baseline is stale".

The replay runs both real scans (``run_scan``, a Fetch) and the real freezes on the scenario of
``core/tests/screen_parity.py`` (bars ending on 14 Oct, the clock at 21:45Z) with a fake Yahoo that refuses every read.
Since C12 and K8b (#383 R3) the picks score from D01 and the freeze checks their D01 bars, so Yahoo's refusal no longer
touches the picks; since PR 10 (G4) "D01" is the prices store (``prices.duckdb``) and the SPY fallback (``load_ohlc``)
is gone, so Yahoo's refusal reaches only the ``.info`` reads. Three shapes:

- **the night as it was:** D01 held every name's 14 Oct bar: both boards freeze and no yfinance bar is read.
- **D01 a day behind for the picks:** the freeze refuses and names them; a Fetch after Daily Bars can complete it.
- **the prices store a day behind for SPY:** SPY's baseline is stale and both are refused for it; no yfinance bar is
  read (before PR 10 a fallback refetch, cured by one session reset, froze both).

Yahoo refuses while yfinance's shared crumb is the one it stopped accepting (``YfData()._crumb``). Offline; the store is
a temp DuckDB and yfinance's on-disk cookie cache is faked.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.tests import screen_parity as sc
from RevenueChaser.module_fundamentals import freeze as fund_freeze
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_swing_potential import freeze as swing_freeze
from RevenueChaser.module_swing_potential import pipeline as sp
from RevenueChaser.rcdata.tests import ledger_queue

D = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 21, 45, tzinfo=timezone.utc)                      # 17:45 EDT
NIGHT_BEFORE = datetime(2026, 10, 13, 21, 0, tzinfo=timezone.utc).timestamp()  # the 13 Oct run's cache

CRUMB_401 = 'HTTP Error 401: {"finance":{"result":null,"error":{"code":"Unauthorized","description":"Invalid Crumb"}}}'


def _dated(df: pd.DataFrame, end: date) -> pd.DataFrame:
    """The scenario's bars, re-dated so the last one is ``end``."""
    out = df.copy()
    out.index = pd.bdate_range(end=end, periods=len(out))
    return out


def _bars(ticker: str, end: date = D) -> pd.DataFrame:
    if ticker == "SHORT1":
        return _dated(sc._bars("up", 0.004, sc.SHORT_BARS), end)
    kind, drift = sc.BARS[ticker]
    return _dated(sc._bars(kind, drift, sc.LONG_BARS), end)


def _d01_day_behind(*tickers: str) -> None:
    """``tickers``' D01 series as the 13 Oct night left it: ending 13 Oct, written that evening."""
    with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
        for t in tickers:
            conn.execute("DELETE FROM price_bars_daily WHERE ticker = ?", [t])
    for t in tickers:
        sc.seed_d01(t, _bars(t, date(2026, 10, 13)), fetched_at=NIGHT_BEFORE)


STALE_CRUMB = "stale-crumb"


class Yahoo:
    """Yahoo after 21:29Z: every read refused while yfinance's crumb is the old one. Like yfinance, nothing raises. A
    refused ``.info`` logs the 401 on the ``yfinance`` logger (yfinance's quote scraper does, ``str(e) +
    e.response.text``) and has no fields; a refused ``history`` logs Yahoo's reason the way yfinance surfaces a chart
    error ("<ticker>: Invalid Crumb"; an assumption: no PC log line of a refused bar read is on file) and is an
    empty frame. ``refuse_new_crumb`` keeps refusing after a reset (a rate limit, not the crumb)."""

    def __init__(self, shared):
        self.shared = shared
        self.refuse_new_crumb = False
        self.history_calls: list[str] = []

    @property
    def answering(self) -> bool:
        return self.shared._crumb != STALE_CRUMB and not self.refuse_new_crumb

    def ticker(self, symbol):
        yahoo = self

        class _Ticker:
            def history(self, *a, **k):
                yahoo.history_calls.append(symbol)
                if yahoo.answering:
                    return _bars(symbol)
                logging.getLogger("yfinance").error(f"{symbol}: Invalid Crumb")
                return pd.DataFrame()

            @property
            def info(self):
                if yahoo.answering:
                    return {"longName": f"{symbol} Holdings", "industry": "Software - Infrastructure"}
                logging.getLogger("yfinance").error(CRUMB_401)
                return {"trailingPegRatio": None}

        return _Ticker()


class _CookieStore:
    def store(self, key, cookie):
        pass


@pytest.fixture
def night(rawstore_tmp_db, monkeypatch):
    """The store as RC-Screeners-Daily found it on 14 Oct (D01 and D04 written), the clock at 21:45Z, Yahoo
    refusing."""
    monkeypatch.setattr(time, "time", lambda: NOW.timestamp())
    sc.patch(monkeypatch)
    from yfinance import cache as yf_cache
    from yfinance import data as yf_data
    shared = yf_data.YfData()
    monkeypatch.setattr(shared, "_crumb", STALE_CRUMB)
    monkeypatch.setattr(shared, "_cookie", True)
    monkeypatch.setattr(yf_cache, "get_cookie_cache", lambda: _CookieStore())
    yahoo = Yahoo(shared)
    monkeypatch.setattr("yfinance.Ticker", yahoo.ticker)
    sc.seed()
    return yahoo


def _fetch_and_freeze():
    ledger.take_notes()
    out = {}
    for name, pipe, frz in (("swing", sp, swing_freeze), ("fundamentals", fp, fund_freeze)):
        payload = pipe.run_scan()
        outcome = frz.freeze_payload(payload, run_id=f"replay-{name}", now=NOW)
        out[name] = (payload, outcome)
    return out


def test_the_night_as_it_was_both_boards_freeze_from_d01_and_read_no_yahoo_bar(night):
    """Before U1 and C12: both refused, "SPY's bars were stale, so the board's baseline is stale"."""
    night.refuse_new_crumb = True
    out = _fetch_and_freeze()
    for name, (payload, outcome) in out.items():
        assert payload["data_quality"]["spy_stale"] is False, name
        assert payload["data_quality"]["stale_ohlc"] == 0, name
        assert outcome.queued == len(payload["candidates"]) > 0, (name, outcome.message)
        assert outcome.forecast_date == D
    assert night.history_calls == []                        # picks and SPY come from D01: no yfinance bar read


def test_d01_a_day_behind_for_the_picks_refuses_and_names_them(night):
    """Option A on D01: the boards scored these picks from 13 Oct bars, so 14 Oct is not frozen; the reason names
    the picks. Every name lacks the bar SPY has: a Daily Bars failure, so none drops as ``stale_bars`` (the board is
    not emptied), and only a Daily Bars rerun can complete the day (#434 review S1, S2)."""
    night.refuse_new_crumb = True
    _d01_day_behind(*(t for t in sc.BARS if t != "SPY"))
    out = _fetch_and_freeze()
    for name, (payload, outcome) in out.items():
        assert payload["data_quality"]["spy_stale"] is False, name
        assert outcome.queued == 0, name
        assert "lack a complete 2026-10-14 daily bar" in outcome.reason, (name, outcome.reason)
        assert "a later Fetch will not change it unless the daily bars are read again" in outcome.reason
        assert "stale_bars" not in {s["key"] for s in payload["funnel_steps"]}
    assert ledger_queue.count() == 0
    assert night.history_calls == []


def test_the_prices_store_a_day_behind_for_spy_refuses_both_for_spy_and_reads_no_yahoo_bar(night):
    """Since PR 10 (G4) there is no SPY fallback (F4 A, part 1): before, the baseline fell back to ``load_ohlc`` (the
    old cache and a yfinance refetch). Now a prices store without SPY's session is a stale baseline, both boards are
    refused for it, and Yahoo is never asked for a bar, cured crumb or not."""
    _d01_day_behind("SPY")
    out = _fetch_and_freeze()
    for name, (payload, outcome) in out.items():
        assert payload["data_quality"]["spy_stale"] is True, name
        assert outcome.queued == 0 and "SPY" in outcome.reason, (name, outcome.reason)
    assert night.history_calls == []
