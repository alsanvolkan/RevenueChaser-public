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


Q3, Q4 = "2026q3_form345.zip", "2026q4_form345.zip"
SET_HREFS = [f"/files/datastandardsinnovation/data/insider-transactions-data-sets/{Q4}",
             f"/files/structureddata/data/insider-transactions-data-sets/{Q3}",
             "/files/structureddata/data/insider-transactions-data-sets/2013q1_form345.zip"]
BOSS = (1234567, "DOE JANE", "Director,Officer", "Chief Executive Officer")
HOLDER = (7654321, "BIG HOLDER LP", "TenPercentOwnerOther", "")


def _q3() -> bytes:
    return ox.f345_set([
        ox.f345_filing("0001234567-26-000021", "15-SEP-2026", 789019, "MICROSOFT CORP", "MSFT", [BOSS, HOLDER],
                       non_derivative=[ox.f345_tx("S", 1000, 510.25, after=50000, date="14-SEP-2026"),
                                       ox.f345_tx("P", 300, None, ad="A", after=50300, date="14-SEP-2026")],
                       derivative=[ox.f345_tx("M", 2000, 0, after=0, date="14-SEP-2026", exercise=100,
                                              underlying=2000, expires="01-JAN-2030", title="Stock Option")],
                       aff10b5one="1"),
        # Form 3 and Form 5 are in the same set; only sec/form4's own forms are typed, but the newest filing that
        # bounds the set's days is read across all of them, so this one carries the quarter to its end.
        ox.f345_filing("0001234567-26-000022", "30-SEP-2026", 789019, "MICROSOFT CORP", "MSFT", [BOSS], form="3"),
        ox.f345_filing("0007654321-26-000023", "16-SEP-2026", 14693, "BROWN FORMAN CORP", "BF.B", [HOLDER],
                       form="4/A"),
    ], folder="2026Q3_FORM345")


def _q4() -> bytes:
    """The open-window set: published while the quarter runs, so its newest filing is 2 October."""
    return ox.f345_set([
        ox.f345_filing("0001234567-26-000031", "02-OCT-2026", 789019, "MICROSOFT CORP", "MSFT", [BOSS],
                       non_derivative=[ox.f345_tx("A", 5, 1.0, ad="A", after=50305, date="01-OCT-2026")]),
    ], without=("AFF10B5ONE",))


@pytest.fixture
def sets(feed, http_mock):
    http_mock.add(ox.F345_PAGE_PATH, ox.sets_page(SET_HREFS))
    http_mock.add(SET_HREFS[0], _q4())
    http_mock.add(SET_HREFS[1], _q3(), headers={"last-modified": "Mon, 14 Sep 2026 16:43:16 GMT"})
    oct5 = "2026-10-05"
    feed.add_day(oct5, [(ox.idx_row(789019, "MICROSOFT CORP", "4", oct5, "0001234567-26-000041"),
                         ox.f4_filing("0001234567-26-000041", oct5, 789019, "MICROSOFT CORP", "MSFT", [CEO],
                                      non_derivative=[ox.f4_transaction("S", 10, 500.0, after=49990)]))])
    feed.install(http_mock)
    return feed


def _backfill(start: str, end: str) -> dict:
    return base.run_request({"dataset": form4.NAME, "params": {"mode": "backfill", "from": start, "to": end}},
                            sleep=lambda s: None)


