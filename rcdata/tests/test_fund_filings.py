"""``sec/filings`` (build plan PR 12): the filing list and its 8-K items, the newest annual report and proxy texts with
the splitter's quality flag (never ``ok`` at the 200,000-character cap), and the list's read date (F2)."""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata.build.fundamentals import filings, tables

from . import fund_fixture as fx

CAP = 200_000


@pytest.fixture
def sec(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    fx.spine()
    return http_mock


def _msft(sec, ten_k=None, rows=None):
    fx.filing_routes(sec, {"789019": fx.submissions(789019, rows or fx.MSFT_FILINGS, "MICROSOFT CORP")},
                     {"msft-10k.htm": ten_k or fx.ten_k(), "msft-def14a.htm": fx.proxy()})


def test_the_cap_is_the_splitters():
    assert filings.max_text_chars() == CAP


@pytest.mark.parametrize("form", ["10-K", "20-F", "DEF 14A"])
def test_quality_is_never_ok_at_the_cap(form):
    html = fx.proxy(CAP + 50_000) if form == "DEF 14A" else fx.ten_k(CAP + 50_000)
    if form == "20-F":
        html = html.replace("Item 1. Business", "Item 4. Information on the Company").replace(
            "Item 1A. Risk Factors", "Item 4A. Unresolved Staff Comments")
    text, quality = filings.slice_text(html, form)
    assert len(text) == CAP and quality == "capped"
    small, quality = filings.slice_text(fx.ten_k(5_000) if form != "DEF 14A" else fx.proxy(5_000),
                                        "10-K" if form == "20-F" else form)
    assert quality == "ok" and len(small) < CAP


def test_the_served_table_refuses_ok_at_the_cap():
    con = duckdb.connect()
    tables.create(con, "filing_item")

    def insert(quality, chars):
        con.execute("INSERT INTO filing_item VALUES ('1', 'a', '10-K', NULL, NULL, NULL, 'document', 'business', "
                    f"'x', {chars}, '{quality}', 'sec/filings', NULL, NULL, NULL)")
    with pytest.raises(duckdb.ConstraintException):
        insert("ok", CAP)
    insert("capped", CAP)
    insert("ok", CAP - 1)


def test_the_backfill_serves_the_filing_list_its_8k_items_and_the_two_documents(sec):
    _msft(sec, ten_k=fx.ten_k(CAP + 10_000))
    check = fx.run(filings.NAME, mode="backfill", to="2026-10-28")
    assert check["result"] == "changed", check["note"]
    assert fx.q("SELECT item FROM filing_item WHERE accession = ? AND kind = 'item' ORDER BY item", fx.EIGHT_K) == [
        ("2.02",), ("9.01",)]
    assert fx.q("SELECT items, form, filing_date FROM sec_submissions WHERE accession = ?", fx.EIGHT_K) == [
        ("2.02,9.01", "8-K", date(2025, 10, 29))]
    assert fx.q("SELECT items FROM sec_submissions WHERE accession = ?", fx.Q26) == [(None,)]
    docs = fx.q("SELECT item, form, accession, chars, quality FROM filing_item WHERE kind = 'document' ORDER BY item")
    assert docs == [("business", "10-K", fx.K25, CAP, "capped"), ("proxy", "DEF 14A", fx.PROXY, 5_000, "ok")]
    assert fx.q("SELECT submissions_as_of FROM fundamental_company WHERE ticker = 'MSFT'") == [(date(2026, 10, 28),)]
    assert fx.q("SELECT submissions_as_of FROM fundamental_company WHERE ticker = 'HEI'") == [(None,)]  # never read
    calls = len(sec.requests)
    fx.run(filings.NAME, mode="backfill", to="2026-10-29")
    assert len(sec.calls("submissions/CIK0000789019")) == 1 and len(sec.calls("msft-10k.htm")) == 1   # resumable
    assert len(sec.requests) > calls                                     # the companies that answered 404 again


def test_a_company_that_filed_has_its_list_read_again_and_its_new_report_fetched(sec):
    _msft(sec)
    fx.run(filings.NAME, mode="backfill", to="2026-10-26")
    new_k = ("0000950170-26-100001", "10-K", "2026-10-27", "2026-06-30", "msft-10k-2026.htm", "")
    sec.routes.clear()
    fx.filing_routes(sec, {"789019": fx.submissions(789019, [new_k] + fx.MSFT_FILINGS)},
                     {"msft-10k-2026.htm": fx.ten_k(7_000)})
    fx.sec_routes(sec, {}, {"20261027": fx.daily_index([("789019", "MICROSOFT CORP", "10-K", "20261027", new_k[0])])})
    check = fx.run(filings.NAME, "2026-10-28")
    assert check["result"] == "changed", check["note"]
    assert fx.q("SELECT accession, chars, quality FROM filing_item WHERE kind = 'document' AND item = 'business'") == [
        (new_k[0], 7_000, "ok")]
    assert fx.q("SELECT submissions_as_of FROM fundamental_company WHERE ticker = 'MSFT'") == [(date(2026, 10, 28),)]


def test_a_filing_made_during_an_outage_longer_than_the_lookback_is_still_listed(sec):
    # The Opus review's S1, for the filing list: read on 1 Sep, an 8-K filed on 13 Oct, the next run on 28 Oct.
    _msft(sec)
    fx.run(filings.NAME, mode="backfill", to="2026-09-01")
    eight_k = ("0000950170-26-100002", "8-K", "2026-10-13", "2026-10-13", "msft-8k.htm", "2.02")
    sec.routes.clear()
    _msft(sec, rows=[eight_k] + fx.MSFT_FILINGS)
    fx.sec_routes(sec, {}, {"20261013": fx.daily_index([("789019", "MICROSOFT CORP", "8-K", "20261013", eight_k[0])])})
    check = fx.run(filings.NAME, "2026-10-28")
    assert check["result"] == "changed", check["note"]
    assert fx.q(f"SELECT item FROM filing_item WHERE accession = '{eight_k[0]}'") == [("2.02",)]
    assert fx.q("SELECT submissions_as_of FROM fundamental_company WHERE ticker = 'MSFT'") == [(date(2026, 10, 28),)]


def test_a_40f_filer_is_a_named_miss(sec):
    fx.filing_routes(sec, {"1094517": fx.submissions(1094517, [
        ("0001193125-25-150000", "40-F", "2025-06-25", "2025-03-31", "tm-40f.htm", "")])})
    fx.run(filings.NAME, mode="backfill", to="2026-10-28")
    assert fx.q("SELECT quality, text FROM filing_item WHERE kind = 'document' AND cik = '1094517'") == [
        ("form_40f", None)]
    assert not sec.calls("tm-40f.htm")
