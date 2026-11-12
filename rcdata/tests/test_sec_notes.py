"""#753: the note-block reader returns the filer's own ``ix:nonNumeric`` text blocks, with their chains and nesting.

What is pinned here, and why each one is worth a test:

- **The kept set.** A name ending in ``TextBlock`` or ``Explanatory`` is a note; every other ``ix:nonNumeric`` is a
  cover fact and is left out *and counted*, so "this filing tags no notes" and "we kept none of them" can be told
  apart in a build report.
- **The chain is the text.** A note longer than one element continues in the element its ``continuedAt`` names. A
  reader that stops at the first element stores the first paragraph of a debt note and calls it the note.
- **Nesting.** Filers tag a note and then tag its subsections inside it (median 76 blocks, 23 top-level), so each
  row says whether it is top-level and the stored total counts the shared sentence once.
- **Every fault is counted, none is silent.** The counts are half the reader's answer: a 5,149-document pass that
  loses a paragraph to a broken chain must say so, because nobody will diff the text by hand.
- **The text is prose, and prose keeps its angle brackets.** The one test here that found a real defect is
  ``test_a_note_s_own_angle_brackets_are_not_markup``: the first version of this reader ran an HTML stripper over
  every block and deleted everything between a prose "<" and the next ">", which took two sentences out of a
  concentration note and counted nothing.

Red on the merge-base: ``rcdata.sec_notes`` does not exist there, so every test below fails at import.
"""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from RevenueChaser.rcdata import sec_notes as notes

from .sec_notes_fixture import (BROKEN_CHAIN, CONTINUATION_BEFORE_ITS_BLOCK, CONTINUATION_INSIDE_ITS_OWN_BLOCK,
                                CONTINUED, DUPLICATE_AND_ORPHAN_CONTINUATIONS, ESCAPED, EXCLUDED_PAGE_FURNITURE,
                                IFRS_AND_PREFIXES, INSIDE_A_COVER_FACT, NESTED, NESTED_IN_CONTINUATION,
                                NESTED_IN_SECOND_CONTINUATION, NO_IXBRL, NO_PERIOD_END, OTHER_IX_PREFIX,
                                PROSE_WITH_ANGLE_BRACKETS, SHARED_CONTINUATION, TABLE_CELLS, TWO_STEP_LOOP,
                                UGLY_MARKUP, UNCLOSED_BLOCK)

FY_END = date(2025, 12, 31)


def _by_name(html: str) -> tuple[dict[str, notes.Note], dict]:
    rows, counts = notes.parse(html)
    return {n.local_name: n for n in rows}, counts


# ── the kept set ───────────────────────────────────────────────────────────────────────────────────────────────
def test_the_cover_facts_are_left_out_and_counted():
    """Three ``dei:`` facts are tagged the same way as a note and are not notes. They have to be visible as a
    count: a filing where everything was skipped must not look like a filing that tagged nothing."""
    rows, counts = notes.parse(CONTINUED)
    assert [n.local_name for n in rows] == ["DebtDisclosureTextBlock"]
    assert counts["not_a_block"] == 3


def test_a_document_with_no_inline_xbrl_gives_no_rows_and_no_counts():
    """0.6% of the stored annual reports (the census). It is empty, not an error."""
    assert notes.parse(NO_IXBRL) == ([], {})


def test_a_nil_fact_is_left_out_and_counted_separately_from_a_cover_fact():
    rows, counts = notes.parse(NO_PERIOD_END)
    assert "SegmentReportingDisclosureTextBlock" not in {n.local_name for n in rows}
    assert counts["nil"] == 1 and counts["not_a_block"] == 3


@pytest.mark.parametrize("name,kept", [
    ("dei:TradingSymbol", False),
    ("dei:EntityFilerCategory", False),
    ("us-gaap:DebtDisclosureTextBlock", True),
    ("ifrs-full:DisclosureOfBorrowingsExplanatory", True),
    ("acme:OurOwnDisclosureTextBlock", True),
    ("us-gaap:ScheduleOfDebtTableTextBlock", True),
    ("us-gaap:TextBlockish", False),
    ("", False),
])
def test_is_block_keeps_the_two_suffixes_and_refuses_the_rest(name, kept):
    """Written out rather than compared with ``BLOCK_SUFFIXES``, which would pass with the suffixes emptied."""
    assert notes.is_block(name) is kept


def test_a_filing_whose_inline_prefix_is_not_ix_is_read_too():
    """The spec binds the namespace, not the prefix, and ``rcdata/build/fundamentals/ixbrl.py`` assumes ``ix:``."""
    rows, _ = notes.parse(OTHER_IX_PREFIX)
    assert [n.text for n in rows] == ["We borrowed money."]


