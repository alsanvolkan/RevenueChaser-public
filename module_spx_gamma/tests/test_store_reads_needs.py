"""SPX Gamma reads the market store through the needs layer, judged against the board's own day (#330).

``test_store_reads.py`` keeps its assertions unchanged: the same numbers on the same fixture store. These pin the new
path: each of the four readers asks ``needs.ask(need, as_of=day, ...)``; the COT ask ends ``RELEASE_DAYS`` before the
day, so only released reports are judged; and the lag limits are the catalog's, read on every call.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.module_spx_gamma import store_reads
from RevenueChaser.rcdata import needs

from . import rcdata_fixture as fx


@pytest.fixture(autouse=True)
def after_the_last_close(monkeypatch):
    """Tue 27 Oct 2026, 08:00 New York: the latest complete session is Mon 26 Oct."""
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc))


@pytest.fixture
def asks(monkeypatch):
    """Every ``needs.ask`` the board makes, as ``(need, as_of, params)``; the real answer is returned."""
    seen: list[tuple] = []
    real = needs.ask

    def recording(need, *, as_of=None, **params):
        seen.append((need, as_of, params))
        return real(need, as_of=as_of, **params)
    monkeypatch.setattr(needs, "ask", recording)
    return seen


def test_every_reader_asks_its_need_judged_from_the_boards_day(rcdata_market, asks):
    day = fx.FRIDAY
    store_reads.read_day(day)
    assert {need for need, _, _ in asks} == {"market.vix", "market.cot", "market.spx_close",
                                             "market.spx_expiry_strike"}
    assert all(as_of == day for _, as_of, _ in asks), asks


def test_a_past_day_not_yet_closed_asks_the_previous_session_judged_from_the_day(rcdata_market, asks):
    """A3 stays the board's: Tue 27 Oct before its close asks for the 26 Oct snapshot, still judged from the 27th."""
    store_reads.zero_one_dte_dollars(date(2026, 10, 27))
    sessions = [(p["session_date"], as_of) for n, as_of, p in asks if n == "market.spx_expiry_strike"]
    assert sessions == [(date(2026, 10, 27), date(2026, 10, 27)), (date(2026, 10, 26), date(2026, 10, 27))]


def test_the_cot_ask_ends_release_days_before_the_day_so_only_released_reports_are_judged(rcdata_market, asks):
    day = date(2026, 10, 22)
    store_reads.cot_tff(day)
    (_, as_of, params), = [a for a in asks if a[0] == "market.cot"]
    assert as_of == day and params["end"] == day - timedelta(days=store_reads.RELEASE_DAYS)


def test_the_board_names_no_series_id_and_calls_no_reader_of_its_own():
    """The four reads go through needs.ask; what remains of ``rcdata.read`` is the ``NEVER_CHECKED`` constant (the
    status itself comes from ``ops``)."""
    source = Path(store_reads.__file__).read_text(encoding="utf-8")
    for call in ("read.market_series(", "read.spx_expiry_strike(", '["SPX"]'):
        assert call not in source, call
    for gone in ("_within", "_sessions_after"):
        assert not hasattr(store_reads, gone), gone


def test_the_lag_limits_are_the_catalogs_read_on_every_call(monkeypatch):
    assert dict(store_reads.MAX_LAG) == {"vix": needs.NEEDS["market.vix"].max_lag,
                                         "cot": needs.NEEDS["market.cot"].max_lag}
    monkeypatch.setitem(needs.catalog.NEEDS, "market.vix",
                        dataclasses.replace(needs.NEEDS["market.vix"], max_lag=7))
    assert store_reads.MAX_LAG["vix"] == 7
    with pytest.raises(TypeError, match="need catalog"):
        store_reads.MAX_LAG["vix"] = 3


def test_a_wider_catalog_window_is_what_the_board_judges_by(rcdata_market, monkeypatch):
    """Fri 30 Oct is 4 sessions after the newest VIX (Mon 26): not stored at 3, stored at 4."""
    assert store_reads.vix_family(date(2026, 10, 30))["stored"] is False
    monkeypatch.setitem(needs.catalog.NEEDS, "market.vix",
                        dataclasses.replace(needs.NEEDS["market.vix"], max_lag=4))
    assert store_reads.vix_family(date(2026, 10, 30))["stored"] is True
    monkeypatch.setitem(needs.catalog.NEEDS, "market.cot",
                        dataclasses.replace(needs.NEEDS["market.cot"], max_lag=11))
    assert store_reads.cot_tff(date(2026, 11, 4))["stored"] is True        # 11 sessions after 20 Oct


