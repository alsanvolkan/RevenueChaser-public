"""M-ROT-2 (#374, A5 + #283): the Rotation RRG reads its closes from D01, one cross-section for live and backfill.

D-M3 "weekly, both" (avo 2026-10-15): every path resamples D01's nominal daily closes to ISO weeks, the current week
closed at the latest complete session, and keeps the 1/4/12-week lookbacks. ``as_of_date`` is that session (rule B),
never the machine's ``date.today()``; a run that is not a complete session never replaces a complete stored one, and
an ETF with too little D01 history is a warning, not a partial run. Offline: a temp store, the calendar clock pinned,
a UW fake that records every call.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import calendar, ledger, rawstore
from RevenueChaser.module_sector_rotation import engine, history as rot_history, report as rot_report, store, universe
from RevenueChaser.module_sector_rotation.tests.d01_seed import ny as _at, seed as _seed, sessions as _sessions

UNIVERSE = {
    "SPY": {"name": "S&P 500", "group": "benchmark"},
    "XLK": {"name": "Technology", "group": "sector"},
    "XLF": {"name": "Financial Services", "group": "sector"},
    "XLE": {"name": "Energy", "group": "sector"},
    "SMH": {"name": "Semiconductors", "group": "theme"},
    "IBB": {"name": "Biotech", "group": "theme"},
}


class _UW:
    """Records every client call; RRG closes must not come from here."""

    def __init__(self):
        self.calls = []

    def etf_ohlc(self, ticker, days=0, candle_size="1d"):
        self.calls.append(("etf_ohlc", ticker, candle_size))
        return []

    def etf_ohlc_result(self, ticker, days=0, candle_size="1d"):
        self.calls.append(("etf_ohlc", ticker, candle_size))
        from RevenueChaser.core.providers.result import ProviderResult
        return ProviderResult("ok", [], None, "")

    def health(self):
        return {"calls": 0, "failed": 0, "auth_error": None, "failures": []}


@pytest.fixture
def world(rawstore_tmp_db, monkeypatch):
    """A six-symbol rotation universe (no macro series) and a pinned clock; ``world.now(...)`` moves it."""
    ledger.take_notes()
    for mod in (engine, store, rot_history, universe):
        monkeypatch.setattr(mod, "ROTATION_ETFS", UNIVERSE)
    monkeypatch.setattr(engine, "MACRO_SERIES", {})
    monkeypatch.setattr(store, "MACRO_SERIES", {})
    state = SimpleNamespace(now=_at(2026, 10, 7, 18))            # Wednesday, after the close

    def _now():
        return state.now.astimezone(timezone.utc)
    monkeypatch.setattr(calendar, "_now", _now)
    monkeypatch.setattr(runners, "run_id", lambda: "job-a5")
    monkeypatch.setattr(runners, "time", SimpleNamespace(time=lambda: _now().timestamp()))
    held = [{"ticker": "AAA", "name": "a", "weight": 1.0, "close": 1.0, "rs_1w": 0.0, "rs_1m": 0.0, "rs_3m": 0.0}]
    monkeypatch.setattr(engine, "fetch_all_holdings",
                        lambda uw, snaps: {s.ticker: held for s in snaps if s.group_name == "theme"})
    monkeypatch.setattr(engine, "sector_etf_trees", lambda: {})
    monkeypatch.setattr(rot_report, "export_rotation_dashboard", lambda *a, **k: runners.ROTATION_LATEST)

    def move(when: datetime):
        state.now = when
    state.move = move
    return state


def _stored(day: date) -> list[dict]:
    return (store.load_snapshot(day) or {}).get("etfs") or []


def _strip(rows: list[dict]) -> list[dict]:
    return sorted(({k: v for k, v in r.items() if k not in ("generated_at", "source")} for r in rows),
                  key=lambda r: r["ticker"])


# ── one cross-section, live and backfill ─────────────────────────────────────────────────────────────────────────────

def test_live_and_backfill_give_identical_rows_for_the_same_closes(world):
    for t in UNIVERSE:
        _seed(t, date(2026, 10, 7))
    runners.run_rotation_latest(_UW())
    live = _stored(date(2026, 10, 7))
    assert len(live) == 5 and {r["source"] for r in live} == {"live"}

    with rawstore.connect() as conn:
        conn.execute("DELETE FROM rotation_snapshots")
    written = store.backfill_weekly(_UW(), date(2026, 10, 5), date(2026, 10, 9))
    assert written == 1
    back = _stored(date(2026, 10, 7))
    assert {r["source"] for r in back} == {"backfill"}
    assert _strip(back) == _strip(live)


def test_the_live_cross_section_contains_no_macro_symbols(world):
    for t in UNIVERSE:
        _seed(t, date(2026, 10, 7))
    snaps = engine.RotationEngine(_UW()).process_all()
    tickers = {s.ticker for s in snaps}
    assert not tickers & set(universe.MACRO_SERIES)
    assert tickers == set(engine.rrg_tickers())


def test_live_and_backfill_produce_the_same_symbol_set(world):
    for t in UNIVERSE:
        _seed(t, date(2026, 10, 7))
    live = {s.ticker for s in engine.RotationEngine(_UW()).process_all()}
    with rawstore.connect() as conn:
        conn.execute("DELETE FROM rotation_snapshots")
    store.backfill_weekly(_UW(), date(2026, 10, 5), date(2026, 10, 9))
    back = {r["ticker"] for r in _stored(date(2026, 10, 7))}
    assert live == back


def test_the_cross_section_uses_weekly_lookbacks_on_iso_weeks():
    """The partial week is closed at the session; lookbacks are 1/4/12 weeks, not 5/21/63 sessions."""
    days = _sessions(date(2025, 6, 2), date(2026, 10, 7))
    spy = [(d, 100.0) for d in days]
    rising = [(d, 100.0 + i) for i, d in enumerate(days)]
    weeks = engine.weekly_closes(rising, date(2026, 10, 7))
    assert weeks[-1] == rising[-1][1]                          # the current week closes at the session
    friday = date(2026, 10, 2)
    assert weeks[-2] == dict(rising)[friday]                   # each past ISO week closes on its last session
    assert len(weeks) == len({d.isocalendar()[:2] for d, _ in rising})
    snaps = engine.cross_section({"XLK": weeks}, engine.weekly_closes(spy, date(2026, 10, 7)), date(2026, 10, 7))
    (s,) = snaps
    assert s.as_of_date == date(2026, 10, 7)
    assert s.rs_1w == round((weeks[-1] / weeks[-2] - 1) * 100, 2)
    assert s.rs_3m == round((weeks[-1] / weeks[-13] - 1) * 100, 2)


# ── the session date (#283) ──────────────────────────────────────────────────────────────────────────────────────────

def test_a_saturday_run_writes_fridays_session(world):
    world.move(_at(2026, 10, 10, 12))                          # Saturday noon in New York
    for t in UNIVERSE:
        _seed(t, date(2026, 10, 9))
    runners.run_rotation_latest(_UW())
    assert store.list_snapshot_dates() == ["2026-10-09"]


def test_a_run_before_the_nightly_wrote_the_session_is_dated_by_its_data(world):
    """D01 has no bar of the latest complete session yet: the RRG is the previous session's, and says so."""
    world.move(_at(2026, 10, 7, 17))                           # after Wednesday's close, D01 still ends Tuesday
    for t in UNIVERSE:
        _seed(t, date(2026, 10, 6))
    with pytest.raises(JobMessage, match="2026-10-07"):
        runners.run_rotation_latest(_UW())
    assert store.list_snapshot_dates() == ["2026-10-06"]