# ── the continuation chain ─────────────────────────────────────────────────────────────────────────────────────
def test_a_continued_note_is_stored_whole_and_in_order():
    """Two continuations, and a paragraph of the page between them that belongs to no block and must stay out."""
    rows, _ = notes.parse(CONTINUED)
    (note,) = rows
    assert note.text == ("Note 7. Debt\nWe borrowed money.\nThe term loan matures in 2029.\n"
                         "The revolver is undrawn.")
    assert "belongs to no block" not in note.text


def test_a_continuation_that_comes_before_its_block_in_the_file_is_still_found():
    rows, counts = notes.parse(CONTINUATION_BEFORE_ITS_BLOCK)
    assert rows[0].text == "Note 7. Debt\nContinued text that comes first in the file."
    assert "continuation_missing" not in counts


def test_a_chain_that_points_at_a_missing_id_keeps_what_it_has_and_counts_the_break():
    by_name, counts = _by_name(BROKEN_CHAIN)
    assert by_name["CommitmentsDisclosureTextBlock"].text == "We have commitments."
    assert counts["continuation_missing"] == 1


def test_a_chain_that_points_back_at_itself_stops_instead_of_looping():
    by_name, counts = _by_name(BROKEN_CHAIN)
    assert by_name["LeasesTextBlock"].text == "We lease.\nAnd lease again."
    assert counts["continuation_loop"] == 1


def test_a_two_step_loop_stops_too():
    """The one-step loop is caught by any guard; this is the one a naive ``seen`` on the block alone misses."""
    rows, counts = notes.parse(TWO_STEP_LOOP)
    assert rows[0].text == "We lease.\nP.\nQ." and counts["continuation_loop"] == 1


def test_a_duplicate_continuation_id_and_an_orphan_are_both_counted():
    """Two elements with one id: one of them can never be read. An orphan's text reaches no block at all. Both
    lose a paragraph of somebody's note, so neither may be silent."""
    rows, counts = notes.parse(DUPLICATE_AND_ORPHAN_CONTINUATIONS)
    assert rows[0].text == "Note 7. Debt\nSecond with this id."
    assert counts["continuation_duplicate_id"] == 1 and counts["continuation_orphan"] == 1


def test_two_blocks_claiming_one_continuation_both_get_it_and_the_case_is_counted():
    """Nothing in the document says which block the text belongs to, so both keep it and the count says the
    top-level sum is long by that much. Guessing silently would be worse."""
    by_name, counts = _by_name(SHARED_CONTINUATION)
    assert by_name["DebtDisclosureTextBlock"].text == "Debt.\nShared text."
    assert by_name["LeasesTextBlock"].text == "Leases.\nShared text."
    assert counts["continuation_shared"] == 1


def test_a_chain_element_inside_its_own_block_is_not_appended_twice():
    """It was already collected through the block's own frame. Appending it again would store the paragraph twice
    and inflate ``chars``; the same holds for a chain element nested in the element before it."""
    rows, _ = notes.parse(CONTINUATION_INSIDE_ITS_OWN_BLOCK)
    (note,) = rows
    assert note.text == "Note 7. Debt\nInner one.\nInner two."
    assert note.text.count("Inner one.") == 1 and note.text.count("Inner two.") == 1


# ── nesting ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_note_tagged_inside_another_names_its_parent_and_is_not_top_level():
    rows, _ = notes.parse(NESTED)
    outer, inner = rows
    assert (outer.seq, outer.top_level, outer.parent_seq) == (0, True, None)
    assert (inner.seq, inner.top_level, inner.parent_seq) == (1, False, 0)
    assert inner.text in outer.text, "the parent's text reads as the page does, children included"


@pytest.mark.parametrize("html", [NESTED_IN_CONTINUATION, NESTED_IN_SECOND_CONTINUATION])
def test_a_note_tagged_inside_a_continuation_belongs_to_the_block_that_continuation_continues(html):
    """Once at the chain's first element, once at its second, which is reached only by following the chain twice.
    Without the owner map the inner note would look top-level and its words would be counted twice."""
    rows, _ = notes.parse(html)
    outer, inner = rows
    assert (outer.top_level, outer.parent_seq) == (True, None)
    assert (inner.top_level, inner.parent_seq) == (False, 0)
    assert inner.text in outer.text


def test_a_note_inside_a_cover_fact_is_top_level_because_a_cover_fact_is_not_a_row():
    """A fact we did not keep cannot be a parent: ``parent_seq`` has to name a ``seq`` that exists."""
    rows, counts = notes.parse(INSIDE_A_COVER_FACT)
    (note,) = rows
    assert (note.top_level, note.parent_seq) == (True, None)
    assert note.text == "We borrowed money.", "the auditor's name is the cover fact's text, not the note's"
    assert counts["not_a_block"] == 4


