"""#485: the ticker box's names come from the spine's ``security`` (need ``identity.universe``) through
``rcdata.read``, never from ``equity_universe``; an empty or failed read is one line in the box's place."""
from __future__ import annotations

import duckdb
import pytest

from RevenueChaser.module_settings import inventory, report
from RevenueChaser.rcdata import read as rcread, serving
from RevenueChaser.rcdata.tests.conftest import market_roots  # noqa: F401 - market roots per test


def _spine(rows):
    path = serving.live_path("spine")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE security (ticker TEXT, name TEXT, tier TEXT, source_as_of DATE)")
        for ticker, name, tier in rows:
            con.execute("INSERT INTO security VALUES (?, ?, ?, DATE '2026-10-28')", [ticker, name, tier])
    finally:
        con.close()


def test_the_names_come_from_the_spine(market_roots):
    _spine([("MU", "Micron Technology", "A"), ("AAPL", "Apple Inc.", "A"), ("ZZZ", None, "A")])
    names, error = inventory._universe_tickers()
    assert error is None
    assert names == [{"ticker": "AAPL", "name": "Apple Inc."}, {"ticker": "MU", "name": "Micron Technology"},
                     {"ticker": "ZZZ", "name": "ZZZ"}]


def test_no_spine_reads_as_no_names(market_roots):
    assert inventory._universe_tickers() == ([], None)


def test_a_failed_read_carries_its_reason(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(rcread, "universe", boom)
    names, error = inventory._universe_tickers()
    assert names == [] and "disk gone" in error


def _drill(html: str) -> str:
    return html[html.index('id="ticker-drill"'):html.index('id="drill"')]


def test_an_empty_read_shows_one_line_instead_of_an_empty_box():
    block = _drill(report.render_settings_dashboard({"rows": [], "tickers": [], "newdata": None}))
    assert "the spine holds no names" in block
    assert 'id="ticker-combo"' not in block


def test_a_failed_read_shows_its_reason_instead_of_an_empty_box():
    block = _drill(report.render_settings_dashboard({"rows": [], "tickers": [], "tickers_error": "OSError: <gone>",
                                                     "newdata": None}))
    assert "the spine could not be read: OSError: &lt;gone&gt;" in block
    assert 'id="ticker-combo"' not in block


def test_names_keep_the_box():
    block = _drill(report.render_settings_dashboard({"rows": [], "newdata": None,
                                                     "tickers": [{"ticker": "MU", "name": "Micron"}]}))
    assert 'id="ticker-combo"' in block and "spine" not in block
