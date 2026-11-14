"""``sec/filings`` (build plan PR 12): the filing list and its 8-K items, the newest annual report and proxy texts with
the splitter's quality flag, and the list's read date (F2).

"Never ``ok`` at the 200,000-character cap" was this layer's invariant while Value Chain pinned ``ours-1``, which
truncates there. Since #847 it pins ``ours-2``, which caps nowhere: the section arrives whole and ``ok``, and both
the label and the ``filing_item`` CHECK follow ``filings.caps_text()``. The capped half is kept here, asked of a
cut that caps, because the stored rows were written by one and the layer must keep serving them.
"""
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
def test_a_whole_document_is_never_cut_or_capped_however_long_it_is(form):
    """The cap belongs to a slice, which runs away when its stop header is missed -- not to a whole document,
    whose length is the filer's (avo, 5 Nov 2026: the document is kept uncut).

    What the ``business`` slice does at the cap is the pinned version's answer, not this layer's, so it is asked
    of :func:`filings.caps_text` rather than written in: see
    ``test_the_business_slice_follows_the_pinned_version_at_the_cap``. A proxy has no slice at all, so the
    pin cannot reach it -- which is a claim about this change and is asserted here for every form, because
    nothing else in this file would notice the layer reading a cap the pinned cut does not have.
    """
    assert filings.caps_text() is False, "the board pins ours-2 since #847, which caps nowhere"
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
    if filings.caps_text():
        assert len(text) == CAP and quality == "capped", "a cutter that caps still stops its slice at the cap"
    else:
        assert len(text) > CAP and quality == "ok", "a cutter that caps nowhere hands the section over whole"
    small = {i: (x, q) for i, x, q in filings.document_sections(
        fx.ten_k(5_000), "10-K" if form == "20-F" else form)}
    assert small["business"][1] == "ok" and len(small["business"][0]) < CAP


def test_the_business_slice_follows_the_pinned_version_at_the_cap():
    """``capped`` says "this is only part of the section", so only a cutter that truncates may say it (#847).

    The pin is ``ours-2`` today and caps nowhere, so a long section is stored whole and ``ok``. The label is not
    cosmetic: ``module_relatedness.locator.asks()`` sends a ``capped`` filing to a model to be re-cut, so a wrong
    ``capped`` is paid work for nothing. Asked of both versions here, because the layer serves rows written by
    ``ours-1`` as well.
    """
    from RevenueChaser.module_value_chain import splitter
    from RevenueChaser.rcdata import sec_sections

    assert filings.caps_text() is sec_sections.rule(splitter.VERSION).caps
    assert filings.caps_text() is False, "the board pins ours-2 since #847"

    html = fx.ten_k(CAP + 50_000)
    _, text, quality = next(r for r in filings.document_sections(html, "10-K") if r[0] == "business")
    assert quality == "ok" and len(text) > CAP
    assert "suspect" not in quality, "S8 is the cap signal and ours-2 does not define it"

    # And the same document under a cutter that does cap: the rows already in the store.
    item, q1 = sec_sections.cut_item(splitter.strip_html(html), "10-K", version=sec_sections.OURS_1)
    assert q1 == "capped" and item is not None and len(item) == CAP