def test_every_parent_seq_names_a_row_that_exists_and_no_row_is_its_own_parent():
    for html in (NESTED, NESTED_IN_CONTINUATION, NESTED_IN_SECOND_CONTINUATION, UNCLOSED_BLOCK,
                 SHARED_CONTINUATION, CONTINUED, IFRS_AND_PREFIXES):
        rows, _ = notes.parse(html)
        seqs = {n.seq for n in rows}
        for n in rows:
            assert n.parent_seq != n.seq
            assert n.parent_seq is None or n.parent_seq in seqs
            assert n.top_level is (n.parent_seq is None)


def test_the_top_level_sum_counts_the_shared_text_once():
    """``top_level_chars`` is what the census's ``block chars`` column measures, and what acceptance compares."""
    rows, _ = notes.parse(NESTED)
    assert notes.top_level_chars(rows) == rows[0].chars < sum(n.chars for n in rows)


def test_a_document_that_ends_inside_a_block_is_counted_as_unclosed():
    """A truncated stored file does this, and then every later block is that block's child and the filing's
    top-level sum is wrong. We cannot repair it; the count is what makes it visible instead of plausible."""
    rows, counts = notes.parse(UNCLOSED_BLOCK)
    assert counts["unclosed"] == 1
    assert rows[1].parent_seq == 0 and "Page footer." in rows[0].text


# ── the name, the kind, the period ─────────────────────────────────────────────────────────────────────────────
def test_the_concept_is_stored_as_filed_and_split_without_a_taxonomy_file():
    by_name, _ = _by_name(IFRS_AND_PREFIXES)
    own = by_name["OurOwnDisclosureTextBlock"]
    assert own.concept == "acme:OurOwnDisclosureTextBlock" and own.taxonomy == "acme"
    assert by_name["DisclosureOfBorrowingsExplanatory"].taxonomy == "ifrs-full"
    assert notes.taxonomy("NoPrefixTextBlock") == ""


@pytest.mark.parametrize("name,kind", [
    ("us-gaap:DebtDisclosureTextBlock", "disclosure"),
    ("ifrs-full:DisclosureOfBorrowingsExplanatory", "disclosure"),
    ("us-gaap:SignificantAccountingPoliciesTextBlock", "policy"),
    ("us-gaap:BasisOfAccountingPolicyTextBlock", "policy"),
    ("ifrs-full:DescriptionOfAccountingPolicyForBorrowingsExplanatory", "policy"),
    # The IFRS spelling a rule written against ``DescriptionOfAccountingPolicy...`` alone calls a disclosure.
    ("ifrs-full:DisclosureOfMaterialAccountingPolicyInformationExplanatory", "policy"),
    ("us-gaap:ScheduleOfDebtTableTextBlock", "table"),
    ("us-gaap:ScheduleOfLeasesTablesTextBlock", "table"),
])
def test_kind_of_covers_both_taxonomies_spellings(name, kind):
    assert notes.kind_of(name) == kind


def test_the_kinds_of_a_real_ifrs_filing_come_out_as_the_names_say():
    by_name, _ = _by_name(IFRS_AND_PREFIXES)
    assert {n: b.kind for n, b in by_name.items()} == {
        "DisclosureOfBorrowingsExplanatory": "disclosure",
        "DescriptionOfAccountingPolicyForBorrowingsExplanatory": "policy",
        "DisclosureOfMaterialAccountingPolicyInformationExplanatory": "policy",
        "SignificantAccountingPoliciesTextBlock": "policy",
        "ScheduleOfDebtTableTextBlock": "table",
        "ScheduleOfLeasesTablesTextBlock": "table",
        "OurOwnDisclosureTextBlock": "disclosure",
    }


def test_the_period_end_is_the_context_s_and_the_two_ways_it_can_be_missing_are_counted():
    """A context with only a start date and a ``contextRef`` naming no context both give ``None``, but only one of
    them means the document is wrong, so they are counted apart."""
    rows, _ = notes.parse(CONTINUED)
    assert rows[0].period_end == FY_END
    rows, counts = notes.parse(NO_PERIOD_END)
    assert [n.period_end for n in rows] == [None, None], "a note is stored without a period rather than dropped"
    assert counts["period_end_missing"] == 2 and counts["context_missing"] == 1


