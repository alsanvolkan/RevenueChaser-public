"""``sec/form4``: Form 4 and 4/A through the shared EDGAR walk (a filing listed under the issuer and each owner is
fetched once), one row per reported transaction, non-derivative and derivative, the issuer named by the spine
(its symbol when the spine's CIK for it agrees, else the CIK's one ticker), and a filing that does not parse kept as
one ``unparsed`` row, never dropped."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.ownership import form4, sec

from . import ownership_fixture as ox

DAY = "2026-10-27"
CEO = (1234567, "DOE JANE", {"dir": 1, "off": "Chief Executive Officer"})
FUND = (7654321, "BIG HOLDER LP", {"ten": 1})


@pytest.fixture
def feed(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec, "_shared_pace", lambda: True)
    e = ox.Edgar()
    sale = ox.f4_filing("0001234567-26-000001", DAY, 789019, "MICROSOFT CORP", "MSFT", [CEO],
                        non_derivative=[ox.f4_transaction("S", 1000, 510.25, after=50000)],
                        derivative=[ox.f4_transaction("M", 2000, 0, ad="D", after=0, derivative=True, exercise=100,
                                                      underlying=2000, expires="2030-01-01",
                                                      title="Stock Option")])
    buy = ox.f4_filing("0007654321-26-000002", DAY, 14693, "BROWN FORMAN CORP", "BFB", [FUND],
                       non_derivative=[ox.f4_transaction("P", 300, None, ad="A", after=9300, price_footnote=True)],
                       form="4/A")
    wrong = ox.f4_filing("0007654321-26-000003", DAY, 1326380, "GAMESTOP CORP", "MSFT", [FUND],
                         non_derivative=[ox.f4_transaction("S", 5, 20.0, after=0)])
    e.add_day(DAY, [
        (ox.idx_row(789019, "MICROSOFT CORP", "4", DAY, "0001234567-26-000001"), sale),
        (ox.idx_row(1234567, "DOE JANE", "4", DAY, "0001234567-26-000001"), None),
        (ox.idx_row(14693, "BROWN FORMAN CORP", "4/A", DAY, "0007654321-26-000002"), buy),
        (ox.idx_row(1326380, "GAMESTOP CORP", "4", DAY, "0007654321-26-000003"), wrong),
        (ox.idx_row(320193, "APPLE INC", "4", DAY, "0000320193-26-000009"),
         ox.submission("0000320193-26-000009", "4", DAY, [("ISSUER", 320193, "APPLE INC")],
                       [("4", "form4.htm", None)])),
        (ox.idx_row(320193, "APPLE INC", "3", DAY, "0000320193-26-000010"), None),
    ])
    e.install(http_mock)
    ox.make_spine()
    return e


def _run(day: str = "2026-10-28") -> dict:
    return base.run_request({"dataset": form4.NAME, "params": {"as_of": day}}, sleep=lambda s: None)


def _served(sql: str, *args) -> list[tuple]:
    with serving.open_read("ownership") as con:
        return con.execute(sql, list(args)).fetchall()


def test_each_form4_is_fetched_once_and_form3_is_not(feed, http_mock):
    row = _run()
    assert row["result"] == "changed"
    asked = sorted(str(c.url).rsplit("/", 1)[1] for c in http_mock.calls("/Archives/edgar/data/"))
    assert asked == ["0000320193-26-000009.txt", "0001234567-26-000001.txt", "0007654321-26-000002.txt",
                     "0007654321-26-000003.txt"]


def test_one_row_per_transaction_with_the_owner_and_the_issuer_named(feed):
    _run()
    assert _served("SELECT form, issuer_cik, ticker, ticker_source, owner_cik, owner_name, is_director, is_officer, "
                   "officer_title, tx_table, code, transaction_date, shares, price, acquired_disposed, shares_after, "
                   "exercise_price, underlying_shares, expiration_date FROM insider_transactions "
                   "WHERE accession = '0001234567-26-000001' ORDER BY tx_table DESC") == [
        ("4", "789019", "MSFT", "spine:exact", "1234567", "DOE JANE", True, True, "Chief Executive Officer",
         "non_derivative", "S", date(2026, 10, 23), 1000.0, 510.25, "D", 50000.0, None, None, None),
        ("4", "789019", "MSFT", "spine:exact", "1234567", "DOE JANE", True, True, "Chief Executive Officer",
         "derivative", "M", date(2026, 10, 23), 2000.0, 0.0, "D", 0.0, 100.0, 2000.0, date(2030, 1, 1))]


def test_a_squashed_symbol_is_named_when_the_spine_cik_agrees_and_a_footnoted_price_is_null(feed):
    _run()
    assert _served("SELECT form, issuer_symbol, ticker, ticker_source, is_ten_pct, code, price FROM "
                   "insider_transactions WHERE accession = '0007654321-26-000002'") == [
        ("4/A", "BFB", "BF-B", "spine:squash", True, "P", None)]


def test_a_symbol_whose_spine_cik_differs_is_named_by_the_issuer_cik(feed):
    _run()
    assert _served("SELECT issuer_symbol, ticker, ticker_source FROM insider_transactions "
                   "WHERE accession = '0007654321-26-000003'") == [("MSFT", "GME", "spine:cik")]


def test_a_filing_that_does_not_parse_is_one_unparsed_row(feed):
    _run()
    assert _served("SELECT status, issuer_cik, ticker, code FROM insider_transactions "
                   "WHERE accession = '0000320193-26-000009'") == [("unparsed", "320193", "AAPL", None)]
