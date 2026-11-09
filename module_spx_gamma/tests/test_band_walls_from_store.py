"""#291: the DTE-band walls come from the market store, and give the same walls the day's own files do.

The board computed those walls from its per-expiry raw files; the store now holds the same endpoint's answer for
the whole chain (`uw/greek_expiry_strike`, #384). Measured on the live store for 2026-11-03 and 2026-11-04, the
two agree on all 24 band/wall strikes; the Net GEX beside a wall can differ on a third Friday (the store sums
SPX and SPXW rows at one strike, the files keep the first). These tests hold that property in
place and cover the fallback for a day the store does not have.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.module_spx_gamma import store_reads
from RevenueChaser.module_spx_gamma.levels import DTE_BANDS, band_walls
from RevenueChaser.module_spx_gamma.normalize import normalize_stored_greek_grid

from . import rcdata_fixture as fx

DAY = fx.LAST
SPOT = 6800.0


def _file_rows(session: date) -> list[dict]:
    """The same grid in the shape the day's own files normalize to."""
    return [{"expiry": r["expiry"], "dte": float(r["dte"]), "strike": float(r["strike"]),
             "call_gex": float(r["call_gex"]), "put_gex": float(r["put_gex"]),
             "net_gamma": float(r["call_gex"]) + float(r["put_gex"])}
            for r in fx.greek_rows(session)]


def test_the_stored_grid_gives_the_same_wall_as_the_days_own_files(rcdata_market):
    """The property the repoint rests on, band by band."""
    grid = store_reads.greek_expiry_grid(DAY)
    assert grid["stored"] is True, grid["label"]
    from_store = band_walls(normalize_stored_greek_grid(grid["rows"]), SPOT)
    from_files = band_walls(_file_rows(DAY), SPOT)
    assert [r["band"] for r in from_store] == [r["band"] for r in from_files]
    for a, b in zip(from_store, from_files):
        assert (a["put_wall"], a["call_wall"]) == (b["put_wall"], b["call_wall"]), a["band"]
        assert (a["put_wall_net"], a["call_wall_net"]) == (b["put_wall_net"], b["call_wall_net"]), a["band"]


def test_every_band_the_board_reads_has_rows_in_the_stored_grid(rcdata_market):
    """A band with no stored expiry would quietly answer no wall, so the fixture covers all of them."""
    rows = normalize_stored_greek_grid(store_reads.greek_expiry_grid(DAY)["rows"])
    for label, keep in DTE_BANDS:
        assert [r for r in rows if keep(r["dte"])], label


def test_the_grid_keeps_uws_calendar_dte(rcdata_market):
    """`levels.DTE_BANDS` are calendar-day bands; a session-counted dte would move every edge."""
    rows = normalize_stored_greek_grid(store_reads.greek_expiry_grid(DAY)["rows"])
    assert {r["dte"] for r in rows} == {0.0, 25.0, 80.0, 150.0, 300.0, 500.0}


def test_a_day_the_store_does_not_hold_is_not_stored_not_an_error(rcdata_market):
    """The caller then falls back to the day's own files, so a backfill gap never empties the bands.

    A session the fixture never served stands in for the gap: the table is there, the day is not in it.
    """
    missing = date(2026, 10, 19)                     # the Monday before the fixture's first session
    assert missing not in fx.SESSIONS
    grid = store_reads.greek_expiry_grid(missing)
    assert grid["stored"] is False
    assert grid["rows"] == [] and grid["failed"] is False
    assert "not stored" in grid["label"] and str(missing) in grid["label"]


def test_the_label_says_what_it_served(rcdata_market):
    label = store_reads.greek_expiry_grid(DAY)["label"]
    assert "greek grid of" in label and "expiries" in label
    assert store_reads.GREEK_SOURCE in label


@pytest.mark.parametrize("bad", [[], [{"strike": None}], [{"expiry": "2026-11-20"}]])
def test_an_unusable_row_is_dropped_rather_than_crashing(bad):
    assert normalize_stored_greek_grid(bad) == []


def test_a_locked_ops_file_is_a_failed_grid_read_never_a_failed_board(rcdata_market, monkeypatch):
    """Review of #436, B1: a store error while the serving file exists used to re-raise out of ``read_day`` and
    fail the whole Fetch; it is a failed read of this one input now, and the bands fall back to the files."""
    import sqlite3
    real = store_reads.needs.ask

    def locked(need, *a, **k):
        if need == "market.spx_greek_expiry_strike":
            raise sqlite3.OperationalError("database is locked")
        return real(need, *a, **k)
    monkeypatch.setattr(store_reads.needs, "ask", locked)
    grid = store_reads.greek_expiry_grid(DAY)
    assert grid["stored"] is False and "database is locked" in grid["label"]
    out = store_reads.read_day(DAY)                              # the board's own entry point: it must not raise
    assert out["store_inputs"]["greek_grid"]["stored"] is False


def test_normalize_takes_the_bands_from_the_stored_grid_and_says_so():
    """Review of #436, S2: the switch in ``normalize_snapshot`` itself, not only the reader."""
    from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot
    rows = fx.greek_rows(DAY)
    spot = {"spot": {"value": SPOT}}
    stored = normalize_snapshot({**spot, "greek_grid": {"stored": True, "rows": rows, "source": "rcdata:test"}},
                                DAY.isoformat())
    files = normalize_snapshot(dict(spot), DAY.isoformat())
    assert stored["dte_band_source"] == "rcdata:test"
    assert files["dte_band_source"] == "raw day files"
    want = band_walls(normalize_stored_greek_grid(rows), SPOT)
    assert stored["dte_band_walls"] == want and any(r["put_wall"] not in (None, "N/A") for r in want)
    assert files["dte_band_walls"] != want, "with no stored grid the bands come from the (here empty) files"
    not_stored = normalize_snapshot({**spot, "greek_grid": {"stored": False, "rows": rows, "source": "rcdata:test"}},
                                    DAY.isoformat())
    assert not_stored["dte_band_source"] == "raw day files"
