"""D28/D29 (#34): the two stored market series read back in the shape their live fetchers serve.

``vol_index_daily`` (the Cboe vol indices) and ``cftc_cot`` (the weekly E-mini TFF row) are written nightly by
``RC-Earnings-Inputs`` and read by SPX Gamma. Offline: a temp store seeded with the stored rows, and a pinned
clock so "the latest complete session" is Wed 2026-10-21 (the holiday table has no October entry: Mon 10-12 trades).

The readers return the 2-tuple every reader here returns; a stale series collapses to ``({}, None)`` so the caller
asks the provider, which is what ``_fresh_session`` exists for.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from RevenueChaser.core import calendar, rawstore
from RevenueChaser.module_earnings_radar import stored_inputs

NOW = datetime(2026, 10, 21, 21, 0, tzinfo=timezone.utc)      # 17:00 New York: Wed 10-21 is complete
LATEST = date(2026, 10, 21)

CLOSES = {"VIX": 15.5, "VIX9D": 14.25, "VIX3M": 17.0, "VIX6M": 18.0, "VIX1Y": 19.0, "VVIX": 90.5}
COT = {"open_interest": 2_000_000.0,
       "asset_mgr_long": 500_000.0, "asset_mgr_short": 120_000.0, "asset_mgr_change_long": 1_500.0,
       "lev_money_long": 300_000.0, "lev_money_short": 260_000.0, "lev_money_change_long": -2_200.0,
       "dealer_long": 90_000.0, "dealer_short": 420_000.0, "dealer_change_long": 300.0}


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


@pytest.fixture(autouse=True)
def _store(rawstore_tmp_db):
    return rawstore_tmp_db


@pytest.fixture(autouse=True)
def _notes(monkeypatch):
    """``_LOGGED`` is one-note-per-process; every test starts with none noted."""
    monkeypatch.setattr(stored_inputs, "_LOGGED", set())


def sessions_back(n: int) -> date:
    """The session ``n`` trading sessions before :data:`LATEST` (0 = ``LATEST`` itself)."""
    d = LATEST
    for _ in range(n):
        d = calendar.previous_trading_day(d - timedelta(days=1))
    return d


def _seed_vol(session: date, closes=None) -> None:
    rawstore.write_rows("vol_index_daily", [
        {"symbol": symbol, "session_date": session, "source": "cboe", "close": close,
         "open": close, "high": close, "low": close}
        for symbol, close in (CLOSES if closes is None else closes).items()])


def _seed_cot(report: date, session: date | None = None, **over) -> None:
    rawstore.write_rows("cftc_cot", [{
        "report_type": "TFF", "market": "E-MINI S&P 500", "report_date": report,
        "session_date": session or report, "source": "cftc", **COT, **over}])


# ── 1. nothing stored ────────────────────────────────────────────────────────────────────────────────────────────

def test_an_empty_table_is_nothing_stored_and_raises_nothing():
    assert stored_inputs.vol_index_rows() == ({}, None)
    assert stored_inputs.cot_row() == ({}, None)


def test_a_table_that_does_not_exist_yet_is_nothing_stored(monkeypatch, tmp_path):
    """Before the first nightly run the store file itself is absent: still not an error."""
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "absent.duckdb")
    assert stored_inputs.vol_index_rows() == ({}, None)
    assert stored_inputs.cot_row() == ({}, None)


# ── 2. the newest session's payload ──────────────────────────────────────────────────────────────────────────────

def test_vol_index_rows_of_three_sessions_are_the_newest_session():
    for n in (0, 1, 2):
        _seed_vol(sessions_back(n), {s: c + n for s, c in CLOSES.items()})

    payload, session = stored_inputs.vol_index_rows()

    assert session == LATEST
    assert payload == {"tickers": {f"^{s}": c for s, c in CLOSES.items()}, "term_mode": "contango"}


def test_vol_index_rows_leaves_out_the_symbols_the_vix_layer_does_not_read():
    _seed_vol(LATEST, {**CLOSES, "OVX": 40.0, "MOVE": 80.0})
    payload, _ = stored_inputs.vol_index_rows()
    assert sorted(payload["tickers"]) == ["^VIX", "^VIX1Y", "^VIX3M", "^VIX6M", "^VIX9D", "^VVIX"]


def test_cot_row_of_three_sessions_is_the_newest_session():
    for n in (0, 1, 2):
        _seed_cot(sessions_back(n), open_interest=2_000_000.0 + n)

    payload, session = stored_inputs.cot_row()

    assert session == LATEST
    assert payload == {
        "report_date": LATEST.isoformat(),
        "open_interest": 2_000_000.0,
        "asset_manager": {"long": 500_000.0, "short": 120_000.0, "net": 380_000.0, "change": 1_500.0},
        "leveraged_funds": {"long": 300_000.0, "short": 260_000.0, "net": 40_000.0, "change": -2_200.0},
        "dealer": {"long": 90_000.0, "short": 420_000.0, "net": -330_000.0, "change": 300.0},
    }


def test_cot_net_is_none_when_one_side_is_null():
    _seed_cot(LATEST, asset_mgr_short=None, dealer_long=None)
    payload, _ = stored_inputs.cot_row()
    assert payload["asset_manager"] == {"long": 500_000.0, "short": None, "net": None, "change": 1_500.0}
    assert payload["dealer"]["net"] is None
    assert payload["leveraged_funds"]["net"] == 40_000.0


# ── 3. stale is "nothing stored": the caller must ask the provider ───────────────────────────────────────────────

def test_a_vol_index_session_past_max_lag_is_not_read():
    _seed_vol(sessions_back(4))                      # MAX_LAG["vol_index_daily"] is 3
    assert stored_inputs.vol_index_rows() == ({}, None)


def test_a_cot_session_past_max_lag_is_not_read():
    _seed_cot(sessions_back(11))                     # MAX_LAG["cftc_cot"] is 10
    assert stored_inputs.cot_row() == ({}, None)


def test_a_weekly_cot_row_six_sessions_old_is_still_current():
    """The real cadence: CFTC publishes on Friday the Tuesday positions, so a current row is ~6 sessions old."""
    _seed_cot(sessions_back(6))
    payload, session = stored_inputs.cot_row()
    assert session == sessions_back(6) and payload["report_date"] == sessions_back(6).isoformat()


# ── 4. the pinned lags ───────────────────────────────────────────────────────────────────────────────────────────

def test_the_two_lags_are_the_spec_values():
    assert stored_inputs.MAX_LAG["vol_index_daily"] == 3            # a daily series, like short interest
    assert stored_inputs.MAX_LAG["cftc_cot"] == 10                  # a weekly series, published three days late


# ── 5. an unreadable store is a note, not a failure ──────────────────────────────────────────────────────────────

def test_a_locked_table_notes_once_and_reads_as_nothing(monkeypatch, caplog):
    def locked(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(rawstore, "query", locked)
    with caplog.at_level(logging.INFO, logger=stored_inputs.__name__):
        assert stored_inputs.vol_index_rows() == ({}, None)
        assert stored_inputs.cot_row() == ({}, None)
        assert stored_inputs.vol_index_rows() == ({}, None)        # a second read adds no second note

    notes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stored inputs:")]
    assert len(notes) == 2
    assert "vol_index_daily" in notes[0] and "database is locked" in notes[0]
    assert "cftc_cot" in notes[1]