def test_the_served_table_refuses_ok_at_the_cap_only_while_the_cut_caps():
    """The CHECK is the stored form of ``capped``, so it is there exactly while the pinned cut can produce one.

    Under ``ours-2`` (#847) a correct section longer than the cap is ``ok``, and a CHECK refusing it would stop
    the build on a right answer. ``filings.rules_state`` names ``caps_text``, so a live copy of the table is
    rebuilt rather than kept with the CHECK the old pin asked for.
    """
    con = duckdb.connect()
    tables.create(con, "filing_item")

    def insert(quality, chars):
        con.execute("INSERT INTO filing_item VALUES ('1', 'a', '10-K', NULL, NULL, NULL, 'document', 'business', "
                    f"'x', {chars}, '{quality}', 'sec/filings', NULL, NULL, NULL)")
    if filings.caps_text():
        with pytest.raises(duckdb.ConstraintException):
            insert("ok", CAP)
    else:
        insert("ok", CAP)
    insert("capped", CAP)
    insert("ok", CAP - 1)
    assert f"caps_text={filings.caps_text()}" in tables.FundamentalsTables().rules_state()


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
    # ``whole`` is the document uncut (avo, 5 Nov 2026); ``business`` is cut by the version Value Chain pins, so
    # its length and label are that version's (#847: ``ours-2`` caps nowhere, so the section is whole and ``ok``).
    assert [d[:3] for d in docs] == [("business", "10-K", fx.K25),
                                     ("proxy", "DEF 14A", fx.PROXY),
                                     ("whole", "10-K", fx.K25)]
    assert docs[1][3:] == (5_000, "ok") and docs[2][3:] == (210_098, "ok")
    if filings.caps_text():
        assert docs[0][3:] == (CAP, "capped")
    else:
        assert docs[0][4] == "ok" and CAP < docs[0][3] < 210_098
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


def test_an_aif_exhibit_with_no_aif_in_its_name_is_still_typed(sec):
    """The blocker of the first review: ``stored_exhibits`` found the stored file by name, so the fallback branch's
    pick (``ex99-1.htm``, no "aif" in it) was fetched, written to disk and never typed -- a row the owner asked
    for, silently absent. It reads the stored index now."""
    fx.filing_routes(sec, {"1094517": fx.submissions(1094517, [
        ("0001193125-25-150000", "40-F", "2025-06-25", "2025-03-31", "tm-40f.htm", "")])})
    sec.add("tm-40f.htm", fx.ten_k(20_000))
    sec.add("index.json", {"directory": {"item": [
        {"name": "tm-40f.htm", "size": "900000"},
        {"name": "ex99-1.htm", "size": "500000"},
        {"name": "ex99-2.htm", "size": "4000000"},
    ]}})
    sec.add("ex99-1.htm", fx.ten_k(9_000))
    fx.run(filings.NAME, mode="backfill", to="2026-10-28")
    rows = fx.q("SELECT item, quality FROM filing_item WHERE kind = 'document' AND cik = '1094517' ORDER BY item")
    assert ("aif", "by_number") in rows, rows      # ex99-1.htm: its number chose it, not its name
    assert ("whole", "ok") in rows
    assert not sec.calls("ex99-2.htm"), "the statements exhibit is not the business description"


def test_a_40f_gets_no_business_row(sec):
    """Value Chain's splitter knows a 10-K's Item 1 and a 20-F's Item 4, not a Canadian 40-F, so a slice of one
    would be an arbitrary cut labelled by rules that do not apply."""
    items = {i for i, _, _ in filings.document_sections(fx.ten_k(5_000), "40-F")}
    assert items == {"whole"}


def test_the_primary_document_is_never_fetched_twice_as_its_own_exhibit():
    """``index.json`` lists the 40-F itself; a primary named like an AIF would be fetched and typed twice."""
    listing = {"directory": {"item": [
        {"name": "cnq-40f-aif.htm", "size": "6572360"},
        {"name": "ex99-1aif.htm", "size": "900000"},
    ]}}
    assert filings.aif_exhibits(listing, "cnq-40f-aif.htm") == ["ex99-1aif.htm"]
    assert filings.aif_exhibits(listing) == ["cnq-40f-aif.htm", "ex99-1aif.htm"], "without it, the primary wins"


def test_an_exhibit_named_exhibit991_is_found_too():
    """EDGAR writes the number as ``exhibit991`` as often as ``ex99-1``."""
    for name in ("exhibit991.htm", "exhibit99-1.htm", "ex99_1.htm", "d12345dex991.htm"):
        listing = {"directory": {"item": [{"name": name, "size": "500000"}]}}
        assert filings.aif_exhibits(listing) == [name], name
    assert filings.aif_exhibits({"directory": {"item": [{"name": "ex99-13.htm", "size": "9"}]}}) == []


