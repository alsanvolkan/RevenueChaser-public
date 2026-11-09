"""#400 10-K package WP4: the inline XBRL rules of the 10-K reading report (section 6, #3-#5, 8 Nov 2026).

#3 a segment tagged with a second dimension ``ConsolidationItemsAxis = OperatingSegmentsMember`` (40 of 130
documents: PG, PLD, TJX, NEE ...) is a segment; #4 a 20-F's IFRS revenue in its own currency (TSM, TM, SAP, NVO)
is read with that currency; #5 a named customer on ``MajorCustomersAxis`` counts whatever concentration type the
filer wrote (VRTX: McKesson 22% as ``ProductConcentrationRiskMember``).
"""
from __future__ import annotations

import pytest

from RevenueChaser.rcdata.build.fundamentals import ixbrl

from . import fund_fixture as fx

FY = ("2024-07-01", "2025-06-30")
SEG = "us-gaap:StatementBusinessSegmentsAxis"
OPS = {"srt:ConsolidationItemsAxis": "us-gaap:OperatingSegmentsMember"}
UNITS = ('<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
         '<xbrli:unit id="eur"><xbrli:measure>iso4217:EUR</xbrli:measure></xbrli:unit>'
         '<xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>')


def _doc(contexts: list[str], facts: list[str]) -> str:
    return ('<html><body><ix:header><ix:resources>' + "".join(contexts) + UNITS
            + "</ix:resources></ix:header>" + "".join(facts) + "</body></html>")


def test_a_segment_with_the_operating_segments_consolidation_member_is_a_segment():
    rev = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    html = _doc([fx._ctx("a", *FY, {SEG: "pg:BeautyMember", **OPS}),
                 fx._ctx("b", *FY, {SEG: "pg:BeautyMember", "srt:ConsolidationItemsAxis": "us-gaap:CorporateMember"})],
                [fx._ix(rev, "a", "15,000"), fx._ix(rev, "b", "9")])
    segments, _ = ixbrl.parse(html)
    assert [(s["label"], s["value"]) for s in segments] == [("Beauty", 15_000e6)]


def test_an_ifrs_revenue_in_its_own_currency_is_read_with_that_currency():
    html = _doc([fx._ctx("a", *FY, {SEG: "sap:CloudMember"})], [fx._ix("ifrs-full:Revenue", "a", "17,000", unit="eur")])
    segments, _ = ixbrl.parse(html)
    assert [(s["label"], s["unit"]) for s in segments] == [("Cloud", "EUR")]


def test_a_named_customer_counts_whatever_concentration_type_the_filer_wrote():
    dims = {"srt:MajorCustomersAxis": "vrtx:McKessonCorporationMember",
            "us-gaap:ConcentrationRiskByTypeAxis": "us-gaap:ProductConcentrationRiskMember",
            "us-gaap:ConcentrationRiskByBenchmarkAxis": "us-gaap:RevenueFromContractWithCustomerMember"}
    html = _doc([fx._ctx("a", *FY, dims)],
                [fx._ix("us-gaap:ConcentrationRiskPercentage1", "a", "22", unit="pure", scale="-2")])
    _, customers = ixbrl.parse(html)
    assert [(c["customer"], c["share"]) for c in customers] == [("Mc Kesson Corporation", pytest.approx(0.22))]
