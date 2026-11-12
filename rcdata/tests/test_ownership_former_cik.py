"""A company that gets a new SEC CIK keeps its ownership rows under the ticker (#749).

A reorganisation or a redomicile gives the new registrant a new CIK; the ticker moves to it and the filing history
stays on the old one, while filers go on citing the old CIK for years (Exxon Mobil: 1,120 insider rows under the
old CIK, 0 under the new one, measured 12 Nov 2026 in #749). The spine already records the change, because
``security_history`` holds each ticker's CIK per SEC snapshot; the ownership naming now reads it, so the insider and
the beneficial-ownership boards serve those rows under the ticker instead of ``ticker = NULL``.

The pair used here (XOM, old CIK 34088 -> new CIK 2006049) comes from edgartools' hand-reviewed successor list
(``edgar/reference/data/successor_registrants.csv``, MIT), cited in #749; no such list enters this repository.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.ownership import common, form4, sec

from . import ownership_fixture as ox

XOM_OLD, XOM_NEW = "34088", "2006049"
TICKERS = ox.SPINE_TICKERS + ("XOM",)
CIKS = {**ox.SPINE_CIKS, "XOM": XOM_NEW}
# One ``security_history`` row: XOM filed under the old CIK until the reorganisation, and no longer does.
XOM_HISTORY = (("XOM", "cik", XOM_OLD, date(2014, 1, 6), date(2026, 7, 15)),)


def _spine(**kwargs) -> dict[str, str]:
    ox.make_spine(tickers=TICKERS, ciks=CIKS, **kwargs)
    return common.spine_former_ciks()


# ── The map the spine's history gives ──────────────────────────────────────────────────────────────────────────

def test_a_cik_the_ticker_no_longer_files_under_is_a_former_cik():
    assert _spine(history=XOM_HISTORY) == {XOM_OLD: "XOM"}


def test_a_spine_that_carries_no_history_yet_names_no_former_cik():
    assert _spine() == {}


def test_a_cik_that_is_still_some_tickers_current_cik_is_not_a_former_one():
    """A ticker that only left an SEC snapshot and came back under the same CIK has an ended period too."""
    assert _spine(history=(("MSFT", "cik", "789019", date(2024, 1, 1), date(2024, 2, 1)),)) == {}


def test_a_cik_several_tickers_once_filed_under_names_nothing():
    """A reused ticker must not inherit another company's rows, so an ambiguous CIK is left out."""
    history = (("XOM", "cik", XOM_OLD, date(2014, 1, 6), date(2026, 7, 15)),
               ("GME", "cik", XOM_OLD, date(2010, 1, 6), date(2013, 7, 15)))
    assert _spine(history=history) == {}


def test_a_period_that_still_holds_is_not_a_former_cik():
    assert _spine(history=(("XOM", "cik", XOM_OLD, date(2014, 1, 6), None),)) == {}


# ── How a pair is named ────────────────────────────────────────────────────────────────────────────────────────

SPINE = {"XOM": XOM_NEW, "MSFT": "789019", "GME": "1326380"}
FORMER = {XOM_OLD: "XOM"}


def test_a_filing_under_the_old_cik_is_named_by_it():
    named = common.issuer_tickers([(XOM_OLD, "XOM"), (XOM_OLD, None)], SPINE, FORMER)
    assert named[(XOM_OLD, "XOM")] == ("XOM", common.ISSUER_FORMER_CIK)
    assert named[(XOM_OLD, None)] == ("XOM", common.ISSUER_FORMER_CIK)
    # Without the map (a spine that carries no history) nothing changes: what the boards show today.
    assert common.issuer_tickers([(XOM_OLD, "XOM")], SPINE)[(XOM_OLD, "XOM")] == (None, None)


def test_the_current_cik_still_names_by_the_symbol_and_an_unknown_cik_names_nothing():
    named = common.issuer_tickers([(XOM_NEW, "XOM"), ("999999", "XOM")], SPINE, FORMER)
    assert named[(XOM_NEW, "XOM")] == ("XOM", common.EXACT)
    assert named[("999999", "XOM")] == (None, None)


def test_a_symbol_of_another_issuer_is_not_taken_from_a_former_cik():
    """The CIK decides, as before: a filing under XOM's old CIK that mistypes the symbol is still XOM's."""
    assert common.issuer_tickers([(XOM_OLD, "MSFT")], SPINE, FORMER)[(XOM_OLD, "MSFT")] == (
        "XOM", common.ISSUER_FORMER_CIK)


# ── The board (Form 4 through the real build) ──────────────────────────────────────────────────────────────────

DAY = "2026-10-27"
CEO = (1234567, "DOE JANE", {"dir": 1, "off": "Chief Executive Officer"})


@pytest.fixture
def feed(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec, "_shared_pace", lambda: True)
    e = ox.Edgar()
    filing = ox.f4_filing("0000034088-26-000001", DAY, int(XOM_OLD), "EXXON MOBIL CORP", "XOM", [CEO],
                          non_derivative=[ox.f4_transaction("S", 1000, 110.5, after=50000)])
    e.add_day(DAY, [(ox.idx_row(int(XOM_OLD), "EXXON MOBIL CORP", "4", DAY, "0000034088-26-000001"), filing),
                    (ox.idx_row(1234567, "DOE JANE", "4", DAY, "0000034088-26-000001"), None)])
    e.install(http_mock)
    return e


def _served(sql: str) -> list[tuple]:
    with serving.open_read("ownership") as con:
        return con.execute(sql).fetchall()


SQL = ("SELECT issuer_cik, issuer_symbol, ticker, ticker_source FROM insider_transactions "
       "WHERE accession = '0000034088-26-000001'")


def test_an_insider_row_filed_under_the_old_cik_is_served_under_the_ticker(feed):
    ox.make_spine(tickers=TICKERS, ciks=CIKS, history=XOM_HISTORY)
    assert base.run_request({"dataset": form4.NAME, "params": {"as_of": "2026-10-28"}},
                            sleep=lambda s: None)["result"] == "changed"
    assert _served(SQL) == [(XOM_OLD, "XOM", "XOM", common.ISSUER_FORMER_CIK)]