def test_a_vix_day_older_than_the_newest_family_row_is_judged_by_its_own_day(rcdata_market, monkeypatch):
    """The board reads the newest day with a VIX close; when a sibling series is newer the envelope's newest day is
    not the board's, and the lag is counted from the VIX day itself (as the board's own count always was)."""
    from RevenueChaser.rcdata import read
    real = read.market_series

    def vix_ends_early(*a, **k):
        return [r for r in real(*a, **k) if not (r["series_id"] == "VIX" and r["obs_date"] > date(2026, 10, 21))]
    monkeypatch.setattr(read, "market_series", vix_ends_early)
    # Mon 26 Oct: VIX's newest is Wed 21 Oct, 3 sessions back (fresh); the siblings run to the 26th.
    assert store_reads.vix_family(date(2026, 10, 26))["obs_date"] == "2026-10-21"
    # Tue 27 Oct: 4 sessions back, not stored, although the family's newest row (26 Oct) is 1 back.
    assert store_reads.vix_family(date(2026, 10, 27))["stored"] is False


def test_the_boards_caret_map_covers_exactly_the_series_the_vix_need_names():
    """The board no longer names the series it asks for; a series the need adds without a caret here would fail
    the whole family into "not stored", so the two lists are one."""
    assert set(needs.NEEDS["market.vix"].filter_values) == set(store_reads.VIX_SERIES)
    assert needs.NEEDS["market.cot"].filter_prefix == store_reads.COT_PREFIX


# ── review of 1e1b250 (#334): days outside the calendar's holiday table keep their data ──

HISTORY_DAY = date(1992, 10, 23)      # a Friday before the holiday table (1993-2028) starts


@pytest.fixture
def a_2024_store(monkeypatch):
    """Readers answering rows for 23 Oct 1992 (and the 2029 day), as a store built from Cboe's 1975- file would."""
    from RevenueChaser.rcdata import read

    def series(series_ids=(), start=None, end=None, prefix=None):
        days = [d for d in (date(1992, 10, 22), HISTORY_DAY, date(2029, 3, 1), date(2029, 3, 2))
                if (start is None or d >= start) and (end is None or d <= end)]
        return [{"series_id": "SPX", "obs_date": d, "value": 5800.0 + d.day, "close": None, "status": "changed",
                 "source": "cboe", "source_as_of": d} for d in days]

    def strikes(session_date, min_dte=0, max_dte=1):
        return [{"session_date": session_date, "expiry": session_date, "strike": 5800.0, "call_gamma_oi": 1e9,
                 "put_gamma_oi": -2e8, "snapshot_time": "2024-10-25T20:15:00Z", "status": "changed",
                 "source_as_of": session_date}]
    monkeypatch.setattr(read, "market_series", series)
    monkeypatch.setattr(read, "spx_expiry_strike", strikes)


@pytest.mark.usefixtures("a_2024_store")
@pytest.mark.parametrize("day", [HISTORY_DAY, date(2029, 3, 2)])
def test_spx_closes_of_a_day_outside_the_holiday_table_are_kept(day):
    out = store_reads.spx_candles(day)
    assert out["stored"] is True and out["rows"][-1]["date"] == day.isoformat(), out["label"]


@pytest.mark.usefixtures("a_2024_store")
def test_the_0_1_dte_slice_of_a_day_outside_the_table_is_kept():
    out = store_reads.zero_one_dte_dollars(HISTORY_DAY)
    assert out["stored"] is True and out["dollars"] == {5800.0: 8e8}, out["label"]


@pytest.mark.usefixtures("a_2024_store")
def test_history_mode_finds_the_stored_close_of_a_day_outside_the_table():
    """The history pull reads its spot from the stored close; without it the day is skipped_no_close."""
    from RevenueChaser.module_spx_gamma import snapshot
    assert snapshot._stored_close(snapshot.stored_spx_candles, "SPX", HISTORY_DAY.isoformat()) == 5800.0 + HISTORY_DAY.day


@pytest.mark.usefixtures("a_2024_store")
def test_vix_and_cot_outside_the_table_stay_not_stored_with_no_reason_as_before():
    """The lag cannot be counted, so the two readers that judge it say "not stored", as _sessions_after did."""
    assert store_reads.vix_family(HISTORY_DAY)["label"] == f"VIX family not stored for {HISTORY_DAY.isoformat()}"
    assert store_reads.cot_tff(HISTORY_DAY)["label"] == f"CFTC COT not stored for {HISTORY_DAY.isoformat()}"


def test_a_failed_row_read_is_read_failed_and_does_not_retry_the_previous_session(rcdata_market, monkeypatch):
    """Should-fix 3: with the market file there, a sqlite3 error comes from the row read itself (read._rows reads the
    status with the rows). The old board said "read failed" and stopped; A3 retries only on no rows."""
    import sqlite3

    from RevenueChaser.rcdata import read
    calls = []

    def locked(*a, **k):
        calls.append(a)
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(read, "spx_expiry_strike", locked)
    out = store_reads.zero_one_dte_dollars(date(2026, 10, 27))                  # not closed: A3 would apply
    assert out["stored"] is False and "read failed: OperationalError: database is locked" in out["label"]
    assert "status unknown" not in out["label"] and len(calls) == 1