# ── the text ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_note_s_own_angle_brackets_are_not_markup():
    """The defect the first review of PR 769 found: an HTML stripper over the parser's own output deleted
    everything from a prose "<" to the next ">", so this block read "Customers below 30%." and nothing was
    counted. The sentence in the middle is the test."""
    rows, counts = notes.parse(PROSE_WITH_ANGLE_BRACKETS)
    (note,) = rows
    assert note.text == ("Customers below < 5% of revenue are not named.\n"
                         "The sentence between the brackets must survive.\n"
                         "Gross margin was > 30% in every segment.")
    assert "escaped_markup" not in counts, "prose is not markup, and must not be counted as it"


@pytest.mark.parametrize("prose", ["If a < b and b > c then a < c.", "Revenue fell < 5% and costs rose > 2%.",
                                   "The ratio is <1 in both years.", "Customers < 5%"])
def test_text_of_leaves_prose_angle_brackets_alone(prose):
    counts: dict = {}
    assert notes.text_of([prose], counts) == prose and counts == {}


def test_escaped_markup_becomes_prose_and_a_doubly_escaped_entity_becomes_its_character():
    """``escape="true"`` with its markup escaped reaches us as the text ``<p>``, and its entities were escaped
    twice, so ``&amp;nbsp;`` has to come back as a space. Both only where a tag was really found."""
    rows, counts = notes.parse(ESCAPED)
    (note,) = rows
    assert note.text == "Note 7. Debt\nWe borrowed money."
    assert "<p>" not in note.text and "&nbsp;" not in note.text
    assert counts["escaped_markup"] == 1


def test_a_table_s_cells_do_not_run_together():
    """A filer's table has no whitespace between cells, so without ``td``/``th`` as line breaks a debt table
    reads ``Term loan500600``. One line per cell is what this layer stores; the boards parse it."""
    rows, _ = notes.parse(TABLE_CELLS)
    (note,) = rows
    assert note.text == "Instrument\n2025\n2024\nTerm loan\n500\n600\nRevolver\n0\n0"


def test_an_ix_exclude_is_the_page_s_furniture_and_is_dropped_and_counted():
    """The spec's own way of saying "this running header is not part of the fact". Acceptance compares
    characters, so keeping it would inflate every page of every note."""
    rows, counts = notes.parse(EXCLUDED_PAGE_FURNITURE)
    (note,) = rows
    assert note.text == "Note 7. Debt\nThe term loan matures in 2029."
    assert "Form 10-K" not in note.text and counts["excluded"] == 1


def test_a_filer_s_worst_markup_does_not_change_the_words():
    """An unclosed ``<p>``, an attribute in the other case, a word split across two ``<span>``s, and a ``<style>``
    block inside the note. All four are things filers do."""
    rows, _ = notes.parse(UGLY_MARKUP)
    (note,) = rows
    assert note.text == "Note 7. Debt\nThe term loan matures in 2029.\nThat is all."
    assert "font-weight" not in note.text, "a stylesheet inside a note is markup, not prose"


# ── the row ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_row_carries_the_whole_key_including_the_document():
    """``seq`` counts within one file, so an accession holding two inline documents would collide without it."""
    rows, _ = notes.parse(CONTINUED)
    row = rows[0].row(cik="320193", accession="0000320193-26-000001", document="aapl-20251231.htm")
    assert row["cik"] == 320193 and row["accession"] == "0000320193-26-000001"
    assert row["document"] == "aapl-20251231.htm"
    assert row["rules_version"] == notes.NOTES_VERSION == "notes-1"
    assert row["top_level"] is True and row["parent_seq"] is None and row["period_end"] == FY_END
    assert row["text"] == rows[0].text and row["chars"] == 87


# ── what the reader may not reach ──────────────────────────────────────────────────────────────────────────────
_ALLOWED_IMPORTS = {"re", "dataclasses", "datetime", "html", "__future__"}


def _imported_names(path: Path) -> set[str]:
    """The top-level name of every module ``path`` imports; a relative import keeps its dots, so it cannot pass."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            found.add("." * node.level + root if node.level else root)
    return found


def test_the_reader_imports_nothing_but_the_standard_library():
    """It lives beside ``sec_sections`` rather than under ``rcdata.build`` for exactly this: the serving side of
    #757 needs ``NOTES_VERSION`` and ``kind_of``, and a board may want ``kind``, and neither should pay for
    duckdb, httpx and the dataset registry. Checked on the source, because another test in the same process may
    have imported the builder for its own reasons.

    An AST scan cannot see a dynamic ``importlib.import_module`` or a module ``__getattr__``; it is here to catch
    the ordinary case, which is somebody adding a convenience import.
    """
    extra = _imported_names(Path(notes.__file__)) - _ALLOWED_IMPORTS
    assert not extra, f"{sorted(extra)} would reach every importer of the note reader"
