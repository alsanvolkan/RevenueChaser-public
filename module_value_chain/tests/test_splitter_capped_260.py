"""#260: a slice cut at ``MAX_ITEM_CHARS`` is labelled ``capped``, not ``ok``.

An Item 1 body longer than the cap holds only part of the section in the stored
slice, so ``slice_item`` says so (the fundamentals layer already calls the case
``capped``). A cap larger than the body still reads ``ok``.
"""
from __future__ import annotations

from unittest import mock

from RevenueChaser.module_value_chain import config, engine, splitter

PROSE = "We make widgets for customers across many markets and we sell them through distributors worldwide.\n" * 60
TOC_10K = (" Table of Contents \n PART I \n Item 1. \n Business \n 3 \n Item 1A. \n Risk Factors \n 12 \n Item 2. \n"
           " Properties \n 20 \n Item 7. \n Management's Discussion and Analysis \n 30 \n")
RISK = " risk text \n" * 300

# Under the PROSE body (5,940 characters) but above MIN_ITEM_CHARS (2,000).
CAP = 5000


def _cut(text):
    item, quality = splitter.slice_item(text)
    return item or "", quality


def test_item_1_longer_than_the_cap_is_capped():
    # The line-start path: a body heading and a line-start Item 1A stop after it.
    text = TOC_10K + " Item 1. Business \n" + PROSE + " Item 1A. Risk Factors \n" + RISK
    with mock.patch.object(config, "MAX_ITEM_CHARS", CAP):
        item, quality = _cut(text)
    assert quality == "capped"
    assert len(item) == CAP


def test_item_1_with_a_larger_cap_is_ok():
    text = TOC_10K + " Item 1. Business \n" + PROSE + " Item 1A. Risk Factors \n" + RISK
    with mock.patch.object(config, "MAX_ITEM_CHARS", 100_000):
        item, quality = _cut(text)
    assert quality == "ok" and item.count("We make widgets") == 60


def test_fallback_path_cut_at_the_cap_is_capped():
    # No line-start stop (Item 1A is inline), so the line-start rule declines and
    # the regex fallback cuts the section at the cap.
    text = "Item 1. Business " + PROSE.rstrip("\n") + " Item 1A. Risk Factors " + RISK
    with mock.patch.object(config, "MAX_ITEM_CHARS", CAP):
        item, quality = _cut(text)
    assert quality == "capped"
    assert len(item) == CAP


def test_capped_ranks_with_overrun_and_below_ok():
    assert engine._RANK["ok"] > engine._RANK["capped"]
    assert engine._RANK["capped"] == engine._RANK["overrun"]
