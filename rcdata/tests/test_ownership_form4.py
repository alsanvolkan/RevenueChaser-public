"""``sec/form4``: Form 4 and 4/A through the shared EDGAR walk (a filing listed under the issuer and each owner is
fetched once), one row per reported transaction, non-derivative and derivative, the issuer named by the spine
(its symbol when the spine's CIK for it agrees, else the CIK's one ticker), and a filing that does not parse kept as
one ``unparsed`` row, never dropped."""
from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import ops, read, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.ownership import form4, form345_sets, sec, sec13f_sets

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


def test_the_read_api_gives_a_tickers_transactions_with_provenance(feed):
    assert read.insider_transactions(["MSFT"]) == []                     # nothing built yet: no rows
    _run()
    rows = read.insider_transactions(["msft", "gme"])
    assert [(r["ticker"], r["code"], r["tx_table"]) for r in rows] == [
        ("GME", "S", "non_derivative"), ("MSFT", "S", "non_derivative"), ("MSFT", "M", "derivative")]
    assert {r["status"] for r in rows} == {ops.latest_check(form4.NAME)["result"]}
    assert {r["source"] for r in rows} == {form4.NAME} and all(r["run_id"] for r in rows)
    assert read.insider_transactions(["MSFT"], start=date(2026, 10, 28)) == []        # filed 27 Oct
    assert read.insider_transactions(["AAPL"]) == []                     # its one filing did not parse


SETS_PAGE = "/data-research/sec-markets-data/insider-transactions-data-sets"
SET_HREFS = ["/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q3_form345.zip",
             "/files/structureddata/data/insider-transactions-data-sets/2026q2_form345.zip",
             "/files/structureddata/data/insider-transactions-data-sets/2013q1_form345.zip"]


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv", "ACCESSION_NUMBER\tDOCUMENT_TYPE\n0001234567-26-000001\t4\n")
    return buf.getvalue()


def _backfill(start: str, end: str) -> dict:
    return base.run_request({"dataset": form4.NAME, "params": {"mode": "backfill", "from": start, "to": end}},
                            sleep=lambda s: None)


def test_a_backfill_keeps_the_insider_data_sets_and_walks_only_the_days_after_them(feed, http_mock):
    oct1 = "2026-10-01"
    feed.add_day(oct1, [(ox.idx_row(789019, "MICROSOFT CORP", "4", oct1, "0001234567-26-000011"),
                         ox.f4_filing("0001234567-26-000011", oct1, 789019, "MICROSOFT CORP", "MSFT", [CEO],
                                      non_derivative=[ox.f4_transaction("S", 10, 500.0, after=49990)]))])
    feed.install(http_mock)
    http_mock.add(SETS_PAGE, ox.sets_page(SET_HREFS))
    for href in SET_HREFS[:2]:
        http_mock.add(href, _zip(), headers={"last-modified": "Mon, 14 Sep 2026 16:43:16 GMT"})
    row = _backfill("2026-09-01", "2026-10-02")
    assert row["result"] == "changed", row["note"]
    asked = [str(c.url) for c in http_mock.calls("_form345.zip")]
    assert asked == [f"https://sec.test{SET_HREFS[0]}"]            # only the set that covers the range, its own folder
    kept = sorted(p.name for p in sec13f_sets.raw_dir(form345_sets.SUB).glob("*.zip"))
    assert kept == ["2026q3_form345.zip"]
    # The walk starts after the newest set's window, not at the requested first day.
    assert [str(c.url).rsplit(".", 2)[1] for c in http_mock.calls("/daily-index/")] == ["20261002", "20261001"]
    assert "insider transactions data sets: kept as raw up to 2026-09-30, not typed yet" in row["note"]
    assert _served("SELECT accession FROM insider_transactions") == [("0001234567-26-000011",)]