def test_an_intraday_run_does_not_replace_a_complete_stored_session(world):
    for t in UNIVERSE:
        _seed(t, date(2026, 10, 6))
    world.move(_at(2026, 10, 6, 18))                           # Tuesday evening: a complete run of Tuesday
    runners.run_rotation_latest(_UW())
    before = _stored(date(2026, 10, 6))
    assert len(before) == 5

    world.move(_at(2026, 10, 7, 11))                           # Wednesday 11:00 ET: the session is open
    with pytest.raises(JobMessage, match="kept the stored"):
        runners.run_rotation_latest(_UW())
    assert _stored(date(2026, 10, 6)) == before                # not rewritten: same generated_at, same rows
    assert store.list_snapshot_dates() == ["2026-10-06"]


def test_an_intraday_run_with_no_stored_session_saves_it(world):
    for t in UNIVERSE:
        _seed(t, date(2026, 10, 6))
    world.move(_at(2026, 10, 7, 11))
    runners.run_rotation_latest(_UW())
    assert len(_stored(date(2026, 10, 6))) == 5


def test_a_complete_run_replaces_its_own_session(world):
    for t in UNIVERSE:
        _seed(t, date(2026, 10, 7))
    runners.run_rotation_latest(_UW())
    first = _stored(date(2026, 10, 7))[0]["generated_at"]
    world.move(_at(2026, 10, 7, 19))
    runners.run_rotation_latest(_UW())
    assert _stored(date(2026, 10, 7))[0]["generated_at"] != first


