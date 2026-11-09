"""#400 10-K package WP2: a slice labelled ``ok`` that still looks wrong says so in its ``quality`` text.

The signals and the loose threshold are the ones measured in the 10-K reading report (§8, 8 Nov 2026): any of
S1 (under 2,000 characters), S4 (ends on "see / refer to / described in"), S5 (the start heading is a reference),
S6 (another item heading inside), S8 (at the cap), or two of S2 (under 3% of a document over 150k), S3 (no
Competition / Human Capital / Employees / Available Information heading, 10-K only) and S4. avo's decision D
(8 Nov): the flags go into the existing ``quality`` TEXT ("ok;suspect:S4"), no schema bump.
"""
from __future__ import annotations

from RevenueChaser.module_value_chain import config, engine, reslice, splitter
from RevenueChaser.rcdata.build.fundamentals import filings

PROSE = "We make widgets for customers across many markets and we sell them through distributors worldwide.\n" * 60
CLEAN = " Competition \n" + PROSE + " Human Capital \n" + PROSE


def test_a_clean_slice_stays_plain_ok():
    assert splitter.flag("ok", CLEAN * 2, CLEAN, "10-K") == "ok"


def test_a_slice_ending_on_a_reference_is_suspect():
    item = CLEAN + " For more on these risks, see “"
    assert splitter.flag("ok", item * 2, item, "10-K") == "ok;suspect:S4"


def test_another_item_heading_inside_the_slice_is_suspect():
    item = CLEAN + " ITEM 7. MANAGEMENT’S DISCUSSION AND ANALYSIS \n" + PROSE
    assert "S6" in splitter.flag("ok", item, item, "10-K")


def test_a_20f_running_item_4_header_is_not_another_item():
    item = CLEAN + " ITEM 4. INFORMATION ON THE COMPANY \n" + PROSE
    assert splitter.flag("ok", item, item, "20-F") == "ok"


def test_two_weak_signals_together_are_suspect_one_alone_is_not():
    doc = PROSE * 40                                   # 6k of a 240k document, and no section heading at all
    assert splitter.flag("ok", doc, PROSE, "10-K") == "ok;suspect:S2,S3"
    assert splitter.flag("ok", PROSE * 2, PROSE, "10-K") == "ok"          # S3 alone
    assert splitter.flag("ok", PROSE * 2, PROSE, "20-F") == "ok"          # S3 is a 10-K signal


def test_a_slice_at_the_cap_is_suspect():
    item = (CLEAN * 40)[:config.MAX_ITEM_CHARS]
    assert splitter.flag("ok", item, item, "10-K") == "ok;suspect:S8"


def test_only_an_ok_label_is_flagged():
    item = PROSE[:1500] + " see “"
    for q in ("short", "missing", "overrun", "capped"):
        assert splitter.flag(q, item, item, "10-K") == q


def test_a_suspect_ok_ranks_as_ok_in_the_store_guard_and_the_reslice():
    assert engine._RANK.get(splitter.base("ok;suspect:S4")) == 2
    assert reslice._rank("ok;suspect:S4") == reslice._rank("ok")
    assert reslice._classify("ok", "a", "ok;suspect:S4", "b") == "ok_text_changed"
    assert reslice._classify("ok;suspect:S6", "a", "overrun", "a") == "relabel"


def test_the_served_business_row_carries_the_flag():
    html = ("<html><body><p>Item 1. Business</p><p>Item 1A. Risk Factors</p><p>PART I</p><p>Item 1. Business</p>"
            f"<p>{PROSE * 2}</p><p>For details, see Note 12 of the</p><p>Item 1A. Risk Factors</p><p>Risks.</p></body></html>")
    by_item = {i: q for i, _, q in filings.document_sections(html, "10-K")}
    assert by_item["business"] == "ok;suspect:S3,S4"      # no section heading, and the tail