def test_ex99_pages_with_no_aif_among_them_are_a_named_miss():
    """"The AIF is in the primary document" and "the AIF was missed" are not the same answer, and this is the one
    case the code can tell apart."""
    none_named = {"directory": {"item": [{"name": "ex99-2.htm", "size": "9"},
                                         {"name": "ex99-3.htm", "size": "9"}]}}
    assert filings.missed_exhibits(none_named) == 2
    assert filings.missed_exhibits({"directory": {"item": [{"name": "ex99-1aif.htm", "size": "9"}]}}) == 0
    assert filings.missed_exhibits({"directory": {"item": []}}) == 0


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


# ── The document kept whole, and a 40-F read at all (avo, 5 Nov 2026) ─────────────────────────────────────────

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


def test_an_exhibit_chosen_by_its_number_alone_says_so_on_the_row(sec):
    """The number is one filer's layout, not a rule of the form, so a by-number pick is a guess -- a filer who
    keeps the AIF in the primary and files its statements as 99.1 would otherwise get an ``aif`` row that reads
    like Brookfield's real one."""
    fx.filing_routes(sec, {"1094517": fx.submissions(1094517, [
        ("0001193125-25-150000", "40-F", "2025-06-25", "2025-03-31", "tm-40f.htm", "")])})
    sec.add("tm-40f.htm", fx.ten_k(20_000))
    sec.add("index.json", {"directory": {"item": [{"name": "ex99-1.htm", "size": "500000"}]}})
    sec.add("ex99-1.htm", fx.ten_k(9_000))
    fx.run(filings.NAME, mode="backfill", to="2026-10-28")
    assert ("aif", "by_number") in fx.q(
        "SELECT item, quality FROM filing_item WHERE kind = 'document' AND cik = '1094517'")
    assert filings.picked_by_name({}, "ex99-1aif.htm") is True
    assert filings.picked_by_name({}, "ex99-1.htm") is False


def test_the_statements_exhibit_is_never_mistaken_for_the_business_description():
    """A 40-F files 99.1 (the Annual Information Form), 99.2 (the statements) and 99.3 (the MD&A), and the
    statements are usually the largest -- so "the biggest ex99" picked the wrong one."""
    listing = {"directory": {"item": [
        {"name": "ex99-1aif.htm", "size": "900000"},
        {"name": "ex99-2financialstatements.htm", "size": "4000000"},
        {"name": "ex99-3mda.htm", "size": "2000000"},
    ]}}
    assert filings.aif_exhibits(listing) == ["ex99-1aif.htm"], "the name that says aif wins outright"
    # No name says aif: only 99.1 is taken, by its number, and 99.2 and 99.3 are left alone.
    no_aif = {"directory": {"item": [
        {"name": "ex99-1.htm", "size": "900000"},
        {"name": "ex99-2.htm", "size": "4000000"},
        {"name": "ex99-13.htm", "size": "5000000"},
    ]}}
    assert filings.aif_exhibits(no_aif) == ["ex99-1.htm"]


def test_a_transition_period_10kt_is_an_annual_report():
    """#400 package (avo's condition, 8 Nov 2026: every SEC reading covered): a 10-KT, the report of a fiscal-year
    change, is the newest annual report when nothing newer exists; Value Chain's picker agrees."""
    from RevenueChaser.module_value_chain import edgar_text
    rows = [{"form": "10-K", "filing_date": "2025-03-01", "accession": "a", "primary_document": "a.htm"},
            {"form": "10-KT", "filing_date": "2026-02-01", "accession": "b", "primary_document": "b.htm"}]
    assert filings.pick(rows, filings.ANNUAL_FORMS)["accession"] == "b"
    payload = {"filings": {"recent": {"form": ["10-K", "10-KT"], "filingDate": ["2025-03-01", "2026-02-01"],
                                      "accessionNumber": ["a", "b"], "primaryDocument": ["a.htm", "b.htm"]}}}
    assert edgar_text.pick_filing(payload)["accession"] == "b"
