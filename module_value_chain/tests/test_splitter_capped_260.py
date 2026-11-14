"""#260: a slice cut at ``MAX_ITEM_CHARS`` is labelled ``capped``, not ``ok``.

An Item 1 body longer than the cap holds only part of the section in the stored
slice, so ``slice_item`` says so (the fundamentals layer already calls the case
``capped``). A cap larger than the body still reads ``ok``.

The cap itself moved with the rule (#752): it is now ``rcdata.sec_sections.MAX_ITEM_CHARS``, part of the
versioned cut, and ``module_value_chain.config.MAX_ITEM_CHARS`` is that same value re-exported for the readers
that record or report it. So this test patches the rule's cap, which is the one the cut reads. Patching the
board's alias no longer changes the cut, and that is the point of the version pin: the cap a stored slice was cut
at is a property of ``ours-1``, not a board setting that can move under already-stored rows.

Since #847 the board pins ``ours-2``, which caps nowhere, so the cut is asked of ``ours-1`` by name here rather
than through ``splitter.slice_item``. The rows this covers are the stored ones, cut by ``ours-1`` before the
re-slice, and they keep the label ``capped`` and the rank it has in ``engine._RANK``. The board's own cut is
``rcdata/tests/test_sec_sections.py::test_the_board_s_cut_no_longer_caps_or_says_capped``.
"""
from __future__ import annotations

from unittest import mock

from RevenueChaser.module_value_chain import engine
from RevenueChaser.rcdata import sec_sections

PROSE = "We make widgets for customers across many markets and we sell them through distributors worldwide.\n" * 60
TOC_10K = (" Table of Contents \n PART I \n Item 1. \n Business \n 3 \n Item 1A. \n Risk Factors \n 12 \n Item 2. \n"
           " Properties \n 20 \n Item 7. \n Management's Discussion and Analysis \n 30 \n")
RISK = " risk text \n" * 300

# Under the PROSE body (5,940 characters) but above MIN_ITEM_CHARS (2,000).
CAP = 5000


def _cut(text):
    item, quality = sec_sections.cut_item(text, "10-K", version=sec_sections.OURS_1)
    return item or "", quality


def test_item_1_longer_than_the_cap_is_capped():
    # The line-start path: a body heading and a line-start Item 1A stop after it.
    text = TOC_10K + " Item 1. Business \n" + PROSE + " Item 1A. Risk Factors \n" + RISK
    with mock.patch.object(sec_sections, "MAX_ITEM_CHARS", CAP):
        item, quality = _cut(text)
    assert quality == "capped"
    assert len(item) == CAP
    # The same text under a cap larger than the body is whole, so it stays ok (one test: red-on-base wants each new
    # test red on main, and this half alone passes there).
    with mock.patch.object(sec_sections, "MAX_ITEM_CHARS", 100_000):
        item, quality = _cut(text)
    assert quality == "ok" and item.count("We make widgets") == 60


def test_fallback_path_cut_at_the_cap_is_capped():
    # No line-start stop (Item 1A is inline), so the line-start rule declines and
    # the regex fallback cuts the section at the cap.
    text = "Item 1. Business " + PROSE.rstrip("\n") + " Item 1A. Risk Factors " + RISK
    with mock.patch.object(sec_sections, "MAX_ITEM_CHARS", CAP):
        item, quality = _cut(text)
    assert quality == "capped"
    assert len(item) == CAP


def test_capped_ranks_with_overrun_and_below_ok():
    assert engine._RANK["ok"] > engine._RANK["capped"]
    assert engine._RANK["capped"] == engine._RANK["overrun"]