def test_the_sets_fill_their_days_and_the_forms_walk_reads_only_what_they_leave(sets, http_mock):
    row = _backfill("2026-09-01", "2026-10-05")
    assert row["result"] == "changed", row["note"]
    asked = [str(c.url) for c in http_mock.calls("_form345.zip")]
    assert sorted(u.rsplit("/", 1)[1] for u in asked) == [Q3, Q4]                      # not 2013q1
    assert f"https://sec.test{SET_HREFS[0]}" in asked                   # resolved against the page, its own folder
    assert sorted(p.name for p in sec13f_sets.raw_dir(form345_sets.SUB).glob("*.zip")) == [Q3, Q4]
    # The open-window set holds nothing after 2 October, so 5 October is left to the walk; nothing else is asked.
    assert [str(c.url).rsplit(".", 2)[1] for c in http_mock.calls("/daily-index/")] == ["20261005"]
    assert form4.DATASET.first_day() == date(2026, 9, 1)
    assert form4.DATASET.is_done(date(2026, 9, 15)) and form4.DATASET.is_done(date(2026, 10, 1))
    assert f"{form345_sets.LABEL} data set {Q4}: its newest filing is 2026-10-02" in row["note"]
    assert _served("SELECT accession, form, filed, issuer_cik, ticker, owner_cik, owner_name, owners, is_director, "
                   "is_officer, officer_title, is_ten_pct, is_other, aff10b5one, tx_table, line, status "
                   "FROM insider_transactions ORDER BY filed, accession, tx_table, line") == [
        ("0001234567-26-000021", "4", date(2026, 9, 15), "789019", "MSFT", "1234567", "DOE JANE", 2, True, True,
         "Chief Executive Officer", False, False, True, "derivative", 0, "ok"),
        ("0001234567-26-000021", "4", date(2026, 9, 15), "789019", "MSFT", "1234567", "DOE JANE", 2, True, True,
         "Chief Executive Officer", False, False, True, "non_derivative", 0, "ok"),
        ("0001234567-26-000021", "4", date(2026, 9, 15), "789019", "MSFT", "1234567", "DOE JANE", 2, True, True,
         "Chief Executive Officer", False, False, True, "non_derivative", 1, "ok"),
        ("0007654321-26-000023", "4/A", date(2026, 9, 16), "14693", "BF-B", "7654321", "BIG HOLDER LP", 1, False,
         False, None, True, True, None, None, None, "no_transaction"),
        ("0001234567-26-000031", "4", date(2026, 10, 2), "789019", "MSFT", "1234567", "DOE JANE", 1, True, True,
         "Chief Executive Officer", False, False, None, "non_derivative", 0, "ok"),
        # The last one the walk typed from the form itself, where aff10b5One is a real element.
        ("0001234567-26-000041", "4", date(2026, 10, 5), "789019", "MSFT", "1234567", "DOE JANE", 1, True, True,
         "Chief Executive Officer", False, False, True, "non_derivative", 0, "ok")]
    # The transaction's own values, including the price a footnote carries (blank in the set, NULL here).
    assert _served("SELECT tx_table, line, code, transaction_date, shares, price, acquired_disposed, shares_after, "
                   "direct_indirect, security_title, exercise_price, underlying_title, underlying_shares, "
                   "expiration_date FROM insider_transactions WHERE accession = '0001234567-26-000021' "
                   "ORDER BY tx_table, line") == [
        ("derivative", 0, "M", date(2026, 9, 14), 2000.0, 0.0, "D", 0.0, "D", "Stock Option", 100.0, "Stock Option",
         2000.0, date(2030, 1, 1)),
        ("non_derivative", 0, "S", date(2026, 9, 14), 1000.0, 510.25, "D", 50000.0, "D", "Common Stock", None, None,
         None, None),
        ("non_derivative", 1, "P", date(2026, 9, 14), 300.0, None, "A", 50300.0, "D", "Common Stock", None, None,
         None, None)]
    assert f"no {form345_sets.AFF} column, aff10b5one is NULL for it" in row["note"]


def test_a_short_backfill_reads_the_forms_and_asks_for_no_set(sets, http_mock):
    row = _backfill("2026-10-01", "2026-10-05")
    assert row["result"] in ("changed", "unchanged"), row["note"]
    assert http_mock.calls("_form345.zip") == []
    assert "the data sets fill a range of" in row["note"]


def test_a_set_already_held_is_asked_again_only_if_modified(sets, http_mock):
    _backfill("2026-09-01", "2026-10-05")
    before = sec13f_sets.raw_path(Q3, form345_sets.SUB).stat().st_mtime_ns
    http_mock.routes.clear()
    http_mock.requests.clear()
    http_mock.add(ox.F345_PAGE_PATH, ox.sets_page(SET_HREFS))
    http_mock.add(SET_HREFS[1], b"", status=304, headers={"last-modified": "Mon, 14 Sep 2026 16:43:16 GMT"})
    http_mock.add(SET_HREFS[0], _q4())
    sets.install(http_mock)
    row = _backfill("2026-09-01", "2026-10-05")
    assert row["result"] == "unchanged", row["note"]
    asked = [c for c in http_mock.calls(Q3)]
    assert len(asked) == 1 and asked[0].headers["if-modified-since"] == "Mon, 14 Sep 2026 16:43:16 GMT"
    assert sec13f_sets.raw_path(Q3, form345_sets.SUB).stat().st_mtime_ns == before
