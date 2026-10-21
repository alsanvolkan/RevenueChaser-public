"""Item 1 / Item 4 header layouts vs the splitter (#116 C17), offline.

Each case is synthetic HTML shaped like a real EDGAR layout: a contents table,
then the body. A case passes when the slice is ``ok`` and holds the business
body only (no Item 1A risk text). Ported from the #116 review corpus: the
patterns before #116 got 12 of these 39 right.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_value_chain import splitter

BODY = ("<p>We design semiconductors. We purchase wafers from Taiwan Semiconductor "
        "Manufacturing Company Limited, our primary foundry supplier, and our customers "
        "include Microsoft and Amazon. " + "We sell products worldwide through distributors. " * 60
        + "</p>")
RISK = "<p>Our business is subject to many risks. " + "Risk text. " * 50 + "</p>"


def _toc(h1, h1a):
    def row(h, page):
        n, _, t = h.partition("|")
        return f"<tr><td>{n}</td><td>{t}</td><td>{page}</td></tr>"
    return f"<table><tr><td>TABLE OF CONTENTS</td></tr>{row(h1, 3)}{row(h1a, 12)}</table>"


def _doc10k(start, stop="Item 1A.|Risk Factors", toc_start=None, toc_stop=None,
            xref=False, no_toc=False):
    body = BODY
    if xref:  # the body cross-references Item 1A early, mid-paragraph
        body = "<p>Our results depend on suppliers; see Item 1A of this report for risks. " + body[3:]
    toc = "" if no_toc else _toc(toc_start or start, toc_stop or stop)
    return (f"<html><body>{toc}<p>PART I</p><p><b>{start.replace('|', ' ')}</b></p>{body}"
            f"<p><b>{stop.replace('|', ' ')}</b></p>{RISK}</body></html>")


def _doc20f(start, stop="Item 4A.|Unresolved Staff Comments"):
    return (f"<html><body>{_toc(start, stop)}<p><b>{start.replace('|', ' ')}</b></p>{BODY}"
            f"<p><b>{stop.replace('|', ' ')}</b></p>{RISK}</body></html>")


_FORTYF_WRAPPER = (
    "<html><body><p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p><p>FORM 40-F</p>"
    "<p>ANNUAL INFORMATION FORM</p><p>The Annual Information Form of the Registrant is filed as "
    "Exhibit 99.1 and is incorporated by reference.</p>"
    "<p>DISCLOSURE CONTROLS AND PROCEDURES</p><p>" + "Controls text. " * 80 + "</p>"
    "<p>SIGNATURES</p></body></html>")
_AIF = (
    "<html><body><p>ANNUAL INFORMATION FORM</p><table><tr><td>GENERAL DEVELOPMENT OF THE "
    "BUSINESS</td><td>5</td></tr><tr><td>DESCRIPTION OF THE BUSINESS</td><td>9</td></tr>"
    "<tr><td>RISK FACTORS</td><td>30</td></tr></table>"
    "<p><b>GENERAL DEVELOPMENT OF THE BUSINESS</b></p><p>" + "History. " * 40 + "</p>"
    "<p><b>DESCRIPTION OF THE BUSINESS</b></p>" + BODY + "<p><b>RISK FACTORS</b></p>" + RISK
    + "</body></html>")

# name -> (html, form, expected to slice correctly)
CASES = {
    "std 'Item 1. Business'": (_doc10k("Item 1.|Business"), "10-K", True),
    "upper 'ITEM 1. BUSINESS'": (_doc10k("ITEM 1.|BUSINESS", "ITEM 1A.|RISK FACTORS"), "10-K", True),
    "colon 'Item 1: Business'": (_doc10k("Item 1:|Business"), "10-K", True),
    "no punct 'Item 1 Business'": (_doc10k("Item 1|Business"), "10-K", True),
    "nbsp": (_doc10k("Item&nbsp;1.&nbsp;Business"), "10-K", True),
    "split line 'Item 1.<br>Business'": (_doc10k("Item 1.<br>Business"), "10-K", True),
    "hyphen 'Item 1 - Business'": (_doc10k("Item 1 -|Business"), "10-K", True),
    "double hyphen 'Item 1 -- Business'": (_doc10k("Item 1 --|Business"), "10-K", True),
    "em dash 'Item 1 — Business'": (_doc10k("Item 1 —|Business"), "10-K", True),
    "em dash tight 'Item 1—Business'": (_doc10k("Item 1—Business"), "10-K", True),
    "en dash 'ITEM 1 – BUSINESS'": (_doc10k("ITEM 1 –|BUSINESS", "ITEM 1A –|RISK FACTORS"),
                                         "10-K", True),
    "entity em dash '&#8212;'": (_doc10k("Item 1 &#8212;|Business"), "10-K", True),
    "period + dash 'Item 1. — Business'": (_doc10k("Item 1. —|Business"), "10-K", True),
    "'Items 1 and 2. Business and Properties'": (
        _doc10k("Items 1 and 2.|Business and Properties"), "10-K", True),
    "'ITEMS 1 AND 2. BUSINESS AND PROPERTIES'": (
        _doc10k("ITEMS 1 AND 2.|BUSINESS AND PROPERTIES", "ITEM 1A.|RISK FACTORS"), "10-K", True),
    "'Items 1 & 2.'": (_doc10k("Items 1 &amp; 2.|Business and Properties"), "10-K", True),
    "'Item 1 and 2.'": (_doc10k("Item 1 and 2.|Business and Properties"), "10-K", True),
    "'Items 1. and 2.'": (_doc10k("Items 1. and 2.|Business and Properties"), "10-K", True),
    "'Item 1. Description of Business'": (_doc10k("Item 1.|Description of Business"), "10-K", True),
    "'Item 1. Our Business'": (_doc10k("Item 1.|Our Business"), "10-K", True),
    "small caps split 'B<font>USINESS'": (_doc10k("Item 1.|B<font size=1>USINESS</font>"), "10-K", True),
    "TOC std, body em dash": (_doc10k("Item 1 —|Business", toc_start="Item 1.|Business",
                                      toc_stop="Item 1A.|Risk Factors"), "10-K", True),
    "TOC em dash, body std": (_doc10k("Item 1.|Business", toc_start="Item 1 —|Business"),
                              "10-K", True),
    "no TOC, em dash": (_doc10k("Item 1 —|Business", "Item 1A —|Risk Factors", no_toc=True),
                        "10-K", True),
    "stop em dash tight 'Item 1A—Risk Factors'": (
        _doc10k("Item 1.|Business", "Item 1A—Risk Factors"), "10-K", True),
    "stop hyphen tight 'Item 1A-Risk Factors'": (
        _doc10k("Item 1.|Business", "Item 1A-Risk Factors"), "10-K", True),
    "stop missing, next is 'Item 1B.'": (
        _doc10k("Item 1.|Business", "Item 1B.|Unresolved Staff Comments"), "10-K", True),
    "stop missing, next is 'Item 2.'": (_doc10k("Item 1.|Business", "Item 2.|Properties"), "10-K", True),
    "body xref 'see Item 1A of this report'": (_doc10k("Item 1.|Business", xref=True), "10-K", True),
    "TOC on one line": (
        "<html><body><p>TABLE OF CONTENTS Item 1. Business 3 Item 1A. Risk Factors 12 Item 2. "
        "Properties 20</p><p><b>Item 1. Business</b></p>" + BODY
        + "<p><b>Item 1A. Risk Factors</b></p>" + RISK + "</body></html>", "10-K", True),
    "inline stop (no block break before Item 1A)": (
        "<html><body>" + _toc("Item 1.|Business", "Item 1A.|Risk Factors")
        + "<p><b>Item 1. Business</b></p>" + BODY[:-4] + " <b>Item 1A. Risk Factors</b> " + RISK[3:]
        + "</body></html>", "10-K", True),
    "20-F std": (_doc20f("Item 4.|Information on the Company"), "20-F", True),
    "20-F upper": (_doc20f("ITEM 4.|INFORMATION ON THE COMPANY", "ITEM 4A.|UNRESOLVED STAFF COMMENTS"),
                   "20-F", True),
    "20-F em dash": (_doc20f("Item 4 —|Information on the Company"), "20-F", True),
    "20-F stop at 'Item 5.'": (
        _doc20f("Item 4.|Information on the Company", "Item 5.|Operating and Financial Review"),
        "20-F", True),
    "20-F 'Information on the Group'": (_doc20f("Item 4.|Information on the Group"), "20-F", True),
    # Integrated annual report: Item 4 is only a row in a cross-reference index.
    "20-F cross-reference index only": (
        "<html><body><table><tr><td>Form 20-F item</td><td>Location</td></tr>"
        "<tr><td>Item 4. Information on the Company</td><td>Strategic report, pages 10-58</td></tr>"
        "<tr><td>Item 4A. Unresolved Staff Comments</td><td>None</td></tr></table>"
        "<p><b>Strategic report</b></p>" + BODY + "<p><b>Principal risks</b></p>" + RISK
        + "</body></html>", "20-F", False),
    # 40-F: no Item 1; the business text is in the AIF exhibit (#184).
    "40-F wrapper": (_FORTYF_WRAPPER, "40-F", False),
    "40-F AIF exhibit": (_AIF, "40-F", False),
}


def _correct(html: str, form: str) -> bool:
    item, quality = splitter.slice_item(splitter.strip_html(html), form)
    return (quality == "ok" and bool(item) and "Taiwan Semiconductor" in item
            and "Our business is subject" not in item)


def test_corpus_has_39_layouts():
    assert len(CASES) == 39


@pytest.mark.parametrize("name", [n for n, c in CASES.items() if c[2]])
def test_layout_slices_the_body_only(name):
    html, form, _ = CASES[name]
    assert _correct(html, form)


def test_corpus_hit_rate_at_least_36_of_39():
    hits = sum(_correct(html, form) for html, form, _ in CASES.values())
    assert hits >= 36


@pytest.mark.parametrize("header", ["Item 10. Business Conduct and Governance",
                                    "Item 11. Business Combinations",
                                    "Item 1A. Business Risks"])
def test_other_items_are_not_an_item_1_start(header):
    html = _doc10k("Item 1.|Business").replace("<p>PART I</p>", f"<p>{header}</p>" + BODY)
    item, quality = splitter.slice_item(splitter.strip_html(html), "10-K")
    # The real Item 1 still wins; the decoy heading never starts a slice.
    assert quality == "ok" and item.startswith("We design semiconductors")


def test_a_slice_without_a_stop_header_is_not_ok():
    """No Item 1A / 1B / 2 header after the start: the slice runs on into the risk
    text (or to the cap), so it must not be labelled ``ok``."""
    html = _doc10k("Item 1.|Business", "Unknown heading", no_toc=True)
    item, quality = splitter.slice_item(splitter.strip_html(html), "10-K")
    assert quality == "overrun"
    assert "Taiwan Semiconductor" in item
