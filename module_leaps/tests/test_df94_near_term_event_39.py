"""DF94 (#39): a pinned name's near-event flag comes from the stored earnings calendar, not only from a flow print.

Since G9 the calendar is the options store's screener set (``option_screener_daily.report_date``, every name reporting
in the 28 days after its session) and a print's report date is that same calendar's, so the tape no longer carries
a second date that could disagree (the old "a flow candidate keeps its own flag" cases are gone with it).

1. a watchlist ticker with no far-dated print and earnings within ``NEAR_EVENT_DAYS`` is a near event, so Quiet;
2. the same ticker with earnings beyond the window keeps the label its footprint earns;
3. a far-dated print of a name reporting within the window is a near-event candidate;
4. the window is inclusive at exactly ``NEAR_EVENT_DAYS`` days, and excludes the next day;
5. the Refresh computes the same flag as the Fetch, from the same store;
6. a calendar that cannot be read, or is too old, never fails the scan, and says so once.
"""
from datetime import date, timedelta

import pytest

from RevenueChaser.module_leaps import engine
from RevenueChaser.module_leaps.scan import REPORT_DATES_MAX_AGE_DAYS, scan_leaps, scan_leaps_cached
from RevenueChaser.module_leaps.tests.options_store import pin, print_row, store_footprint
from RevenueChaser.module_leaps.tests.test_leaps_l3 import seed_session
from RevenueChaser.module_oipd.tests.options_store import write_options
from RevenueChaser.rcdata import read

TODAY = date(2026, 10, 6)
FAR = date(2027, 12, 17)                 # > 270 days after TODAY
GROWING = (1000, 200)                    # today's far-dated OI, against the seeded 500/0 session


def _calendar(dates: dict, session=TODAY):
    """The screener set's report dates, one row a name, as ``uw/option_screener`` stores them."""
    write_options(option_screener_daily=[{"session_date": session, "ticker": t, "report_date": d,
                                          "passes_gates": True, "source": "uw/option_screener"}
                                         for t, d in dates.items()])


def _seed_growing(ticker="AAA", prints=()):
    """One recorded session a day earlier, so today's stored chain grows the far-dated call OI."""
    seed_session(TODAY - timedelta(days=1), {ticker: [{"expiry": FAR.isoformat(), "call_oi": 500, "put_oi": 0}]})
    store_footprint(ticker, TODAY, {FAR: GROWING}, prints=prints)
    pin(ticker)


# ── 1. the pinned name's own calendar date is the near event ──

def test_a_pinned_name_with_earnings_in_five_days_is_a_near_event_and_quiet(rawstore_tmp_db):
    _seed_growing()
    _calendar({"AAA": TODAY + timedelta(days=5)})
    (aaa,), _ = scan_leaps(None, today=TODAY)
    assert aaa["near_term_event"] is True and aaa["label"] == "Quiet"


# ── 2. beyond the window the footprint's own label stands ──

def test_a_pinned_name_with_earnings_forty_days_out_keeps_its_footprint_label(rawstore_tmp_db):
    _seed_growing()
    _calendar({"AAA": TODAY + timedelta(days=40)})
    (aaa,), _ = scan_leaps(None, today=TODAY)
    assert aaa["near_term_event"] is False and aaa["label"] == "Accumulating"


# ── 3. a far-dated print of a name reporting soon is a near-event candidate ──

def test_a_print_of_a_name_reporting_within_the_window_is_a_near_event_candidate(rawstore_tmp_db):
    _seed_growing(prints=[print_row("AAA", "q1", TODAY, date(2027, 6, 18))])
    _calendar({"AAA": TODAY + timedelta(days=3)})
    (aaa,), _ = scan_leaps(None, today=TODAY)
    assert aaa["frequency"] == 1 and aaa["near_term_event"] is True and aaa["label"] == "Quiet"


# ── 4. the window is inclusive at exactly NEAR_EVENT_DAYS ──

def test_the_near_event_window_is_inclusive_at_the_constant(rawstore_tmp_db):
    _calendar({"AAA": TODAY + timedelta(days=engine.NEAR_EVENT_DAYS),
               "BBB": TODAY + timedelta(days=engine.NEAR_EVENT_DAYS + 1)})
    for t in ("AAA", "BBB"):
        store_footprint(t, TODAY, {FAR: (0, 0)})
        pin(t)
    watched, _ = scan_leaps(None, today=TODAY)
    assert {w["ticker"]: w["near_term_event"] for w in watched} == {"AAA": True, "BBB": False}


# ── 5. the Refresh computes the Fetch's flag from the same store ──

def test_a_refresh_shows_the_fetchs_near_event_flag(rawstore_tmp_db):
    _seed_growing()
    _calendar({"AAA": TODAY + timedelta(days=2)})
    (fetched,), _ = scan_leaps(None, today=TODAY, footprints=[])
    (refreshed,), _ = scan_leaps_cached(today=TODAY)
    assert fetched["near_term_event"] is refreshed["near_term_event"] is True


# ── 6. a calendar that cannot be read never fails the scan, and says so once ──

def _raise_read(*a, **k):
    raise RuntimeError("no such table: option_screener_daily")


@pytest.mark.parametrize("calendar", ["empty", "raises", "stale"])
def test_an_unreadable_calendar_gives_no_flags_and_one_warning(rawstore_tmp_db, monkeypatch, calendar):
    _seed_growing()
    if calendar == "raises":
        monkeypatch.setattr(read, "option_screener", _raise_read)
    elif calendar == "stale":
        old = TODAY - timedelta(days=REPORT_DATES_MAX_AGE_DAYS + 1)
        _calendar({"AAA": TODAY + timedelta(days=2)}, session=old)
    warnings: list[str] = []
    (aaa,), _ = scan_leaps(None, today=TODAY, warnings=warnings)
    assert aaa["near_term_event"] is False and aaa["label"] == "Accumulating"
    assert len(warnings) == 1 and "earnings calendar" in warnings[0]


# ── 7. the cached path stays offline ──

def test_the_cached_path_makes_no_network_call(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core.providers import unusual_whales as uw_mod

    def _network(*a, **k):
        raise AssertionError("the cached path opened a network call")

    for name in ("option_trades", "screener_batch", "oi_per_expiry", "flow_per_expiry", "iv_rank"):
        monkeypatch.setattr(uw_mod.UWClient, name, _network)
    _seed_growing()
    watched, unwatched = scan_leaps_cached(today=TODAY)
    assert [w["ticker"] for w in watched] == ["AAA"] and unwatched == []
