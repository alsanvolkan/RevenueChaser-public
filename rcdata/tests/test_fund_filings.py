"""``sec/filings`` (build plan PR 12): the filing list and its 8-K items, the newest annual report and proxy texts with
the splitter's quality flag (never ``ok`` at the 200,000-character cap), and the list's read date (F2)."""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build.fundamentals import filings, tables

from . import fund_fixture as fx
from .sec_answers import MISSING_HEADERS, MISSING_OBJECT

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
    """The cap belongs to a slice, which runs away when its stop header is missed -- not to a whole document,
    whose length is the filer's (avo, 5 Nov 2026: the document is kept uncut)."""
    html = fx.proxy(CAP + 50_000) if form == "DEF 14A" else fx.ten_k(CAP + 50_000)
    if form == "20-F":
        html = html.replace("Item 1. Business", "Item 4. Information on the Company").replace(
            "Item 1A. Risk Factors", "Item 4A. Unresolved Staff Comments")
    by_item = {item: (text, quality) for item, text, quality in filings.document_sections(html, form)}
    if form == "DEF 14A":
        text, quality = by_item["proxy"]
        assert len(text) > CAP and quality == "ok", "a proxy is kept whole"
        return
    whole, whole_quality = by_item["whole"]
    assert len(whole) > CAP and whole_quality == "ok", "the document is kept whole"
    text, quality = by_item["business"]
    assert len(text) == CAP and quality == "capped", "a slice still stops at the cap"
    small = {i: (x, q) for i, x, q in filings.document_sections(
        fx.ten_k(5_000), "10-K" if form == "20-F" else form)}
    assert small["business"][1] == "ok" and len(small["business"][0]) < CAP


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
    # ``whole`` is the document uncut (avo, 5 Nov 2026); ``market`` and ``financials`` get no row here because
    # this fixture's 10-K has neither Item 5 nor Item 8.
    assert docs == [("business", "10-K", fx.K25, CAP, "capped"),
                    ("proxy", "DEF 14A", fx.PROXY, 5_000, "ok"),
                    ("whole", "10-K", fx.K25, 210_098, "ok")]
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


def test_a_40f_filers_primary_document_is_read_like_any_other(sec):
    """It was skipped whole, on the premise that a 40-F keeps its business description in an exhibit. Measured on
    two real filings (5 Nov 2026): Canadian Natural writes everything into the primary document and Brookfield
    files an Annual Information Form exhibit beside it, so both shapes exist and the primary is always read."""
    fx.filing_routes(sec, {"1094517": fx.submissions(1094517, [
        ("0001193125-25-150000", "40-F", "2025-06-25", "2025-03-31", "tm-40f.htm", "")])})
    sec.add("tm-40f.htm", fx.ten_k(20_000))
    sec.add("index.json", {"directory": {"item": []}})
    fx.run(filings.NAME, mode="backfill", to="2026-10-28")
    rows = fx.q("SELECT item, quality FROM filing_item WHERE kind = 'document' AND cik = '1094517' ORDER BY item")
    assert ("whole", "ok") in rows and sec.calls("tm-40f.htm")


def test_a_document_the_archive_does_not_hold_is_a_named_miss_not_a_failed_run(sec):
    """A primary document that is not there answers the bucket's 403, where the code read only a 404: the run
    failed instead of naming the miss."""
    _msft(sec)
    sec.routes = [r for r in sec.routes if "msft-10k.htm" not in r[0]]
    sec.add("/msft-10k.htm", MISSING_OBJECT, status=403, headers=MISSING_HEADERS)
    check = fx.run(filings.NAME, "2026-10-28")
    assert check["result"] != "failed", check["note"]
    kinds = [i["kind"] for i in ops.open_issues(filings.NAME)[1]]
    assert "document_missing" in kinds


# ── The document kept whole, and the two sections avo asked for (5 Nov 2026) ──────────────────────────────────

def test_the_sections_are_the_longest_match_not_the_table_of_contents():
    """A 10-K opens with a contents page that lists every item header, so the first match of ``Item 5.`` is a line
    of the contents and only the longest match is the section. Same rule as the business slicer's."""
    body = ("Table of contents Item 5. Market for Registrant's Common Equity 31 "
            "Item 6. Selected Financial Data 40 Item 7. Management's Discussion 42 "
            "Item 8. Financial Statements 55 Item 9. Changes in Accountants 92 ")
    body += "Item 5. Market for Registrant's Common Equity " + ("dividends and repurchases. " * 400)
    body += "Item 6. Selected Financial Data "
    body += "Item 8. Financial Statements and Supplementary Data " + ("notes to the statements. " * 400)
    body += "Item 9. Changes in Accountants "
    by_item = {i: (x, q) for i, x, q in filings.document_sections(f"<html><body>{body}</body></html>", "10-K")}
    assert by_item["market"][1] == "ok" and "dividends and repurchases" in by_item["market"][0]
    assert "Item 6" not in by_item["market"][0], "the slice stops at the next item header"
    assert by_item["financials"][1] == "ok" and "notes to the statements" in by_item["financials"][0]
    assert len(by_item["whole"][0]) > len(by_item["market"][0]) + len(by_item["financials"][0])
    assert "notes to the statements" not in by_item["market"][0], "the two sections do not overlap"


def test_a_section_the_filer_does_not_have_gets_no_row():
    """No row rather than a row that says nothing: the ``whole`` row proves the document was read, so a missing
    section is the filer's, not a gap in the fetch."""
    items = {i for i, _, _ in filings.document_sections(fx.ten_k(5_000), "10-K")}
    assert "whole" in items and "business" in items
    assert "market" not in items and "financials" not in items


def test_nothing_this_layer_stores_is_cut_except_value_chains_own_field():
    """avo, 5 Nov 2026: "belgeyi hic kesmeyelim, hepsi kalsin". ``business`` is Value Chain's field and its own
    slicer caps it, which stays that board's rule; everything else is kept entire."""
    assert filings.SLICED_ITEMS == ("business",)
    body = "Item 8. Financial Statements and Supplementary Data " + ("notes. " * 40_000)   # 280,000 characters
    by_item = {i: (len(x), q) for i, x, q in filings.document_sections(f"<html><body>{body}</body></html>", "10-K")}
    assert by_item["financials"][0] > filings.max_text_chars(), "an Item 8 past the cap is kept whole"
    assert by_item["financials"][1] == "overrun", "and labelled, because no stop header followed"


def test_an_aif_exhibit_is_picked_by_name_largest_first():
    listing = {"directory": {"item": [
        {"name": "bn-20251231.htm", "size": "1064676"},
        {"name": "a2025-40xfex991aif.htm", "size": "1263940"},
        {"name": "ex991aif-small.htm", "size": "9000"},
        {"name": "bn-20251231_htm.xml", "size": "8009950"},
        {"name": "cover.jpg", "size": "608032"},
    ]}}
    assert filings.aif_exhibits(listing) == ["a2025-40xfex991aif.htm", "ex991aif-small.htm"]
    assert filings.aif_exhibits({"directory": {"item": []}}) == []
    assert filings.aif_exhibits({}) == []
