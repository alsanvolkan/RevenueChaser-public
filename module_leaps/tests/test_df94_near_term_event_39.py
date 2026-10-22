"""DF94 (#39): a pinned name's near-event flag comes from the stored earnings calendar, not only from a flow print.

Red-first for the five behaviours:
1. a watchlist ticker with no far-dated print and earnings within ``NEAR_EVENT_DAYS`` is a near event, so Quiet;
2. the same ticker with earnings beyond the window keeps the label its footprint earns;
3. a flow candidate keeps its own near-event flag, whether the calendar has no row or a later one;
4. the window is inclusive at exactly ``NEAR_EVENT_DAYS`` days, and excludes the next day;
5. the Refresh (cached path) keeps the recorded flag and never consults today's calendar.
"""
from datetime import date, timedelta

import pytest

from RevenueChaser.core import earnings_dates
from RevenueChaser.core import rawstore
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.module_leaps import engine
from RevenueChaser.module_leaps import history as leaps_history
from RevenueChaser.module_leaps.scan import scan_leaps, scan_leaps_cached
from RevenueChaser.module_leaps.tests.test_leaps_l3 import run_at_of, seed_session

TODAY = date(2026, 10, 6)
FAR = "2027-12-17"                       # > 270 days after TODAY
GROWING = (1000, 200)                    # today's far-dated OI, against the seeded 500/0 session


class _UW:
    """A UW double: far-dated OI per ticker, one tape page, names from the screener."""
    auth_error = None

    def __init__(self, oi, tape=()):
        self.oi, self.tape = oi, list(tape)
        self.calls = self.failed = 0

    def health(self):
        return {"calls": self.calls, "failed": self.failed, "auth_error": None, "failures": [],
                "breaker_open": False, "skipped_calls": 0}

    def option_trades(self, **kw):
        return list(self.tape)

    def screener_batch(self, tickers):
        return {t: {"full_name": f"{t} Inc"} for t in tickers}

    def oi_per_expiry(self, ticker, date=None):
        self.calls += 1
        call, put = self.oi.get(ticker, (0, 0))
        return [{"expiry": FAR, "call_oi": call, "put_oi": put}]

    def flow_per_expiry(self, ticker):
        self.calls += 1
        return []

    def iv_rank(self, ticker):
        self.calls += 1
        return [{"iv_rank_1y": "55.0"}]

    def oi_per_expiry_result(self, ticker, date=None):
        return ProviderResult("ok", self.oi_per_expiry(ticker, date))

    def flow_per_expiry_result(self, ticker):
        return ProviderResult("ok", self.flow_per_expiry(ticker))

    def iv_rank_result(self, ticker):
        return ProviderResult("ok", self.iv_rank(ticker))


def _print(pid, ticker, ned=None):
    return {"id": pid, "underlying_symbol": ticker, "strike": "200", "expiry": "2027-06-18",
            "premium": "200000", "size": 10, "option_type": "call", "delta": "0.8",
            "next_earnings_date": ned, "executed_at": f"{TODAY.isoformat()}T14:00:00Z",
            "ask_vol": 10, "bid_vol": 2, "tags": []}


def _calendar(monkeypatch, dates=None, calls=None):
    """The stored calendar as a fake: ``dates`` as returned, every call counted."""
    def read(today=None):
        if calls is not None:
            calls.append(today)
        return dict(dates or {})
    monkeypatch.setattr(earnings_dates, "stored_next_dates", read)


def _seed_growing(ticker="AAA"):
    """One recorded session a day earlier, so today's read grows the far-dated call OI."""
    seed_session(date(2026, 10, 5), {ticker: [{"expiry": FAR, "call_oi": 500, "put_oi": 0}]})
    rawstore.save_watchlist({ticker})


# ── 1. the pinned name's own calendar date is the near event ──

def test_a_pinned_name_with_earnings_in_five_days_is_a_near_event_and_quiet(rawstore_tmp_db, monkeypatch):
    _seed_growing()
    _calendar(monkeypatch, {"AAA": TODAY + timedelta(days=5)})
    watched, _ = scan_leaps(_UW({"AAA": GROWING}), today=TODAY)
    (aaa,) = watched
    assert aaa["near_term_event"] is True and aaa["label"] == "Quiet"


# ── 2. beyond the window the footprint's own label stands ──

def test_a_pinned_name_with_earnings_forty_days_out_keeps_its_footprint_label(rawstore_tmp_db, monkeypatch):
    _seed_growing()
    _calendar(monkeypatch, {"AAA": TODAY + timedelta(days=40)})
    watched, _ = scan_leaps(_UW({"AAA": GROWING}), today=TODAY)
    (aaa,) = watched
    assert aaa["near_term_event"] is False and aaa["label"] == "Accumulating"


# ── 3. a flow candidate keeps the flag it has when the calendar has no row ──

def test_a_flow_candidate_keeps_its_own_near_event_flag_without_a_calendar_row(rawstore_tmp_db, monkeypatch):
    _seed_growing()
    _calendar(monkeypatch, {})
    ned = (TODAY + timedelta(days=3)).isoformat()
    watched, _ = scan_leaps(_UW({"AAA": GROWING}, tape=[_print("q1", "AAA", ned)]), today=TODAY)
    (aaa,) = watched
    assert aaa["near_term_event"] is True and aaa["label"] == "Quiet"


