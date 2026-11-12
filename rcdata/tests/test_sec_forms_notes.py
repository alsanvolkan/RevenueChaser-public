"""#753: the note-block reader returns the filer's own ``ix:nonNumeric`` text blocks, with their chains and nesting.

What is pinned here, and why each one is worth a test:

- **The kept set.** A name ending in ``TextBlock`` or ``Explanatory`` is a note; every other ``ix:nonNumeric`` is a
  cover fact and is left out *and counted*, so "this filing tags no notes" and "we kept none of them" can be told
  apart in a build report.
- **The chain is the text.** A note longer than one element continues in the element its ``continuedAt`` names. A
  reader that stops at the first element stores the first paragraph of a debt note and calls it the note.
- **Nesting.** Filers tag a note and then tag its subsections inside it (median 76 blocks, 23 top-level). Each row
  says whether it is top-level, so the same sentence is not counted three times in the stored total.
- **A broken chain does not raise and does not loop.** A missing id and a self-referencing id are both in the
  sample's shapes, and a 5,149-document pass may not die on one filer's typo.

Red on the merge-base: ``rcdata.build.sec_forms`` does not exist there, so every test below fails at import.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata.build.sec_forms import notes

from .sec_notes_fixture import (BROKEN_CHAIN, CONTINUED, ESCAPED, IFRS_AND_PREFIXES, INSIDE_A_COVER_FACT,
                                NESTED, NESTED_IN_CONTINUATION, NO_IXBRL, NO_PERIOD_END)

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
    assert [n.local_name for n in rows] == ["IncomeTaxDisclosureTextBlock"]
    assert counts["nil"] == 1 and counts["not_a_block"] == 3


@pytest.mark.parametrize("name", ["dei:TradingSymbol", "us-gaap:DebtDisclosureTextBlock",
                                  "ifrs-full:DisclosureOfBorrowingsExplanatory", "acme:OurOwnDisclosureTextBlock",
                                  "us-gaap:ScheduleOfDebtTableTextBlock"])
def test_is_block_reads_the_suffix_and_nothing_else(name):
    assert notes.is_block(name) == name.endswith(notes.BLOCK_SUFFIXES)


# ── the continuation chain ─────────────────────────────────────────────────────────────────────────────────────
def test_a_continued_note_is_stored_whole_and_in_order():
    """Two continuations, and a paragraph of the page between them that belongs to no block and must stay out."""
    rows, _ = notes.parse(CONTINUED)
    (note,) = rows
    assert note.text == ("Note 7. Debt\nWe borrowed money.\nThe term loan matures in 2029.\n"
                         "The revolver is undrawn.")
    assert "belongs to no block" not in note.text
    assert note.chars == len(note.text)


def test_a_chain_that_points_at_a_missing_id_keeps_what_it_has_and_counts_the_break():
    by_name, counts = _by_name(BROKEN_CHAIN)
    assert by_name["CommitmentsDisclosureTextBlock"].text == "We have commitments."
    assert counts["continuation_missing"] == 1


def test_a_chain_that_points_back_at_itself_stops_instead_of_looping():
    by_name, counts = _by_name(BROKEN_CHAIN)
    assert by_name["LeasesTextBlock"].text == "We lease.\nAnd lease again."
    assert counts["continuation_loop"] == 1


# ── nesting ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_note_tagged_inside_another_names_its_parent_and_is_not_top_level():
    rows, _ = notes.parse(NESTED)
    outer, inner = rows
    assert (outer.seq, outer.top_level, outer.parent_seq) == (0, True, None)
    assert (inner.seq, inner.top_level, inner.parent_seq) == (1, False, 0)
    assert inner.text in outer.text, "the parent's text reads as the page does, children included"


def test_a_note_tagged_inside_a_continuation_belongs_to_the_block_that_continuation_continues():
    """The same nesting one element along the chain. Without the owner map the inner note would look top-level,
    and its words would be counted twice in the stored total."""
    rows, _ = notes.parse(NESTED_IN_CONTINUATION)
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


def test_the_top_level_sum_counts_the_shared_text_once():
    """``top_level_chars`` is what the census's ``block chars`` column measures, and what acceptance compares."""
    rows, _ = notes.parse(NESTED)
    assert notes.top_level_chars(rows) == rows[0].chars < sum(n.chars for n in rows)


# ── the name, the kind, the period ─────────────────────────────────────────────────────────────────────────────
def test_the_concept_is_stored_as_filed_and_split_without_a_taxonomy_file():
    by_name, _ = _by_name(IFRS_AND_PREFIXES)
    own = by_name["OurOwnDisclosureTextBlock"]
    assert own.concept == "acme:OurOwnDisclosureTextBlock" and own.taxonomy == "acme"
    assert by_name["DisclosureOfBorrowingsExplanatory"].taxonomy == "ifrs-full"


@pytest.mark.parametrize("name,kind", [
    ("us-gaap:DebtDisclosureTextBlock", "disclosure"),
    ("ifrs-full:DisclosureOfBorrowingsExplanatory", "disclosure"),
    ("us-gaap:SignificantAccountingPoliciesTextBlock", "policy"),
    ("ifrs-full:DescriptionOfAccountingPolicyForBorrowingsExplanatory", "policy"),
    ("us-gaap:ScheduleOfDebtTableTextBlock", "table"),
])
def test_kind_of_covers_both_taxonomies_spellings(name, kind):
    assert notes.kind_of(name) == kind


def test_the_kinds_of_a_real_ifrs_filing_come_out_as_the_names_say():
    by_name, _ = _by_name(IFRS_AND_PREFIXES)
    assert {n: b.kind for n, b in by_name.items()} == {
        "DisclosureOfBorrowingsExplanatory": "disclosure",
        "DescriptionOfAccountingPolicyForBorrowingsExplanatory": "policy",
        "SignificantAccountingPoliciesTextBlock": "policy",
        "ScheduleOfDebtTableTextBlock": "table",
        "OurOwnDisclosureTextBlock": "disclosure",
    }


def test_the_period_end_is_the_context_s_and_a_context_without_one_gives_none():
    rows, _ = notes.parse(CONTINUED)
    assert rows[0].period_end == FY_END
    rows, _ = notes.parse(NO_PERIOD_END)
    assert rows[0].period_end is None, "a note is stored without a period rather than dropped"


# ── the text ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_escaped_markup_becomes_prose_and_an_entity_becomes_its_character():
    rows, _ = notes.parse(ESCAPED)
    (note,) = rows
    assert note.text == "Note 7. Debt\nWe borrowed money."
    assert "<p>" not in note.text and "&nbsp;" not in note.text


def test_the_row_carries_the_key_and_the_rules_version():
    rows, _ = notes.parse(CONTINUED)
    row = rows[0].row(cik="320193", accession="0000320193-26-000001")
    assert row["cik"] == 320193 and row["accession"] == "0000320193-26-000001"
    assert row["rules_version"] == notes.NOTES_VERSION == "notes-1"
    assert row["chars"] == len(row["text"]) and row["top_level"] is True and row["parent_seq"] is None


def test_the_reader_needs_no_network_and_no_model():
    """The suite's own guard would raise on a socket, so this says it in the file that would notice."""
    source = (notes.__file__ and open(notes.__file__, encoding="utf-8").read()) or ""
    for forbidden in ("httpx", "requests", "socket", "urllib", "duckdb", "openai", "deepseek"):
        assert forbidden not in source, f"{forbidden} has no business in the note reader"
