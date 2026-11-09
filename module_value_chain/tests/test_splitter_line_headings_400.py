"""#400 10-K package WP1: the Item 1 / Item 4 cut on the shapes the served text really has.

Each shape is taken from a filing of the #400 input checks (rounds 2-3, 9 Nov 2026), where ``slice_item`` was wrong
while labelling the slice ``ok`` (ITUB, IH, ALKS, F, MREO, NBP), ``overrun`` (MXC, ONEG), ``short`` (REFR, HYFT) or
``missing`` (PHM, SAP). The filings themselves stay out of the repository; the pilot's 32-filing check is in
alsanvolkan/claude-coordinator pilots/400-relatedness-evren (fixtures-r3).
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_value_chain import config, splitter

PROSE = "We make widgets for customers across many markets and we sell them through distributors worldwide.\n" * 60
TOC_10K = (" Table of Contents \n PART I \n Item 1. \n Business \n 3 \n Item 1A. \n Risk Factors \n 12 \n Item 2. \n"
           " Properties \n 20 \n Item 7. \n Management’s Discussion and Analysis \n 30 \n")
TOC_20F = (" ITEM 4. \n INFORMATION ON THE COMPANY \n 24 \n 4A. \n History and Development of the Company \n 24 \n"
           " ITEM 4A. \n UNRESOLVED STAFF COMMENTS \n 101 \n ITEM 5. \n OPERATING AND FINANCIAL REVIEW \n 101 \n")
RISK = " risk text \n" * 300


def _cut(text, form="10-K"):
    item, quality = splitter.slice_item(text, form)
    return item or "", quality


def test_quoted_cross_reference_mid_line_does_not_end_item_4():
    # ITUB: "... see “Item 5. Operating and Financial Review ...”" cut a 293k Item 4 at 16k, labelled ok.
    text = (TOC_20F + " ITEM 4. INFORMATION ON THE COMPANY \n 4A. History and Development \n" + PROSE
            + " For a description of our capital expenditures, see “Item 5. Operating and Financial Review”.\n"
            + PROSE + " ITEM 4A. UNRESOLVED STAFF COMMENTS \n Not applicable. \n ITEM 5. OPERATING \n" + RISK)
    item, quality = _cut(text, "20-F")
    assert quality == "ok" and item.count("We make widgets") == 120 and "UNRESOLVED" not in item


def test_quoted_item_1a_reference_does_not_end_item_1():
    # ALKS, MREO: 'see “Item 1A—Risk Factors” in this Annual Report' inside Item 1.
    text = (TOC_10K + " Item 1. Business \n" + PROSE + " described in “Item 1A—Risk Factors” in this Annual Report.\n"
            + PROSE + " Item 1A. Risk Factors \n" + RISK)
    item, quality = _cut(text)
    assert quality == "ok" and item.count("We make widgets") == 120 and "risk text" not in item


def test_any_later_item_closes_item_1():
    # MXC: the body ran on into ITEM 7 (overrun, 137k) because no 1A / 1B / 2 heading matched.
    text = TOC_10K + " ITEM 1. BUSINESS \n" + PROSE + " ITEM 7. MANAGEMENT’S DISCUSSION AND ANALYSIS \n" + RISK
    item, quality = _cut(text)
    assert quality == "ok" and "risk text" not in item


@pytest.mark.parametrize("heading,form", [("ITEM I. BUSINESS", "10-K"),                      # PHM: missing
                                          ("ITEM 4. INFORMATION ABOUT SAP", "20-F"),          # SAP: missing
                                          ("ITEM 4. INFORMATION OF THE COMPANY", "20-F"),     # CIGL
                                          ("ITEM 4. INFO RMATION ON THE COMPANY", "20-F")])   # HYFT, NBP
def test_heading_variants_are_a_start(heading, form):
    stop = " ITEM 4A. UNRESOLVED STAFF COMMENTS \n" if form == "20-F" else " ITEM 1A. RISK FACTORS \n"
    item, quality = _cut((TOC_20F if form == "20-F" else TOC_10K) + f" {heading} \n" + PROSE + stop + RISK, form)
    assert quality == "ok" and item.count("We make widgets") == 60 and "risk text" not in item


def test_contents_entry_followed_by_page_number_is_not_the_body():
    # AEE: "Item 1. Business \n 7 \n General \n 7 ..." with sub-entries, 1A further than 400 characters on.
    # A contents block longer than MIN_ITEM_CHARS: the old "first slice of 2,000 wins" took it as the body.
    toc = " Item 1. Business \n 7 \n" + " General Section \n 7 \n" * 150 + " Item 1A. Risk Factors \n 22 \n"
    item, quality = _cut(toc + " Item 1. Business \n" + PROSE + " Item 1A. Risk Factors \n" + RISK)
    assert quality == "ok" and item.lstrip().startswith("We make widgets") and "risk text" not in item


def test_item_4_longer_than_the_cap_is_capped_and_ok():
    # BCH: Item 4 is 404k and names "Item 4A" in a sentence early on; the slice is capped at the body heading.
    big = PROSE * 2 + " as noted in “Item 4A. Unresolved Staff Comments” below.\n" + PROSE * 38
    item, _ = _cut(TOC_20F + " Item 4 Information on the Company \n" + big + " Item 4A Unresolved Staff Comments \n", "20-F")
    assert len(item) == config.MAX_ITEM_CHARS and item.lstrip().startswith("We make widgets")
