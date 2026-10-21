"""E5a (#372): the Earnings Fetch reads the ``earnings_calendar`` rows the nightly D26 sweep stored and calls UW
only for the (day, session) pairs the store does not cover.

Offline: a fake client that counts its calls, a temp store seeded with the stored rows. The stored event must be
byte-identical to the live one built from the same UW row (the live read is the reference), because the run
downstream (screener, scoring) cannot tell which path a row came from except by its ``source``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.app import runners
from RevenueChaser.core import calendar, rawstore

SESSION = date(2026, 10, 13)          # Tuesday, the night the D26 sweep ran
DAY = date(2026, 10, 16)              # Friday, the day the Fetch reads the calendar for (past the live days)
SATURDAY = date(2026, 10, 17)
NOW = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc)      # 17:00 New York: the D14 session is complete


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


@pytest.fixture(autouse=True)
def _store(rawstore_tmp_db):
    return rawstore_tmp_db


def _item(ticker, **over):
    item = {"symbol": ticker, "sector": "Technology", "full_name": f"{ticker} Inc", "marketcap": 1_500_000_000,
            "expected_move_perc": 0.052, "street_mean_est": 1.25, "pre_earnings_close": 10.0,
            "post_earnings_close": 10.5, "report_time": "premarket"}
    item.update(over)
    return item


def _row(item, day=DAY, session="premarket", session_date=SESSION):
    return {"session_date": session_date, "earnings_date": day, "ticker": item["symbol"],
            "report_session": session, "item": item, "marketcap": 1_500_000_000, "source": "uw"}


class _UW:
    """Records every ``calendar_day_raw`` call; serves one canned row per (day, session)."""

    def __init__(self, rows=None):
        self.rows = rows or {}
        self.calls = []

    def calendar_day_raw(self, d, session):
        self.calls.append((d, session))
        return list(self.rows.get((d, session), []))


def _live_events(uw, days):
    """The events the live path builds: the reference for the stored path."""
    out = []
    for d in days:
        if d.weekday() >= 5:
            continue
        for session in ("premarket", "afterhours"):
            for item in uw.calendar_day_raw(d, session):
                ev = runners._calendar_event(item, d, session)
                if ev is not None:
                    out.append(ev)
    return out


def _key(ev):
    return (str(ev["earnings_date"]), ev["earnings_time"], ev["ticker"])


def test_full_coverage_calls_nothing_and_matches_the_live_events():
    items = {"premarket": [_item("ZZZ"), _item("AAA")], "afterhours": [_item("MMM")]}
    rawstore.write_rows("earnings_calendar", [_row(i, session=s) for s in items for i in items[s]])
    fake = _UW({(DAY, s): items[s] for s in items})
    days = [DAY]

    events, raw_cal = runners._fetch_calendar_days(fake, days)

    assert fake.calls == []                                   # nothing live: every pair is covered
    assert sorted(events, key=_key) == sorted(_live_events(fake, days), key=_key)
    assert sorted(raw_cal) == ["AAA", "MMM", "ZZZ"]
    assert all(e["source"] == "UW" for e in events)           # counted against UW_CALENDAR_ROW_LIMIT as before


def test_one_uncovered_pair_is_exactly_one_live_call():
    rawstore.write_rows("earnings_calendar", [_row(_item("AAA"), session="premarket")])
    fake = _UW({(DAY, "premarket"): [_item("AAA")], (DAY, "afterhours"): [_item("BBB")]})

    events, _ = runners._fetch_calendar_days(fake, [DAY])

    assert fake.calls == [(DAY, "afterhours")]
    assert sorted(e["ticker"] for e in events) == ["AAA", "BBB"]
    assert [e for e in events if e["ticker"] == "BBB"][0]["earnings_time"] == "AMC"


def test_a_session_older_than_the_lag_reads_everything_live():
    """A sweep from the previous week is not the calendar: the pair is not covered (MAX_LAG 1 session)."""
    rawstore.write_rows("earnings_calendar", [_row(_item("AAA"), session_date=date(2026, 10, 9))])
    fake = _UW({(DAY, "premarket"): [_item("AAA")], (DAY, "afterhours"): [_item("BBB")]})

    events, _ = runners._fetch_calendar_days(fake, [DAY])

    assert fake.calls == [(DAY, "premarket"), (DAY, "afterhours")]
    assert sorted(e["ticker"] for e in events) == ["AAA", "BBB"]


def test_a_store_error_is_a_live_read_not_a_failure(monkeypatch):
    """A broken store must not lose the calendar: the pairs read live."""
    rawstore.write_rows("earnings_calendar", [_row(_item("AAA"))])
    fake = _UW({(DAY, "premarket"): [_item("AAA")], (DAY, "afterhours"): [_item("BBB")]})
    monkeypatch.setattr(rawstore, "query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store is busy")))

    events, _ = runners._fetch_calendar_days(fake, [DAY])

    assert fake.calls == [(DAY, "premarket"), (DAY, "afterhours")]
    assert sorted(e["ticker"] for e in events) == ["AAA", "BBB"]


def test_a_stored_item_without_a_ticker_is_skipped():
    rawstore.write_rows("earnings_calendar", [
        {"session_date": SESSION, "earnings_date": DAY, "ticker": "", "report_session": "premarket",
         "item": {"symbol": ""}, "source": "uw"}])
    fake = _UW({(DAY, "premarket"): [{"symbol": ""}]})

    events, _ = runners._fetch_calendar_days(fake, [DAY])

    assert events == []


def test_weekends_are_skipped_without_a_read():
    fake = _UW({(DAY, "premarket"): [_item("AAA")]})

    events, _ = runners._fetch_calendar_days(fake, [SATURDAY, DAY])

    assert SATURDAY not in [d for d, _ in fake.calls]
    assert fake.calls == [(DAY, "premarket"), (DAY, "afterhours")]
    assert [e["earnings_date"] for e in events] == [DAY]


def test_the_stored_path_uses_the_item_not_the_typed_columns():
    """``marketcap`` is a DOUBLE column; the live row is text. The rebuild must keep the served text."""
    item = _item("AAA", marketcap="1.5B")
    rawstore.write_rows("earnings_calendar", [_row(item)])
    fake = _UW({(DAY, "afterhours"): [_item("BBB")]})

    events, _ = runners._fetch_calendar_days(fake, [DAY])

    aaa = [e for e in events if e["ticker"] == "AAA"][0]
    assert aaa["marketcap"] == "1.5B"


def test_premarket_wins_raw_cal_on_a_covered_pair():
    """The stored path keeps the live rule: a name in both sessions keeps its premarket event."""
    rawstore.write_rows("earnings_calendar", [
        _row(_item("AAA", report_time="premarket"), session="premarket"),
        _row(_item("AAA", report_time="afterhours"), session="afterhours")])
    fake = _UW()

    _, raw_cal = runners._fetch_calendar_days(fake, [DAY])

    assert raw_cal["AAA"]["earnings_time"] == "BMO"
    assert raw_cal["AAA"]["report_time"] == "premarket"


def test_today_and_the_next_trading_day_stay_live_even_when_stored():
    """Review S4: the stored row is last night's, and the EM gate, the cap, R4 and the card's estimate and report
    time read it. UW moves those fields during the day for the names about to report, so today's and the next
    trading day's pairs (2-4 calls) stay live; later days read the store."""
    today, tomorrow = date(2026, 10, 14), date(2026, 10, 15)          # NOW is Wed 10-14 17:00 New York
    rawstore.write_rows("earnings_calendar", [_row(_item(t), day=d, session=s)
                                              for t, d in (("AAA", today), ("BBB", tomorrow), ("CCC", DAY))
                                              for s in ("premarket", "afterhours")])
    fake = _UW({(today, "premarket"): [_item("AAA", expected_move_perc=0.08)]})

    events, raw_cal = runners._fetch_calendar_days(fake, [today, tomorrow, DAY])

    assert fake.calls == [(today, "premarket"), (today, "afterhours"),
                          (tomorrow, "premarket"), (tomorrow, "afterhours")]
    assert raw_cal["AAA"]["expected_move_perc"] == 0.08               # today's live answer, not last night's
    assert "BBB" not in raw_cal                                       # tomorrow read live: UW served nothing
    assert raw_cal["CCC"]["earnings_date"] == DAY                     # Friday came from the store
    assert runners._CALENDAR_SOURCES == {"stored": 2, "live": 4, "session": SESSION.isoformat()}
