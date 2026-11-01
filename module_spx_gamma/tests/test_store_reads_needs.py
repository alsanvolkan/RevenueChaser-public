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
    """The four reads go through needs.ask; ``rcdata.read`` is left for the dataset status only."""
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
