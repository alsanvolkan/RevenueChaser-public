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
  concentration note and counted nothing. The second round found the *fix* still eating ``EBITDA<x and
  leverage>y`` and ``a floor of <LIBOR + 2%>``, so the cases that look most like markup are parametrized out.
- **The counts asserted whole, once.** Every other test here reads one key, and none of them would notice a
  reader that reported a fault on a document that is fine, so one test compares the whole dict on a healthy
  document.

Red on the merge-base: ``rcdata.sec_notes`` does not exist there, so every test below fails at import.
"""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from RevenueChaser.rcdata import sec_notes as notes

from .sec_notes_fixture import (BLOCK_CLAIMING_THE_CONTINUATION_IT_SITS_IN, BROKEN_CHAIN,
                                CHAIN_ELEMENT_INSIDE_ANOTHER_BLOCK, CHAIN_INSIDE_ANOTHER_BLOCKS_CONTINUATION,
                                ESCAPED_WITH_ATTRIBUTES, HEALTHY_NESTED_CHILD_CHAIN, KEPT_GRANDPARENT_EMPTY_PARENT,
                                MUTUAL_PARENT_BLOCKS, THREE_BLOCKS_ONE_CONTINUATION, UNCLOSED_COVER_FACT,
                                UNCLOSED_STYLE,
                                CHAIN_INSIDE_EARLIER_CHAIN_ELEMENT, CHILD_CHAIN_LEAVES_ITS_PARENT,
                                CONTINUATION_BEFORE_ITS_BLOCK, CONTINUATION_INSIDE_ITS_OWN_BLOCK,
                                CONTINUATION_WITH_NO_ID, CONTINUED, DUPLICATE_AND_ORPHAN_CONTINUATIONS,
                                EMPTY_PARENT_BLOCK, ESCAPED, EXCLUDED_PAGE_FURNITURE, IFRS_AND_PREFIXES,
                                INSIDE_A_COVER_FACT, NESTED, NESTED_IN_CONTINUATION,
                                NESTED_IN_SECOND_CONTINUATION, NO_IXBRL, NO_PERIOD_END, OTHER_IX_PREFIX,
                                PROSE_THAT_LOOKS_LIKE_MARKUP, PROSE_WITH_ANGLE_BRACKETS, SHARED_CONTINUATION,
                                TABLE_CELLS, TWO_STEP_LOOP, UGLY_MARKUP, UNCLOSED_BLOCK, UNCLOSED_EXCLUDE)

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


def test_a_healthy_document_counts_nothing_but_its_cover_facts():
    """The counts asserted whole, not by key. Every other test here says "this key is 3"; none of them would
    notice a reader that also reported ``continuation_missing`` or ``empty`` on a document that is fine, and a
    build report where every filing carries a fault is a build report nobody reads."""
    rows, counts = notes.parse(CONTINUED)
    assert counts == {"not_a_block": 3} and len(rows) == 1


def test_an_ordinary_nested_note_with_a_chain_counts_nothing_either():
    """The shape a real filing is full of: a child note with a continuation of its own, inside its parent, with
    a paragraph of the parent between the two. The second round's fix reported ``continuation_overlap`` and
    ``child_text_outside_parent`` on it, which would have put two faults on nearly every row of every filing --
    both asked about strings and about any enclosing block, where the question is about the frames that fed this
    row and the frames its own parent read. The whole dict is the assertion, because a count that fires here is
    not a smaller version of a count that fires correctly; it makes the report useless."""
    rows, counts = notes.parse(HEALTHY_NESTED_CHILD_CHAIN)
    parent, child = rows
    assert counts == {"not_a_block": 3}
    assert child.parent_seq == parent.seq and not child.top_level
    assert child.text == "Term loan.\nTerm loan continued."
    assert notes.top_level_chars(rows) == parent.chars


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


def test_a_chain_element_inside_an_earlier_element_of_the_same_chain_is_not_appended_twice():
    """The second half of the same rule, and the only shape that pins the stitch's memory of what it has read:
    the chain's second element is nested inside its first, which is itself outside the block. Without that
    memory ``Two.`` is stored twice and ``chars`` is long by its length."""
    rows, counts = notes.parse(CHAIN_INSIDE_EARLIER_CHAIN_ELEMENT)
    (note,) = rows
    assert note.text == "Note 7. Debt\nOne.\nTwo."
    assert counts == {"not_a_block": 3}


def test_a_chain_element_inside_a_different_block_is_kept_in_both_rows_and_counted():
    """Not the same as :data:`SHARED_CONTINUATION`: here one block claims the element and another *encloses* it,
    so the text is in both rows for two different reasons. The document does not say which note the paragraph
    belongs to, so it stays in both and the count says the top-level sum is long by that much."""
    by_name, counts = _by_name(CHAIN_ELEMENT_INSIDE_ANOTHER_BLOCK)
    assert by_name["DebtDisclosureTextBlock"].text == "Debt.\nText inside the leases note."
    assert by_name["LeasesTextBlock"].text == "Leases.\nText inside the leases note."
    assert counts["continuation_overlap"] == 1
    assert all(n.top_level for n in by_name.values()), "enclosed by a continuation, not by the block itself"


def test_the_overlap_is_seen_one_level_down_as_well():
    """The same double count through a continuation instead of a block: the element sits inside *another block's*
    continuation. The first version of this check asked only about block frames, so this one was silent -- which
    is the exact failure the count exists to prevent. 36 of the 90 characters here are counted twice."""
    by_name, counts = _by_name(CHAIN_INSIDE_ANOTHER_BLOCKS_CONTINUATION)
    assert counts["continuation_overlap"] == 1
    shared = "Text inside the leases chain."
    assert shared in by_name["DebtDisclosureTextBlock"].text
    assert shared in by_name["LeasesTextBlock"].text


def test_a_child_whose_own_chain_reaches_outside_its_parent_is_counted():
    """The other direction, and the one that makes the acceptance sum *short*: the child's second paragraph is in
    no top-level row at all, because the parent's text stops where the parent's tags do. Nothing here can repair
    that; the count is what stops a low total looking like a quiet filing."""
    rows, counts = notes.parse(CHILD_CHAIN_LEAVES_ITS_PARENT)
    parent, child = rows
    assert child.parent_seq == parent.seq and child.text not in parent.text
    assert child.text == "Term loan.\nTerm loan continued."
    assert counts["child_text_outside_parent"] == 1
    assert notes.top_level_chars(rows) == parent.chars < sum(n.chars for n in rows)


def test_a_continuation_with_no_id_at_all_is_counted_and_its_text_reaches_no_block():
    """``id`` is the only way to name a continuation, so without one the paragraph is unreachable. It is not an
    orphan -- an orphan is in the map and nobody claims it -- so it gets a count of its own."""
    rows, counts = notes.parse(CONTINUATION_WITH_NO_ID)
    (note,) = rows
    assert note.text == "Note 7. Debt" and "no id to reach it by" not in note.text
    assert counts["continuation_no_id"] == 1 and counts["continuation_missing"] == 1


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
                 SHARED_CONTINUATION, CONTINUED, IFRS_AND_PREFIXES, EMPTY_PARENT_BLOCK,
                 CHAIN_ELEMENT_INSIDE_ANOTHER_BLOCK, CHILD_CHAIN_LEAVES_ITS_PARENT, UNCLOSED_EXCLUDE,
                 HEALTHY_NESTED_CHILD_CHAIN, KEPT_GRANDPARENT_EMPTY_PARENT, MUTUAL_PARENT_BLOCKS,
                 BLOCK_CLAIMING_THE_CONTINUATION_IT_SITS_IN, THREE_BLOCKS_ONE_CONTINUATION, UNCLOSED_STYLE):
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


def test_a_row_is_never_left_pointing_at_a_parent_that_was_dropped_as_empty():
    """A parent that cuts no text of its own is not stored, and #757 would write its ``seq`` as a dangling key.
    The child is re-pointed at its nearest kept ancestor -- here there is none, so it is top-level, which is
    true: no stored block holds its words."""
    rows, counts = notes.parse(EMPTY_PARENT_BLOCK)
    (note,) = rows
    assert (note.seq, note.top_level, note.parent_seq) == (1, True, None)
    assert note.text == "The term loan matures in 2029."
    assert counts["empty"] == 1 and counts["parent_dropped_as_empty"] == 1


def test_a_row_whose_parent_was_dropped_names_its_grandparent_and_not_nothing():
    """The one shape where "nearest kept ancestor" and "top-level" differ, and the only thing that would notice a
    re-point replaced by ``parent_seq = None``. The grandparent does hold the child's tags, so saying the child
    is top-level would count its words twice in the acceptance sum."""
    rows, counts = notes.parse(KEPT_GRANDPARENT_EMPTY_PARENT)
    grandparent, child = rows
    assert (grandparent.seq, child.seq) == (0, 2), "the dropped parent keeps its seq; the rows are not renumbered"
    assert child.parent_seq == 0 and not child.top_level
    assert counts["empty"] == 1 and counts["parent_dropped_as_empty"] == 1


def test_two_blocks_that_are_each_other_s_parent_do_not_leave_the_filing_with_no_top_level_row():
    """Each one tagged inside the continuation the other claims. Unbroken, neither is top-level and the whole
    filing's text counts as zero characters -- which reads as "this filer tags nothing", the one wrong answer
    that looks like an ordinary one. Broken at the lowest ``seq``, and counted."""
    rows, counts = notes.parse(MUTUAL_PARENT_BLOCKS)
    assert counts["parent_cycle"] == 1
    assert [n.top_level for n in rows] == [True, False]
    assert notes.top_level_chars(rows) > 0


def test_a_block_claiming_the_continuation_it_sits_inside_is_not_its_own_parent():
    """The owner map then names the block itself, and a row whose ``parent_seq`` is its own ``seq`` is a cycle of
    one: #757 would store it, and the top-level sum would be zero."""
    rows, _ = notes.parse(BLOCK_CLAIMING_THE_CONTINUATION_IT_SITS_IN)
    (note,) = rows
    assert note.parent_seq is None and note.top_level
    assert note.seq == 1 and note.parent_seq != note.seq


