"""The 13F board's own file, ``boards/sec_13f.duckdb`` (#732, owner 12 Nov 2026).

"13F panosu sonuclarini kendi pano duckdb sine yazmali, taki update etmesi gerekene kadar da oradan okumalali":
the board writes what it shows to its own file and reads it until the ownership build or the day changes. "Çeyrek SEC
o 3 ayı kapsayan zip dosyasini yazdiginda tam sayılsin ve uzerinde daha degisiklik almayacak sekilde kapansin": a
quarter the file holds as closed keeps its rows whatever a later build says.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import ownership, rawstore
from RevenueChaser.module_sec_13f import engine, store

Q1, Q2, Q3 = "2026-03-31", "2026-06-30", "2026-09-30"


def _cell(shares, complete=True, closed=None):
    c = {m: None for m in ownership.SEC13F_METRICS}
    c.update(shares=shares, holders=3, complete=complete)
    if closed is not None:
        c["closed"] = closed
    return c


def _brk(period, shares, cik="1", kind="stopped"):
    return {"period": period, "kind": kind, "shares": shares, "cik": cik, "filer_name": f"Fund {cik}"}


@pytest.fixture
def builder(monkeypatch):
    """The builder's rows and the live build's stamp, set per test."""
    state = {"totals": {}, "breaks": {}, "stamp": "build-1", "reads": 0}

    def totals():
        state["reads"] += 1
        return state["totals"]
    monkeypatch.setattr(ownership, "sec13f_totals", totals)
    monkeypatch.setattr(ownership, "sec13f_filer_breaks",
                        lambda tickers: {t: b for t, b in state["breaks"].items() if t in set(tickers)})
    monkeypatch.setattr(store, "source_stamp", lambda: state["stamp"])
    return state


def test_the_file_lives_beside_the_other_board_files():
    assert "sec_13f" in rawstore.BOARD_DBS
    assert {rawstore._db_of(t) for t in (store.CELLS, store.BREAKS, store.META)} == {"sec_13f"}


def test_a_sync_writes_the_builders_rows_and_the_board_then_reads_the_file(builder):
    builder["totals"] = {"AAA": {Q1: _cell(100.0, closed=True), Q2: _cell(110.0, closed=False)}}
    builder["breaks"] = {"AAA": [_brk(Q2, 5.0)]}

    totals, breaks, written = store.sync()
    assert written and totals == builder["totals"] and breaks == builder["breaks"]
    assert store.stored_totals() == builder["totals"] and store.stored_breaks() == builder["breaks"]

    reads = builder["reads"]
    assert engine.load_totals() == builder["totals"]
    assert builder["reads"] == reads                      # current: the board asked the builder nothing
    assert store.sync()[2] is False                       # and a second Refresh writes nothing


def test_a_new_build_is_taken_but_a_closed_quarter_keeps_its_rows(builder):
    builder["totals"] = {"AAA": {Q1: _cell(100.0, closed=True), Q2: _cell(110.0, closed=False)}}
    builder["breaks"] = {"AAA": [_brk(Q1, 4.0), _brk(Q2, 5.0)]}
    store.sync()

    # A later build restates the closed quarter, adds a ticker to it, and moves the open one.
    builder["stamp"] = "build-2"
    builder["totals"] = {"AAA": {Q1: _cell(999.0, closed=True), Q2: _cell(120.0, closed=True)},
                         "BBB": {Q1: _cell(7.0, closed=True), Q2: _cell(8.0, closed=True)}}
    builder["breaks"] = {"AAA": [_brk(Q1, 99.0, cik="9"), _brk(Q2, 6.0)]}
    totals, breaks, written = store.sync()

    assert written
    assert totals["AAA"][Q1]["shares"] == 100.0 and totals["AAA"][Q2]["shares"] == 120.0
    assert "BBB" in totals and Q1 not in totals["BBB"]
    assert sorted((b["period"], b["shares"]) for b in breaks["AAA"]) == [(Q1, 4.0), (Q2, 6.0)]
    assert store.stored_totals() == totals


def test_a_stale_file_is_read_through_without_a_write(builder):
    builder["totals"] = {"AAA": {Q1: _cell(100.0, closed=True)}}
    store.sync()
    builder["stamp"] = "build-2"
    builder["totals"] = {"AAA": {Q1: _cell(5.0, closed=True), Q2: _cell(6.0)}}
    assert engine.load_totals() == {"AAA": {Q1: _cell(100.0, closed=True), Q2: _cell(6.0)}}
    assert store.stored_stamp() == "build-1"              # the chart route reads; only a Refresh writes


def test_with_no_ownership_store_nothing_is_written(builder):
    builder["stamp"] = None
    builder["totals"] = {"AAA": {Q1: _cell(1.0)}}
    totals, _breaks, written = store.sync()
    assert written is False and totals == builder["totals"] and store.stored_totals() == {}


def test_an_empty_or_missing_file_reads_as_no_rows():
    assert store.stored_totals() == {} and store.stored_breaks() == {} and store.stored_stamp() is None
