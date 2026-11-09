"""Opus review of MW-ROT-A (#374, M-ROT-2/3), 2026-10-16: the D01 inputs line up by session.

- The RRG lines an ETF's ISO-week closes up with SPY's weeks: a week D01 lacks for the ETF does not shift its 4- and
  12-week lookbacks onto other weeks, and an ETF whose D01 series stops before the session's week is not plotted as
  of that session (it is missing, so the run is partial).
- A split inside the window does not move the RRG (the prices store's split basis applies it on read, since PR 9).
- Money Flow does not rank an ETF whose D01 series stops before the session (missing, so the result is partial).
- The range backfill names the ETFs it left out of the sessions it wrote.
Offline: a temp store, the calendar clock pinned.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import rawstore
from RevenueChaser.module_sector_rotation import engine, money_flow as mf, store
from RevenueChaser.module_sector_rotation.tests.bars_seed import ny, pin_clock, seed as _seed, sessions as _sessions
from RevenueChaser.rcdata import read
from RevenueChaser.rcdata.tests import prices_store
from RevenueChaser.module_sector_rotation.tests.test_rotation_a5_374 import UNIVERSE, _UW, _stored, world  # noqa: F401

SESSION = date(2026, 10, 7)


def _bars(ticker: str, days: list[date], close, fetched_at: float = 0.0) -> None:
    prices_store.put_bars([
        {"ticker": ticker, "session_date": d, "open": close(d), "high": close(d), "low": close(d), "close": close(d),
         "volume": 1e6, "fetched_at": fetched_at} for d in days], replace=False, overwrite=True)


def _rising(d: date) -> float:
    return 100.0 + (d - date(2025, 1, 1)).days * 0.1


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ── the RRG lines weeks up with SPY's ─────────────────────────────────────────────────────────────────────────────────

def test_a_week_missing_from_an_etf_series_does_not_shift_its_lookbacks(world):
    days = _sessions(date(2025, 3, 3), SESSION)
    gap = _week_start(SESSION) - timedelta(weeks=6)          # one whole ISO week, between the 4- and 12-week points
    for t in UNIVERSE:
        if t != "XLK":
            _seed(t, SESSION)
    _bars("XLK", [d for d in days if _week_start(d) != gap], _rising)
    snaps = {s.ticker: s for s in engine.RotationEngine(_UW()).process_all()}
    spy = engine.weekly_closes([(d, c) for d, c in engine.bar_points(["SPY"], days[0], SESSION)["SPY"]], SESSION)

    def week_close(weeks_back: int) -> float:
        monday = _week_start(SESSION) - timedelta(weeks=weeks_back)
        return _rising(max(d for d in days if _week_start(d) == monday))
    want_3m = round(((week_close(0) / week_close(12) - 1) - (spy[-1] / spy[-13] - 1)) * 100, 2)
    want_1m = round(((week_close(0) / week_close(4) - 1) - (spy[-1] / spy[-5] - 1)) * 100, 2)
    assert snaps["XLK"].rs_1m == want_1m
    assert snaps["XLK"].rs_3m == want_3m
    assert store.backfill_weekly(_UW(), SESSION, SESSION) == 1           # the backfill lines weeks up the same way
    back = {r["ticker"]: r for r in _stored(SESSION)}
    assert (back["XLK"]["rs_1m"], back["XLK"]["rs_3m"]) == (want_1m, want_3m)


def test_an_etf_whose_series_stops_before_the_sessions_week_is_not_plotted_as_of_it(world):
    for t in UNIVERSE:
        if t != "IBB":
            _seed(t, SESSION)
    _seed("IBB", date(2026, 9, 18))                          # the series stops three weeks before the session
    with pytest.raises(JobMessage, match="IBB"):
        runners.run_rotation_latest(_UW())
    assert "IBB" not in {r["ticker"] for r in _stored(SESSION)}
    (head,) = rawstore.query("SELECT * FROM result_runs WHERE board = 'module_sector_rotation'", db="history")
    assert head["quality"] == "partial" and "IBB" in head["quality_note"]


def test_a_split_inside_the_window_does_not_move_the_rrg(world):
    for t in UNIVERSE:
        _seed(t, SESSION)
    plain = {s.ticker: (s.rs_1w, s.rs_1m, s.rs_3m) for s in engine.RotationEngine(_UW()).process_all()}
    ex = date(2026, 8, 3)                                      # inside the 12-week lookback
    rows = read.ticker_bars(["XLK"]).to_dict("records")
    for r in rows:                                             # as traded before a 2:1 split: twice the price
        r["session_date"] = r["session_date"].date()
        if r["session_date"] < ex:
            for k in ("open", "high", "low", "close"):
                r[k] = r[k] * 2
    prices_store.put_bars(rows)
    prices_store.put_action("XLK", ex, "split", 2.0)
    split = {s.ticker: (s.rs_1w, s.rs_1m, s.rs_3m) for s in engine.RotationEngine(_UW()).process_all()}
    assert split == plain


# ── Money Flow ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_money_flow_does_not_rank_an_etf_whose_series_stops_before_the_session(rawstore_tmp_db, monkeypatch):
    session = date(2026, 8, 14)
    pin_clock(monkeypatch, ny(2026, 8, 14, 18))
    ranked = mf.ranked_tickers()
    stale = ranked[0]
    for k, t in enumerate(ranked):
        end = date(2026, 7, 24) if t == stale else session
        _seed(t, end, start=date(2026, 5, 1), closes=(lambda i: 100.0 + i) if k % 2 else (lambda i: 200.0 - i))
    res = mf.compute_money_flow(days=30, window=21, top_n=len(ranked))
    assert res["partial"] is True and res["missing"] == [stale]
    assert stale not in {r["ticker"] for r in res["inflows"] + res["outflows"]}
    assert res["as_of"] == session.isoformat()


# ── backfill ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_backfill_names_the_etfs_it_left_out(world):
    for t in UNIVERSE:
        if t != "IBB":
            _seed(t, SESSION)
    res = store.backfill_weekly(_UW(), date(2026, 9, 28), date(2026, 10, 7))
    assert res == 2
    assert "IBB" in res.message()
    with rawstore.connect() as conn:
        conn.execute("DELETE FROM rotation_snapshots")
    with pytest.raises(JobMessage, match="IBB"):
        runners.run_rotation_latest(_UW(), start="2026-09-28", end="2026-10-07")
    (head,) = rawstore.query("SELECT * FROM result_runs WHERE board = 'module_sector_rotation'", db="history")
    assert "IBB" in json.dumps(json.loads(head["warnings"]))