def test_three_blocks_on_one_continuation_count_the_continuation_once():
    """``continuation_shared`` counts the text that is in more than one row, not the rows that hold it. Counting
    per block instead gives 3 here, and gives 2 for one block on a two-element chain, which is not a number."""
    rows, counts = notes.parse(THREE_BLOCKS_ONE_CONTINUATION)
    assert len(rows) == 3 and counts["continuation_shared"] == 1


def test_an_unclosed_exclude_is_counted_because_every_later_block_comes_out_empty():
    """The quietest fault in the document: after an ``ix:exclude`` the filer never closed, no text is collected
    at all, so a filing's remaining notes are simply absent and nothing about the rows says why."""
    rows, counts = notes.parse(UNCLOSED_EXCLUDE)
    assert [n.local_name for n in rows] == ["DebtDisclosureTextBlock"]
    assert rows[0].text == "Note 7. Debt", "the words before the exclude are the only ones that survive"
    assert counts["unclosed_exclude"] == 1 and counts["empty"] == 1


def test_an_unclosed_cover_fact_and_an_unclosed_stylesheet_are_each_counted():
    """Named here because nothing else names them, and each is a different silence: a cover fact left open makes
    every later block a child of something that is not a row, and a ``<style>`` left open puts ``html.parser``
    into CDATA mode, so the rest of the document is never read at all."""
    rows, counts = notes.parse(UNCLOSED_COVER_FACT)
    assert counts["unclosed_cover_fact"] == 1 and rows[0].top_level
    rows, counts = notes.parse(UNCLOSED_STYLE)
    assert counts["unclosed_markup_tag"] == 1
    assert [n.text for n in rows] == ["Note 7. Debt"], "what follows the stylesheet is never parsed"


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


