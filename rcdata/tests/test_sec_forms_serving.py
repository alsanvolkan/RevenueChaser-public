"""The ``sec_forms`` serving kind (#757): the two tables, the four needs, and the staleness rule that decides
which company is cut again.

The build is driven the way production drives it -- ``sec/filings`` fetches the documents, then ``sec/forms`` cuts
what is on disk -- because the one trap this layer can fall into is invisible from a unit call: two datasets
writing into one ``parquet/sec/forms/`` folder would leave both build manifests empty, every later build would
read "nothing changed", and the served tables would freeze after the first build with every test green. So the
incremental test below adds a filing and asserts it **reaches the served table**.
"""
from __future__ import annotations

import shutil
from datetime import date

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.fundamentals import common, filings
from RevenueChaser.rcdata.build.sec_forms import forms
from RevenueChaser.rcdata.needs import serve

from . import fund_fixture as fx
from . import sec_notes_fixture as nf

AS_OF = "2026-10-28"
LATER = date(2026, 10, 29)
MSFT = "789019"
NEW_K = "0000789019-26-000100"


@pytest.fixture
def sec(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    fx.spine()
    return http_mock


def q(sql: str, *params) -> list[tuple]:
    con = serving.open_read("sec_forms")
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()


def annual(body_chars: int = 5_000) -> str:
    """A 10-K the item cutter can cut **and** the note reader can read: the note fixture's inline-XBRL header and
    its one continued debt note, then the item headings and the business body."""
    return nf.CONTINUED.replace("\n</body></html>", fx.ten_k(body_chars).split("<body>", 1)[1])


def _stored(sec, rows=None, documents=None) -> None:
    """The documents on disk, fetched by ``sec/filings`` as in production."""
    fx.filing_routes(sec, {MSFT: fx.submissions(789019, rows or fx.MSFT_FILINGS, "MICROSOFT CORP")},
                     {"msft-10k.htm": annual(), "msft-def14a.htm": fx.proxy(), **(documents or {})})
    fx.run(filings.NAME, mode="backfill", to=AS_OF)


def _refiled(sec, rows, documents) -> None:
    """A later day's filing list and the documents it names, on disk.

    ``sec/filings`` keeps the list it already stored (``_subs_raws``), so running its backfill again re-reads the
    old one and a new filing never arrives. A later day's run is what fetches a new list, and this is that run's
    steps -- except that the **list is written rather than answered**: ``HttpMock`` serves the first route whose
    substring matches the URL, so a second route for the submissions URL can never win over the one the first run
    added. The documents are new URLs, so those are ordinary routes.
    """
    for name, html in documents.items():
        sec.add(f"/{name}", html)
    raw = filings._subs_dir(MSFT) / f"{LATER.isoformat()}.json.gz"
    base.write_raw_json(common.meta_path(raw), {"cik": MSFT, "as_of": LATER.isoformat(), "fetched_at": 2.0,
                                                "run_id": "t-refiled"})
    base.write_raw_json(raw, fx.submissions(789019, rows, "MICROSOFT CORP"))
    ctx = base.RunContext(run_id="t-refiled", dataset="test")
    filings.read_documents(ctx, MSFT, raw, LATER)
    filings.DATASET.to_parquet_all(ctx, [raw])


def _cut(**params) -> dict:
    check = fx.run(forms.NAME, AS_OF, **params)
    return check


# ── a fresh build ───────────────────────────────────────────────────────────────────────────────────────────────

def test_a_fresh_build_serves_the_sections_with_the_method_that_found_each(sec):
    _stored(sec)
    check = _cut()
    assert check["result"] != "failed", check.get("note")
    assert dict(q("SELECT section, method FROM filing_section WHERE cik = ? ORDER BY section", MSFT)) == {
        "business": forms.HEADER, "proxy": forms.UNCUT, "whole": forms.UNCUT}
    # Every row says which rule wrote it, so a half-finished migration to ``ours-2`` is visible, not inferred.
    assert q("SELECT DISTINCT rules_version FROM filing_section") == [(forms.SECTIONS_VERSION,)]
    assert f"sections {forms.SECTIONS_VERSION} {check['rows']}" in check["note"], check["note"]


def test_the_business_row_is_the_item_and_its_span_in_the_whole_document(sec):
    _stored(sec)
    _cut()
    whole, = q("SELECT text FROM filing_section WHERE cik = ? AND section = 'whole'", MSFT)
    item, chars, start, end = q("SELECT text, chars, start_char, end_char FROM filing_section "
                                "WHERE cik = ? AND section = 'business'", MSFT)[0]
    assert "We develop software and cloud services." in item and chars == len(item)
    assert whole[0][start:end] == item, "the span points at the section inside the whole document"


def test_an_empty_cut_is_still_a_business_row_carrying_the_cutters_label(sec):
    """#843: a filing the item rule cannot cut gets the row anyway, with empty text and ``crossref``.

    HSBC, Barclays, America Movil and Vodafone file the annual report itself and map the form's items to it in a
    table, so there is no span to cut: 22 of the 5,131 stored annual reports (13 Nov 2026). Appending the row only
    ``if item`` dropped the label with the text, and a reader cannot tell "the section is in another document, do
    not look" from "no row" -- not cut yet, or a cut that failed. ``module_relatedness.locator.asks`` (#841) reads
    this label and pays for a locator call on the ``missing`` path that no row can answer.
    """
    # The index maps each item to a page and carries no item caption the start patterns could read as a heading,
    # which is why this filing's cut is empty and not a few characters of the table (McDonald's own index does
    # give a caption: ``test_sec_sections_ours_2`` holds that case).
    index = "Form 10-K Cross-Reference Index\nPage reference\nPart I\n1 Business 3\n1A Risk Factors 27\n"
    report = "<html><body>About the group.\n" + "This annual report describes the group. " * 200 + "\n" + index
    _stored(sec, documents={"msft-10k.htm": report + "</body></html>"})
    assert _cut()["result"] != "failed"
    rows = q("SELECT text, chars, quality, method, start_char, end_char FROM filing_section "
             "WHERE cik = ? AND section = 'business'", MSFT)
    assert rows == [("", 0, "crossref", forms.HEADER, None, None)], rows
    # And the other rows of the same filing are untouched: the whole document is still served whole.
    assert q("SELECT chars > 0 FROM filing_section WHERE cik = ? AND section = 'whole'", MSFT) == [(True,)]


def test_the_note_blocks_of_the_annual_report_are_served_with_their_chain_joined(sec):
    _stored(sec)
    _cut()
    rows = q("SELECT local_name, taxonomy, kind, seq, top_level, text FROM filing_note WHERE cik = ? "
             "ORDER BY seq", MSFT)
    assert [(r[0], r[1], r[2], r[4]) for r in rows] == [("DebtDisclosureTextBlock", "us-gaap", "disclosure", True)]
    # The chain is the text: the block and the two continuations it names, in order.
    assert "We borrowed money." in rows[0][5] and "The revolver is undrawn." in rows[0][5]
    assert "belongs to no block" not in rows[0][5]
    assert q("SELECT DISTINCT rules_version FROM filing_note") == [(forms.NOTES_VERSION,)]


def test_a_proxy_is_never_read_for_notes(sec):
    """The proxy has no inline XBRL and is not an annual report: a note row keyed to it would be a cut of the
    wrong document."""
    _stored(sec)
    _cut()
    assert q("SELECT count(*) FROM filing_note WHERE form = 'DEF 14A'") == [(0,)]


# ── the staleness rule ──────────────────────────────────────────────────────────────────────────────────────────

def test_a_second_run_cuts_nothing(sec):
    _stored(sec)
    _cut()
    again = _cut()
    assert "0 of" in (again.get("note") or ""), again.get("note")


def test_a_new_filing_reaches_the_served_table(sec):
    """The one-dataset trap: with two datasets in one Parquet folder both manifests stay empty and this row never
    arrives, with every other test still green."""
    _stored(sec)
    _cut()
    assert q("SELECT count(*) FROM filing_section WHERE accession = ?", NEW_K) == [(0,)]

    _refiled(sec, [(NEW_K, "10-K", "2026-07-30", "2026-06-30", "msft-10k-26.htm", ""), *fx.MSFT_FILINGS],
             {"msft-10k-26.htm": annual(6_000)})
    assert filings._doc_path(MSFT, NEW_K).exists(), "sec/filings stored the new document before anything is cut"
    check = _cut()
    assert "1 of" in (check.get("note") or ""), check.get("note")
    assert check["result"] != "failed", check.get("note")
    assert q("SELECT section FROM filing_section WHERE accession = ? ORDER BY section", NEW_K) == [
        ("business",), ("whole",)]
    assert q("SELECT count(*) FROM filing_note WHERE accession = ?", NEW_K) == [(1,)]


def _age_the_stored_rows(version: str = "ours-0") -> None:
    """Rewrite the stored sections partition as if an older rule had cut it.

    Not ``monkeypatch`` on :data:`forms.SECTIONS_VERSION`: that constant is handed to ``sec_sections``, which
    refuses a version it cannot cut with, so a made-up one fails the run instead of ageing the rows.
    """
    part = common.parquet_dir(forms.NAME) / f"cik={MSFT}" / forms.SECTIONS_PARQUET
    con = duckdb.connect()
    try:
        cols = ", ".join(f"'{version}' AS rules_version" if c == "rules_version" else f'"{c}"'
                         for c, _ in forms.SECTION)
        # Into a table first: the write goes through a tmp file and a rename over the one being read.
        con.execute(f"CREATE TABLE aged AS SELECT {cols} FROM read_parquet({base.sql_path(part)})")
        base.write_parquet(con, "SELECT * FROM aged", part)
    finally:
        con.close()


def test_a_company_whose_rows_hold_another_cutter_version_is_cut_again(sec):
    """What nothing else triggers: the documents on disk did not change, our own rule did."""
    _stored(sec)
    _cut()
    _age_the_stored_rows()
    assert MSFT in forms.held()[1], "the company holds a row of another version"
    check = _cut()
    assert "1 of" in (check.get("note") or ""), check.get("note")
    assert q("SELECT DISTINCT rules_version FROM filing_section") == [(forms.SECTIONS_VERSION,)]


# ── the swap's own checks ───────────────────────────────────────────────────────────────────────────────────────

def test_a_gone_partition_is_cut_again_rather_than_shrinking_the_file(sec):
    """The company's Parquet is gone but its documents are not, so the staleness rule cuts it again and the
    served table is whole. This is the one place a serving kind can quietly lose rows -- its own ``fetch``
    rebuilding what was removed is what stops the shrink guard from ever being the thing that notices."""
    _stored(sec)
    _cut()
    before = q("SELECT count(*) FROM filing_section WHERE cik = ?", MSFT)[0][0]
    shutil.rmtree(common.parquet_dir(forms.NAME) / f"cik={MSFT}")
    check = _cut()
    assert check["result"] != "failed", check.get("note")
    assert q("SELECT count(*) FROM filing_section WHERE cik = ?", MSFT)[0][0] == before


def test_an_exhibits_business_row_is_served_with_the_aif_method(sec):
    """The row the fundamentals layer drops: ``filings.py`` keeps an exhibit's text under the item name ``aif``
    and skips every other section of it, so no board can ask for "the business section" and reach a 40-F filer's
    Annual Information Form. Here it is a ``business`` row whose ``method`` says how it was found.
    """
    fx.filing_routes(sec, {"1094517": fx.submissions(1094517, [
        ("0001193125-25-150000", "40-F", "2025-06-25", "2025-03-31", "tm-40f.htm", "")])})
    sec.add("tm-40f.htm", fx.ten_k(20_000))
    sec.add("index.json", {"directory": {"item": [{"name": "tm-40f.htm", "size": "900000"},
                                                  {"name": "ex99-1aif.htm", "size": "500000"}]}})
    sec.add("ex99-1aif.htm", fx.ten_k(9_000))
    fx.run(filings.NAME, mode="backfill", to=AS_OF)
    _cut()
    rows = q("SELECT section, method, document FROM filing_section WHERE cik = '1094517' ORDER BY section, document")
    assert ("business", forms.AIF, "0001193125-25-150000.ex99-1aif.htm.gz") in rows, rows
    assert ("whole", forms.UNCUT, "0001193125-25-150000.html.gz") in rows


# ── the needs ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_business_need_answers_and_each_section_need_counts_only_its_own_rows(sec):
    _stored(sec)
    _cut()
    answer = serve.ask("sec.business", ciks=[MSFT])
    assert len(answer.rows) == 1 and answer.rows[0]["section"] == "business"

    # Without the need's ``where`` each of the three would count the whole table and read its state off another
    # section's rows.
    assert serve.summary("sec.proxy")["row_count"] == 1
    assert serve.summary("sec.whole")["row_count"] == 1
    assert serve.summary("sec.business")["row_count"] == 1
    assert q("SELECT count(*) FROM filing_section")[0][0] == 3


def test_the_note_need_answers_by_local_name_across_taxonomies(sec):
    _stored(sec)
    _cut()
    assert serve.ask("sec.note", ciks=[MSFT], local_name="DebtDisclosureTextBlock").rows
    assert not serve.ask("sec.note", ciks=[MSFT], local_name="SegmentReportingDisclosureTextBlock").rows


# ── the suspect flag on the business row (#835) ─────────────────────────────────────────────────────────────────

def _k_ending_in_a_cross_reference() -> str:
    """A 10-K whose Item 1 ends on a bare "See" in front of the Item 1A heading -- the shape the 10-K reading
    report found labelled ``ok`` while the cut was wrong (``_TAIL_REF``/S4, the shared
    ``suspect_tail_cross_reference`` case). Built on the note fixture's header, as :func:`annual` is, so the build
    still reads its one note."""
    paragraph = "<p>We develop software and cloud services. Our customers include enterprises.</p>"
    k = ("<html><body><p>Item 1. Business</p><p>Item 1A. Risk Factors</p>"
         "<p>PART I</p><p>Item 1. Business</p>" + paragraph * 80
         + "<p>See</p><p>Item 1A. Risk Factors</p><p>Risks.</p></body></html>")
    return nf.CONTINUED.replace("\n</body></html>", k.split("<body>", 1)[1])


def test_the_business_row_carries_the_suspect_flag_when_the_cut_ends_in_a_reference(sec):
    """#835: ``sec_forms`` wrote the 10-K/20-F business row with the cutter's bare label and no ``;suspect`` flag,
    so a wrong-but-``ok`` cut reached Relatedness and Value Chain with nothing telling them to look again. The row
    now carries :data:`forms.SECTIONS_VERSION`'s own flags; a cut that ends on a cross-reference is ``ok;suspect:S4``.
    """
    _stored(sec, documents={"msft-10k.htm": _k_ending_in_a_cross_reference()})
    _cut()
    quality, = q("SELECT quality FROM filing_section WHERE cik = ? AND section = 'business'", MSFT)[0]
    assert quality.startswith("ok;suspect:"), quality
    assert "S4" in quality, quality
    # The flag rides in the label text; the cut itself is unchanged, so a reader that only wants the cut strips it.
    assert forms.sec_sections.base(quality) == "ok"

    # The guard that a clean cut stays a bare ``ok`` lives here, not in a test of its own: on the merge-base a
    # clean cut is bare before and after, so a standalone guard would pass there and ``red-on-base`` would refuse
    # it (as PR 827 found). A short, well-formed 10-K has no signal to raise (S2 needs a huge filing).
    clean = [r for r in forms.sections_of(fx.ten_k(), "10-K") if r[0] == forms.BUSINESS][0]
    assert clean[2] == "ok", clean[2]


# ── the AIF business row carries the AIF cutter's flags, not the item cutter's (#838) ───────────────────────────

def test_the_aif_business_row_of_a_long_40_f_is_not_flagged_suspect():
    """The layer cuts a 40-F's business row with :func:`sec_sections.cut_aif_business` and flagged it from the
    ITEM signal set, so S3 -- does the slice carry a 10-K's own section headings -- was asked of a Canadian
    Annual Information Form, which NI 51-102 never asks for one. Together with S2 (an AIF is a small share of a
    long 40-F) that tripped the flag, and ``module_relatedness.locator.asks`` fires on any ``;suspect``, so a
    correct business row went to the writer model: a paid call per 40-F where the board runs a paid provider.

    The call site is what says which cutter made the text (``_flag(..., aif=True)``), so this reads the layer
    rather than :func:`sec_sections.cut_flag`, whose own cases are in ``test_sec_sections_ours_2``.
    """
    from .test_sec_sections_ours_2 import _LONG_40F

    rows = {r[0]: r for r in forms.sections_of(_LONG_40F, "40-F")}
    business = rows[forms.BUSINESS]
    assert business[3] == forms.AIF, business[3]
    assert business[2] == "ok", business[2]


# ── the cutter version this layer stores with (#845) ─────────────────────────────────────────────────────────────

def test_the_layer_stores_its_sections_at_ours_2(sec):
    """#845: the pin moved before the first full cut, not after it.

    ``forms._stale`` makes a company stale when its rows hold another ``rules_version``, so a full pass cut at
    ``ours-1`` would be re-cut whole the moment the pin moved -- 5,131 annual reports, paid twice. The behaviour
    that proves the version rather than reading the constant back is the cap: ``ours-1`` stops the business
    section at :data:`sec_sections.MAX_ITEM_CHARS` and labels it ``capped``, ``ours-2`` stores it whole.
    """
    assert forms.SECTIONS_VERSION == forms.sec_sections.OURS_2
    cap = forms.sec_sections.MAX_ITEM_CHARS
    _stored(sec, documents={"msft-10k.htm": annual(cap + 40_000)})
    check = _cut()
    assert check["result"] != "failed", check.get("note")
    chars, quality = q("SELECT chars, quality FROM filing_section WHERE cik = ? AND section = 'business'", MSFT)[0]
    assert chars > cap, f"the section is stored whole, not cut at the cap: {chars}"
    assert forms.sec_sections.base(quality) == "ok" and "capped" not in quality, quality
    assert q("SELECT DISTINCT rules_version FROM filing_section") == [("ours-2",)]
