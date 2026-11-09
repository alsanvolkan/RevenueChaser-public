"""#400 10-K package WP3: the 20-F and 40-F rules of the 10-K reading report (section 4, A6-A7, 8 Nov 2026).

20-F: Item 4.B's regulation chapter is dropped (PDD 87% and BABA 68% of 4.B are PRC regulation). 40-F: the business
text of an Annual Information Form kept in the primary document (CNQ) is "General Development" to "Risk Factors",
and an AIF filed as Exhibit 1 (RY ``dex1.htm``) is found.
"""
from __future__ import annotations

from RevenueChaser.module_value_chain import splitter
from RevenueChaser.rcdata.build.fundamentals import filings

PROSE = "We make widgets for customers across many markets and we sell them through distributors worldwide.\n" * 60
REG = "The PRC government regulates the internet sector through licences and approvals.\n" * 300
TOC_20F = (" ITEM 4. \n INFORMATION ON THE COMPANY \n 24 \n ITEM 4A. \n UNRESOLVED STAFF COMMENTS \n 101 \n"
           " ITEM 5. \n OPERATING AND FINANCIAL REVIEW \n 101 \n")


def _item_4(middle: str) -> str:
    return (TOC_20F + " ITEM 4. INFORMATION ON THE COMPANY \n A. History and Development of the Company \n" + PROSE
            + middle + " ITEM 4A. UNRESOLVED STAFF COMMENTS \n None. \n ITEM 5. OPERATING \n" + PROSE)


def test_the_regulation_chapter_of_4b_is_dropped():
    item, quality = splitter.slice_item(_item_4(" B. Business Overview \n" + PROSE + " PRC Regulations \n" + REG), "20-F")
    assert quality == "ok" and item.count("We make widgets") == 120 and "PRC government" not in item


def test_a_regulation_heading_before_4b_does_not_cut():
    item, _ = splitter.slice_item(_item_4(" Regulation \n" + PROSE + " B. Business Overview \n" + PROSE), "20-F")
    assert item.count("We make widgets") == 180


AIF = (" ANNUAL INFORMATION FORM \n TABLE OF CONTENTS \n GENERAL DEVELOPMENT OF THE BUSINESS \n 5 \n"
       " DESCRIPTION OF THE BUSINESS \n 9 \n RISK FACTORS \n 40 \n" + " Forward-looking statements apply.\n" * 50
       + " GENERAL DEVELOPMENT OF THE BUSINESS \n" + PROSE + " DESCRIPTION OF THE BUSINESS \n" + PROSE
       + " RISK FACTORS \n" + " risk text \n" * 300)


def test_an_aif_in_the_40f_primary_document_gives_a_business_row():
    html = "<html><body>" + "".join(f"<p>{line}</p>" for line in AIF.split("\n")) + "</body></html>"
    rows = {item: (text, quality) for item, text, quality in filings.document_sections(html, "40-F")}
    text, quality = rows["business"]
    assert quality.startswith("ok") and text.count("We make widgets") == 120 and "risk text" not in text


def test_a_40f_without_an_aif_still_has_only_the_whole_row():
    html = "<html><body><p>" + PROSE + "</p></body></html>"
    assert {item for item, _, _ in filings.document_sections(html, "40-F")} == {"whole"}


def test_an_aif_filed_as_exhibit_1_is_found():
    listing = {"directory": {"item": [{"name": "d40f.htm", "size": "90000"}, {"name": "dex1.htm", "size": "800000"},
                                      {"name": "dex10.htm", "size": "900000"}, {"name": "dex2.htm", "size": "700000"}]}}
    assert filings.aif_exhibits(listing, "d40f.htm") == ["dex1.htm"]