@pytest.mark.parametrize("prose", [
    # Every one of these opens a bracket with a letter straight after it, which is what a tag looks like. A
    # pattern that stops at "<" plus a letter eats all of them, and the second review round caught it doing
    # exactly that to the first two.
    "Covenant holds where EBITDA<x and leverage>y.",
    "A floor of <LIBOR + 2%> applies.",
    "See <Table of Contents> above.",
    "Revenue<n> is reported in millions.",
    "Our site is <www.example.com>.",
    "The form was <S-1>.",
    "Debt fell 5<x<10 and y>3.",
    "The ratio is <1 in both years.",
    "Customers < 5%",
])
def test_text_of_leaves_prose_angle_brackets_alone(prose):
    counts: dict = {}
    assert notes.text_of([prose], counts) == prose
    assert counts == {}, "nothing tag-shaped was found, so there is nothing to report either"


def test_a_real_tag_name_in_a_sentence_is_left_alone_and_said_so():
    """``<b>`` is a tag name, and this sentence is still prose: a filer writing *about* its own markup. The test
    of a document is a structural tag -- a paragraph, a table, a list item -- and there is none here, so nothing
    is removed. The count is there because the decision went the other way on the same input shape."""
    counts: dict = {}
    prose = "We use <b> for bold in our own filings."
    assert notes.text_of([prose], counts) == prose
    assert counts == {"tag_shaped_text": 1}