# ── no UW candles ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_fetch_makes_no_weekly_candle_call(world):
    for t in UNIVERSE:
        _seed(t, date(2026, 10, 7))
    uw = _UW()
    runners.run_rotation_latest(uw)                                        # live
    runners.run_rotation_latest(uw, start="2026-09-01", end="2026-10-07")  # range
    assert [c for c in uw.calls if c[0] == "etf_ohlc" and c[2] == "1w"] == []
    assert [c for c in uw.calls if c[0] == "etf_ohlc" and c[1] in UNIVERSE] == []   # the RRG asks UW for nothing


# ── depth ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_etf_with_too_short_depth_is_a_warning_and_the_run_stays_complete(world):
    for t in UNIVERSE:
        if t != "IBB":
            _seed(t, date(2026, 10, 7))
    _seed("IBB", date(2026, 10, 7), start=date(2026, 4, 1))    # ~27 weeks, under the 48-week floor
    with pytest.raises(JobMessage, match="IBB"):
        runners.run_rotation_latest(_UW())
    assert {r["ticker"] for r in _stored(date(2026, 10, 7))} == {"XLK", "XLF", "XLE", "SMH"}
    (head,) = rawstore.query("SELECT * FROM result_runs WHERE board = 'module_sector_rotation'", db="history")
    assert head["quality"] == "complete", head["quality_note"]
    assert "IBB" in json.dumps(json.loads(head["warnings"]))
    assert json.loads(head["context"])["missing"] == []


def test_an_etf_with_no_d01_series_is_still_a_partial_run(world):
    for t in UNIVERSE:
        if t != "IBB":
            _seed(t, date(2026, 10, 7))
    with pytest.raises(JobMessage, match="IBB"):
        runners.run_rotation_latest(_UW())
    (head,) = rawstore.query("SELECT * FROM result_runs WHERE board = 'module_sector_rotation'", db="history")
    assert head["quality"] == "partial" and "IBB" in head["quality_note"]


# ── backfill ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_backfill_writes_sessions_not_weekends_or_holidays(world):
    for t in UNIVERSE:
        _seed(t, date(2026, 9, 11))
    world.move(_at(2026, 9, 12, 12))                           # Saturday; Monday 2026-09-07 was Labor Day
    store.backfill_snapshots(_UW(), days=5)
    got = store.list_snapshot_dates()
    assert got == ["2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]


def test_a_range_never_writes_a_session_that_is_not_complete(world):
    for t in UNIVERSE:
        _seed(t, date(2026, 10, 6))
    world.move(_at(2026, 10, 7, 11))                           # Wednesday intraday: Wednesday is not written
    res = store.backfill_weekly(_UW(), date(2026, 9, 28), date(2026, 10, 9))
    assert store.list_snapshot_dates() == ["2026-09-30"]
    assert res == 1


# ── the Earnings dependency (rotation_quadrant_map) ──────────────────────────────────────────────────────────────────

def test_the_quadrant_map_computes_and_stores_the_session(world):
    world.move(_at(2026, 10, 10, 12))                          # Saturday: the session is Friday
    for t in UNIVERSE:
        _seed(t, date(2026, 10, 9))
    m = engine.rotation_quadrant_map(uw=_UW())
    assert set(m) == {"XLK", "XLF", "XLE", "SMH", "IBB"}
    assert store.list_snapshot_dates() == ["2026-10-09"]
    assert engine.rotation_quadrant_map(uw=None, compute=False) == m      # the stored session is reused


def test_the_quadrant_map_never_replaces_a_stored_session_from_an_incomplete_run(world):
    for t in UNIVERSE:
        _seed(t, date(2026, 10, 6))
    world.move(_at(2026, 10, 6, 18))
    runners.run_rotation_latest(_UW())
    before = _stored(date(2026, 10, 6))
    world.move(_at(2026, 10, 7, 17))                           # Wednesday's session is complete, D01 ends Tuesday
    m = engine.rotation_quadrant_map(uw=_UW())
    assert m == {r["ticker"]: r["quadrant"] for r in before}
    assert _stored(date(2026, 10, 6)) == before and store.list_snapshot_dates() == ["2026-10-06"]
