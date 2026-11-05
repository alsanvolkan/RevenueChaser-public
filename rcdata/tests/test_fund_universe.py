"""Which companies the fundamentals build asks about: avo's own list when the spine holds one (avo, 5 Nov 2026).

The boards that read ``fundamentals.duckdb`` (Fundamentals, Asymmetric Growth, Financials, Relatedness, Value
Chain) all screen operating companies, and since this change their universe is avo's list too
(``core/universe.py``). The build has to ask about the same names, or a card on the list would read "not
available" and a company off it would cost a fetch nobody reads.
"""
from __future__ import annotations

import duckdb

from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build.fundamentals import common
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built, run_all  # noqa: F401 - the 10 names


def _list(rows: list[tuple[str, str]]) -> None:
    con = duckdb.connect(str(serving.live_path("spine")))
    try:
        con.execute("DELETE FROM my_universe")
        if rows:
            con.executemany("INSERT INTO my_universe (ticker, kind) VALUES (?, ?)", rows)
    finally:
        con.close()


def _no_list() -> None:
    con = duckdb.connect(str(serving.live_path("spine")))
    try:
        con.execute("DROP TABLE my_universe")
    finally:
        con.close()


def test_without_the_list_tier_a_decides(built):
    """A spine built before schema 5: the old rule, every Tier A company with a CIK."""
    _no_list()
    assert {r["ticker"] for r in common.universe()} >= {"MSFT", "WFC"}


def test_the_list_decides_which_companies_are_asked_about(built):
    _list([("MSFT", "stock"), ("SPY", "etf")])
    assert [r["ticker"] for r in common.universe()] == ["MSFT"], "the list only, and no ETF"


def test_a_fund_on_the_list_is_never_a_company(built):
    """An ETF has no statements to ask the SEC for, so ``kind`` 'etf' never reaches a fundamentals fetch."""
    _list([("SPY", "etf")])
    assert common.universe() == []


def test_an_empty_list_asks_about_nobody(built):
    _list([])
    assert common.universe() == []