def test_a_flow_candidate_keeps_its_flag_against_a_calendar_row_that_says_otherwise(rawstore_tmp_db, monkeypatch):
    """#99 review: the two sources are OR'd. The candidate's date comes off the tape, the stored calendar may be
    several sessions old (``earnings_dates.MAX_AGE_DAYS``), so a stale calendar row must never take the flag away."""
    _seed_growing()
    _calendar(monkeypatch, {"AAA": TODAY + timedelta(days=40)})
    ned = (TODAY + timedelta(days=3)).isoformat()
    watched, _ = scan_leaps(_UW({"AAA": GROWING}, tape=[_print("q1", "AAA", ned)]), today=TODAY)
    (aaa,) = watched
    assert aaa["near_term_event"] is True and aaa["label"] == "Quiet"


# ── 4. the window is inclusive at exactly NEAR_EVENT_DAYS ──

def test_the_near_event_window_is_inclusive_at_the_constant(rawstore_tmp_db, monkeypatch):
    far_out = TODAY + timedelta(days=engine.NEAR_EVENT_DAYS + 1)
    _calendar(monkeypatch, {"AAA": TODAY + timedelta(days=engine.NEAR_EVENT_DAYS), "BBB": far_out})
    rawstore.save_watchlist({"AAA", "BBB"})
    watched, _ = scan_leaps(_UW({"AAA": (0, 0), "BBB": (0, 0)}), today=TODAY)
    flags = {w["ticker"]: w["near_term_event"] for w in watched}
    assert flags == {"AAA": True, "BBB": False}


# ── 5. the Refresh keeps the recorded flag, whatever today's calendar says ──

def _record(as_of: date, near_term_event: bool) -> dict:
    snap = {"ticker": "AAA", "name": "AAA Inc", "as_of": as_of.isoformat(), "label": "Accumulating", "score": 40,
            "far_call_oi": 1000, "far_put_oi": 200, "far_call_share": 0.8333,
            "near_term_event": near_term_event}
    oi = [{"expiry": FAR, "call_oi": 1000, "put_oi": 200}]
    leaps_history.record([{"ticker": "AAA", "name": "AAA Inc", "snapshot": snap,
                           "inputs": {"oi_per_expiry": oi}, "fetch_status": "ok"}],
                         run_id=f"s{as_of.isoformat()}", run_at=run_at_of(as_of))
    return snap


def test_a_refresh_keeps_the_recorded_near_event_flag(rawstore_tmp_db, monkeypatch):
    session = TODAY - timedelta(days=60)
    snap = _record(session, near_term_event=True)
    rawstore.save_watchlist({"AAA"})
    engine.save_cache("AAA", dict(snap))
    calls = []
    # Today's calendar has the next report 40 days out: recomputing against it would clear the recorded flag.
    _calendar(monkeypatch, {"AAA": TODAY + timedelta(days=40)}, calls=calls)

    watched, _ = scan_leaps_cached(today=TODAY)
    (aaa,) = watched
    assert aaa["near_term_event"] is True
    assert calls == []                                        # the recorded session's date decides, not today


# ── 6. a calendar that cannot be read never fails the scan, and says so once ──

def _raise_read(today=None):
    raise RuntimeError("no such table: earnings_calendar")


@pytest.mark.parametrize("calendar", ["empty", "raises"])
def test_an_unreadable_calendar_gives_no_flags_and_one_warning(rawstore_tmp_db, monkeypatch, calendar):
    _seed_growing()
    if calendar == "raises":
        monkeypatch.setattr(earnings_dates, "stored_next_dates", _raise_read)
    else:
        _calendar(monkeypatch, {})
    warnings: list[str] = []
    watched, _ = scan_leaps(_UW({"AAA": GROWING}), today=TODAY, warnings=warnings)
    (aaa,) = watched
    assert aaa["near_term_event"] is False and aaa["label"] == "Accumulating"
    assert len(warnings) == 1 and "earnings calendar" in warnings[0]


# ── 7. the cached path stays store-only ──

def test_the_cached_path_makes_no_network_call_and_no_calendar_read(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core.providers import unusual_whales as uw_mod

    def _network(*a, **k):
        raise AssertionError("the cached path opened a network call")

    monkeypatch.setattr(uw_mod.UWClient, "option_trades", _network)
    monkeypatch.setattr(uw_mod.UWClient, "screener_batch", _network)
    monkeypatch.setattr(earnings_dates, "stored_next_dates", _network)

    engine.save_cache("AAA", {"ticker": "AAA", "label": "Quiet", "score": 5, "as_of": "2026-10-05",
                              "near_term_event": False})
    rawstore.save_watchlist({"AAA"})
    watched, unwatched = scan_leaps_cached(today=TODAY)
    assert [w["ticker"] for w in watched] == ["AAA"] and unwatched == []
    assert rawstore.load_watchlist() == {"AAA"}