def test_four_sentences_that_look_like_markup_survive_a_whole_block():
    """The same thing through ``parse``, because ``text_of`` is not where a filing's text comes from."""
    rows, counts = notes.parse(PROSE_THAT_LOOKS_LIKE_MARKUP)
    (note,) = rows
    assert note.text.splitlines() == [
        "Covenant holds where EBITDA<x and leverage>y for the period.",
        "The facility bears interest at a floor of <LIBOR + 2%>.",
        "We use <b> for bold in our own filings.",
        "Our site is <www.example.com> and the form was <S-1>.",
    ]
    assert "escaped_markup" not in counts and counts["tag_shaped_text"] == 1


def test_escaped_markup_becomes_prose_and_a_doubly_escaped_entity_becomes_its_character():
    """``escape="true"`` with its markup escaped reaches us as the text ``<p>``, and its entities were escaped
    twice, so ``&amp;nbsp;`` has to come back as a space. Both only where a tag was really found."""
    rows, counts = notes.parse(ESCAPED)
    (note,) = rows
    assert note.text == "Note 7. Debt\nWe borrowed money."
    assert "<p>" not in note.text and "&nbsp;" not in note.text
    assert counts["escaped_markup"] == 1


def test_an_escaped_note_with_attributes_a_table_and_a_url_comes_out_as_prose():
    """What pins the three parts of the escaped-markup rule, each of which a mutant otherwise walks through:

    - the **attribute group** -- ``<p style="margin:0">`` and ``<span class="x">`` are tags; without it they stay
      in the text;
    - the **breadth of ``_HTML_TAGS``** -- an escaped ``<table>``, ``<tr>`` and ``<td align="right">``; with the
      set cut back the table is stored as soup;
    - the **per-entity second pass** -- ``?id=1&copy=2`` in the prose. ``html.unescape`` over the whole string
      accepts a legacy entity name without its semicolon and writes a copyright sign into the URL.

    And a ``<td nowrap>`` stays, on purpose: a valueless attribute is not in the pattern, because a pattern that
    took one would also take ``<Table of Contents>``. It is counted rather than quietly left, which is the whole
    difference between a row that is readable and a row that is clean.
    """
    rows, counts = notes.parse(ESCAPED_WITH_ATTRIBUTES)
    (note,) = rows
    assert note.text == ("Note 7. Debt\nFiled at x.test/f?id=1&copy=2 by us.\n"
                         "<td nowrap>Term loan\n500\nThat is all.")
    assert "font-weight" not in note.text, "an escaped stylesheet is markup here too, not the note's prose"
    assert counts["escaped_markup"] == 1 and counts["escaped_markup_residue"] == 1


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
    assert row["context_ref"] == "FY", "the only thing that tells two rows of a combined filing apart"
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
