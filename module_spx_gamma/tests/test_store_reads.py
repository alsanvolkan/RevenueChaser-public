"""``store_reads``: SPX Gamma's one reader of the market store (D4), on the fixture serving file (PR 3, sub-step 2).

Every reader maps ``rcdata.read`` rows to the shape the board used to get from its own files; a day without rows is
a "not stored" marker, never an exception and never a provider call (the root conftest blocks sockets).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.module_spx_gamma import store_reads
from RevenueChaser.rcdata import read

from . import rcdata_fixture as fx


@pytest.fixture(autouse=True)
def after_the_last_close(monkeypatch):
    """Tue 27 Oct 2026, 08:00 New York: the latest complete session is Mon 26 Oct."""
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc))


# ── 0-1 DTE dollars ──

def test_zero_one_dte_sums_call_and_put_oi_gamma_per_strike_over_both_expiries():
    s = fx.LAST
    nxt = calendar.next_trading_day(s)
    fx.serve(spx={s: [fx.uw_row(s, s, 6800, 4e9, -1e9), fx.uw_row(s, nxt, 6800, 2e9, -0.5e9),
                      fx.uw_row(s, nxt, 6810, 1e9, 0)]})
    out = store_reads.zero_one_dte_dollars(s)
    assert out["stored"] is True and out["fallback"] is False
    assert out["dollars"] == {6800.0: 4.5e9, 6810.0: 1e9}
    assert out["session"] == "2026-10-26" and out["snapshot_time"] == "2026-10-26T20:15:00Z"
    assert out["status"] == "changed" and out["source"] == store_reads.SPX_SOURCE
    assert out["expiries"] == ["2026-10-26", "2026-10-27"]
    assert "2026-10-26" in out["label"] and "20:15" in out["label"]


def test_a_friday_sums_the_monday_expiry_as_one_dte(rcdata_market):
    f = fx.FRIDAY
    fx.serve(spx={f: [fx.uw_row(f, f, 6800, 1e9, 0), fx.uw_row(f, date(2026, 10, 26), 6800, 2e9, 0)]})
    out = store_reads.zero_one_dte_dollars(f)
    assert out["dollars"] == {6800.0: 3e9}
    assert out["expiries"] == ["2026-10-23", "2026-10-26"]


def test_a_complete_day_without_rows_is_not_stored_and_does_not_borrow_an_older_session(rcdata_market):
    out = store_reads.zero_one_dte_dollars(date(2026, 10, 19))      # a session before the fixture's first
    assert out["stored"] is False and out["dollars"] == {}
    assert out["label"] == "0-1 DTE not stored for 2026-10-19"


def test_a_day_not_yet_closed_uses_the_previous_session_and_keeps_expiries_from_the_day_on(rcdata_market):
    """A3: Tue 27 Oct before its close: the 26 Oct snapshot, its 27 Oct expiry only (0 DTE for the board's day)."""
    out = store_reads.zero_one_dte_dollars(date(2026, 10, 27))
    assert out["stored"] is True and out["fallback"] is True
    assert out["session"] == "2026-10-26" and out["expiries"] == ["2026-10-27"]
    assert len(out["dollars"]) == 239
    # review SF2: the previous session's snapshot holds the day's 0 DTE expiry only, and says so
    assert out["label"].startswith("0 DTE only (previous-session snapshot, 2026-10-26 2026-10-26T20:15:00Z)")
    assert "0-1 DTE" not in out["label"]


def test_no_serving_file_is_not_stored():
    out = store_reads.zero_one_dte_dollars(fx.LAST)
    assert out["stored"] is False and out["status"] == read.NEVER_CHECKED


def test_a_failing_read_is_a_not_stored_marker_not_an_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(read, "spx_expiry_strike", boom)
    monkeypatch.setattr(read, "market_series", boom)
    day = store_reads.read_day(fx.LAST)
    assert day["zero_one_dte"]["stored"] is False and "database is locked" in day["zero_one_dte"]["label"]
    for key in ("vix", "cftc", "candles"):
        assert day["store_inputs"][key]["stored"] is False


# ── VIX family ──

def test_vix_family_maps_to_the_caret_keys_with_term_mode(rcdata_market):
    out = store_reads.vix_family(fx.LAST)
    assert out["stored"] is True and out["obs_date"] == "2026-10-26"
    tickers = out["payload"]["tickers"]
    assert set(tickers) == {"^VIX", "^VIX9D", "^VIX3M", "^VIX6M", "^VIX1Y", "^VVIX"}
    assert tickers["^VVIX"] is not None, "VVIX is a one-column file: its value is the close"
    assert out["payload"]["term_mode"] == "contango"          # VIX 16.x < VIX3M 18.x
    assert out["status"] == "changed" and out["source"] == store_reads.SERIES_SOURCE


def test_vix_takes_the_newest_obs_on_or_before_the_day(rcdata_market):
    assert store_reads.vix_family(fx.FRIDAY)["obs_date"] == "2026-10-23"
    assert store_reads.vix_family(date(2026, 10, 25))["obs_date"] == "2026-10-23"      # a Sunday


def test_vix_older_than_three_sessions_is_not_stored(rcdata_market):
    assert store_reads.vix_family(date(2026, 10, 29))["stored"] is True       # Thu: Mon 26 is 3 sessions back
    late = store_reads.vix_family(date(2026, 10, 30))                       # Fri: 4 sessions back
    assert late["stored"] is False and late["label"] == "VIX family not stored for 2026-10-30"


# ── CFTC COT ──

def test_cot_maps_the_e_mini_report_with_change_as_this_report_minus_the_previous(rcdata_market):
    out = store_reads.cot_tff(fx.LAST)
    cot = out["payload"]
    assert out["stored"] is True and cot["report_date"] == "2026-10-20" and cot["release_date"] == "2026-10-23"
    am = cot["asset_manager"]
    assert (am["long"], am["short"], am["net"]) == (1_030_000.0, 303_000.0, 727_000.0)
    assert am["change"] == 10_000.0, "A5: this report's long minus the previous report's long"
    assert cot["leveraged_funds"]["change"] == -5000.0 and cot["dealer"]["change"] == 3000.0
    assert cot["open_interest"] == 2_003_000.0


def test_a_report_counts_only_from_its_friday_release(rcdata_market):
    """Tuesday 20 Oct's report is released Friday 23 Oct: on Thursday the board shows the 13 Oct report."""
    assert store_reads.cot_tff(date(2026, 10, 22))["payload"]["report_date"] == "2026-10-13"
    assert store_reads.cot_tff(fx.FRIDAY)["payload"]["report_date"] == "2026-10-20"


def test_the_oldest_report_has_no_change_and_a_stale_report_is_not_stored(rcdata_market):
    first = store_reads.cot_tff(date(2026, 10, 5))["payload"]
    assert first["report_date"] == "2026-09-29" and first["asset_manager"]["change"] is None
    assert store_reads.cot_tff(date(2026, 11, 3))["stored"] is True            # 10 sessions after 20 Oct
    stale = store_reads.cot_tff(date(2026, 11, 4))                             # 11 sessions after
    assert stale["stored"] is False and stale["label"] == "CFTC COT not stored for 2026-11-04"


# ── SPX candles ──

def test_spx_candles_are_the_closes_of_the_window_without_ohlc_or_volume(rcdata_market):
    out = store_reads.spx_candles(fx.LAST)
    rows = out["rows"]
    assert out["stored"] is True and rows[-1]["date"] == "2026-10-26"
    assert rows[0]["date"] >= "2026-08-21" and rows == sorted(rows, key=lambda r: r["date"])
    assert all(r["close"] is not None and r["open"] is None and r["volume"] is None for r in rows)
    assert out["source"] == store_reads.SERIES_SOURCE and "SPX" in out["label"]


def test_no_candles_in_the_window_is_not_stored(rcdata_market):
    out = store_reads.spx_candles(date(2027, 3, 1))
    assert out["stored"] is False and out["rows"] == []
    assert out["label"] == "SPX closes not stored for 2027-03-01"


# ── per-call, read-only, closed ──

def test_every_read_opens_read_only_and_closes_before_it_returns(rcdata_market, monkeypatch):
    from RevenueChaser.rcdata import serving
    opened = []
    real = serving.open_read

    def counting(kind):
        con = real(kind)
        opened.append(con)
        return con
    monkeypatch.setattr(serving, "open_read", counting)
    store_reads.read_day(fx.LAST)
    assert len(opened) >= 4, "one connection per read, none kept"
    for con in opened:
        with pytest.raises(Exception, match="(?i)closed"):
            con.execute("SELECT 1")


def test_store_reads_keeps_no_connection_at_module_level(rcdata_market):
    import duckdb
    store_reads.read_day(fx.LAST)
    held = [v for v in vars(store_reads).values() if isinstance(v, duckdb.DuckDBPyConnection)]
    assert held == []


def test_read_day_hands_build_day_the_raw_keys(rcdata_market):
    day = store_reads.read_day(fx.LAST)
    assert set(day) == {"vix", "cftc", "ohlc", "zero_one_dte", "store_inputs"}
    assert day["ohlc"]["data"] and day["vix"]["tickers"]["^VIX"] is not None and day["cftc"]["report_date"]
    assert set(day["store_inputs"]) == {"vix", "cftc", "candles", "zero_one_dte"}
    for meta in day["store_inputs"].values():
        assert meta["stored"] is True and meta["status"] == "changed" and meta["source_as_of"]


def test_the_banner_names_a_0_dte_only_previous_session_slice(rcdata_market):
    """Review SF2: the pin line and the data-quality banner both say the slice is partial (0 DTE only)."""
    from RevenueChaser.module_spx_gamma import render
    from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot
    from .conftest import make_raw
    raw = {**make_raw(), **store_reads.read_day(date(2026, 10, 27))}
    snap = normalize_snapshot(raw, "2026-10-27")
    notice = render.data_quality_notice(snap) or ""
    assert "0 DTE only (previous-session snapshot, 2026-10-26" in notice
    assert "0 DTE only (previous-session snapshot, 2026-10-26" in (render.pin_source_line(snap) or "")


def test_a_broken_ops_file_on_the_not_stored_path_is_a_marker_not_an_exception(monkeypatch):
    """Review SF3: no market.duckdb yet, then the status read of ops.sqlite fails (locked, stamped newer, corrupt).
    read_day must still return markers: build_day, and the fetch manifest after the paid UW calls, call it."""
    import sqlite3

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(read, "spx_expiry_strike", lambda *a, **k: [])
    monkeypatch.setattr(read, "dataset_status", locked)
    day = store_reads.read_day(fx.LAST)
    zod = day["zero_one_dte"]
    assert zod["stored"] is False and zod["label"].startswith("0-1 DTE not stored for 2026-10-26")
    assert "status unknown" in zod["label"] and "database is locked" in zod["label"]


def test_the_weekly_change_is_none_across_a_missing_report_and_the_board_says_gap():
    """Review SF4: with the 6 Oct report missing, 13 Oct minus 29 Sep is two weeks, not a weekly change."""
    from RevenueChaser.module_spx_gamma import render
    from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot
    from .conftest import make_raw
    fx.serve(spx={}, cot_reports=(date(2026, 9, 29), date(2026, 10, 13)))
    out = store_reads.cot_tff(date(2026, 10, 19))
    cot = out["payload"]
    assert cot["report_date"] == "2026-10-13" and cot["change_gap"] is True
    assert all(cot[g]["change"] is None for g in ("asset_manager", "leveraged_funds", "dealer"))
    snap = normalize_snapshot({**make_raw(), "cftc": cot}, "2026-10-19")
    table = render.render_html(snap).split("CFTC COT")[1].split("</table>")[0]
    assert table.count("n/a (gap)") == 3


def test_a_weekly_change_of_consecutive_reports_has_no_gap(rcdata_market):
    cot = store_reads.cot_tff(fx.LAST)["payload"]
    assert cot["change_gap"] is False and cot["asset_manager"]["change"] == 10_000.0


def test_the_cot_release_date_is_called_an_approximation(rcdata_market):
    """Review SF5: report + 3 days is not CFTC's schedule (holiday weeks, the 2025 shutdown); the board says so."""
    label = store_reads.cot_tff(fx.LAST)["label"]
    assert "released ~2026-10-23 (report + 3 days; holiday weeks can differ)" in label
