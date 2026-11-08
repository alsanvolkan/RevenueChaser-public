"""The boards' universe follows avo's own list when the spine holds one (avo, 5 Nov 2026).

``prices/universe.py`` has read ``my_universe`` since #417, so the prices store already holds avo's list and
nothing else. The boards did not: ``core/universe.py`` read the spine's Tiers A and B whole, so a name avo had
taken out still had a card, with no price and no fundamentals behind it. One list, both sides.
"""
from __future__ import annotations

import duckdb
import pytest

from RevenueChaser.core import rawstore, spine_reads, universe
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built, run_all  # noqa: F401 - the 10 names

CAPS = {"MSFT": 3.1e12, "ETN": 1.3e11, "HEI": 3.0e10, "HEI-A": 2.0e10, "NVR": 2.5e10, "TM": 2.4e11,
        "CNA": 1.2e10, "FDX": 6.5e10, "WFC": 2.3e11, "SPY": 6.0e11, "WFC-PC": 1.0e9}


@pytest.fixture
def caps():
    rawstore.write_rows("equity_universe", [{"ticker": t, "market_cap": c, "price": 10.0} for t, c in CAPS.items()])
    rawstore.write_rows("security_master", [{"ticker": "SPY", "security_type": "etp"},
                                            {"ticker": "WFC-PC", "security_type": "preferred"}])


def _list(rows: list[tuple[str, str]]) -> None:
    """The live spine's ``my_universe`` table holds exactly ``rows`` (ticker, kind)."""
    con = duckdb.connect(str(serving.live_path("spine")))
    try:
        con.execute("DELETE FROM my_universe")
        if rows:
            con.executemany("INSERT INTO my_universe (ticker, kind) VALUES (?, ?)", rows)
    finally:
        con.close()
    spine_reads.clear()


def _no_list() -> None:
    """A spine built before schema 5: no ``my_universe`` table at all."""
    con = duckdb.connect(str(serving.live_path("spine")))
    try:
        con.execute("DROP TABLE my_universe")
    finally:
        con.close()
    spine_reads.clear()


def test_without_the_list_the_tiers_decide(built, caps):
    """No ``my_universe`` table (a spine older than schema 5): Tiers A and B whole, exactly as before."""
    _no_list()
    assert spine_reads.my_universe_tickers() is None
    assert "SPY" in universe.classification_universe(0), "an excluded type is in the universe (PR 2)"
    assert len(universe.classification_universe(0)) == len(spine_reads.universe(("A", "B")))


def test_the_list_is_the_universe_for_the_boards(built, caps):
    _list([("MSFT", "stock"), ("TM", "stock"), ("SPY", "etf")])
    assert spine_reads.my_universe_tickers() == {"MSFT", "TM", "SPY"}
    assert universe.classification_universe(0) == ["MSFT", "SPY", "TM"], "cap order, the list only"
    assert universe.classification_universe(0, operating_only=True) == ["MSFT", "TM"], "the ETF leaves that board"


def test_a_name_the_tiers_keep_but_the_list_drops_has_no_card(built, caps):
    """The point of the change: ``WFC`` is Tier A and was always on every board; off the list, it is off them."""
    _list([("MSFT", "stock")])
    assert "WFC" not in universe.classification_universe(0)
    rows = universe.classification_universe_rows(0)
    assert [r["ticker"] for r in rows] == ["MSFT"]
    assert rows[0]["market_cap"] == CAPS["MSFT"] and rows[0]["security_type"] == "common"


def test_the_funnel_counts_the_list_and_not_the_tiers(built, caps):
    """``universe_counts`` and the boards must never report different sets (the funnel's own contract)."""
    _list([("MSFT", "stock"), ("SPY", "etf")])
    counts = universe.universe_counts(0)
    assert counts["scanned_total"] == 2 and counts["classification_total"] == 2
    assert counts["funds_etfs"] == 1 and counts["operating_total"] == 1


def test_an_empty_list_is_an_empty_universe(built, caps):
    """A table that exists and is empty is avo having taken everything out, not a missing list."""
    _list([])
    assert spine_reads.my_universe_tickers() == set()
    assert universe.classification_universe(0) == []
