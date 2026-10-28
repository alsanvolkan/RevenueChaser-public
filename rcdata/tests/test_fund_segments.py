"""``segment_revenue`` and ``customer_share`` (build plan PR 12): the inline XBRL of the stored annual report
(revenue by business segment, product and country; a major customer's ``ConcentrationRiskPercentage1``) and
Compustat's segments and customers (``wrds/segments``, a fake worker, never WRDS)."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata.build.fundamentals import companyfacts, filings, ixbrl, wrds_fund
from RevenueChaser.rcdata.build.spine import wrds as spine_wrds

from . import fund_fixture as fx

FY25 = date(2025, 6, 30)


def test_the_newest_year_by_one_dimension_and_the_first_revenue_concept():
    segments, customers = ixbrl.parse(fx.ixbrl_10k())
    assert sorted((s["axis"], s["label"], s["value"], s["concept"], s["period_start"], s["period_end"])
                  for s in segments) == [
        ("business", "Intelligent Cloud", 106_265e6, "RevenueFromContractWithCustomerExcludingAssessedTax",
         date(2024, 7, 1), FY25),
        ("business", "More Personal Computing", 51_500e6, "RevenueFromContractWithCustomerExcludingAssessedTax",
         date(2024, 7, 1), FY25),
        ("geographic", "US", 144_546e6, "RevenueFromContractWithCustomerExcludingAssessedTax", date(2024, 7, 1), FY25),
        ("product", "Xbox Content And Services", 23_455e6, "RevenueFromContractWithCustomerExcludingAssessedTax",
         date(2024, 7, 1), FY25)]
    assert [(c["customer"], c["member"], c["benchmark"], c["share"], c["period_end"]) for c in customers] == [
        ("Customer A", "msft:CustomerAMember", "revenue", 0.12, FY25)]


@pytest.mark.parametrize("text, fmt, scale, sign, value", [
    ("1,234.5", "ixt:num-dot-decimal", "6", "", 1_234.5e6), ("1.234,5", "ixt:num-comma-decimal", "3", "", 1_234_500.0),
    ("—", "ixt:fixed-zero", "6", "", 0.0), ("(12)", "ixt:num-dot-decimal", "0", "-", -12.0),
    ("7", "ixt-sec:numwordsen", "0", "", None)])
def test_a_number_as_displayed(text, fmt, scale, sign, value):
    assert ixbrl.number(text, fmt, scale, sign) == value


@pytest.fixture
def sec(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    fx.spine()
    return http_mock


def test_the_annual_report_serves_its_segments_and_customer(sec):
    fx.filing_routes(sec, {"789019": fx.submissions(789019, fx.MSFT_FILINGS)},
                     {"msft-10k.htm": fx.ixbrl_10k(), "msft-def14a.htm": fx.proxy()})
    fx.run(filings.NAME, mode="backfill", to="2026-10-28")
    assert fx.q("SELECT axis, segment, value, accession, source FROM segment_revenue WHERE axis = 'business' "
                "ORDER BY segment") == [("business", "Intelligent Cloud", 106_265e6, fx.K25, filings.NAME),
                                        ("business", "More Personal Computing", 51_500e6, fx.K25, filings.NAME)]
    assert fx.q("SELECT cik, customer, benchmark, share, period_end, accession, source FROM customer_share") == [
        ("789019", "Customer A", "revenue", 0.12, FY25, fx.K25, filings.NAME)]


@pytest.fixture
def fake(monkeypatch, http_mock, sec):
    fx.sec_routes(http_mock, {"789019": fx.msft_facts()})
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")       # MSFT's FY2025 revenue: 281,000
    conn = fx.FakeFundWrds()
    monkeypatch.setattr(spine_wrds, "_factory", lambda: conn)
    spine_wrds.reset()
    yield conn
    spine_wrds.reset()


def test_compustat_segments_and_customers(fake):
    check = fx.run(wrds_fund.SEGMENTS.name, mode="backfill", to="2026-10-27")
    assert check["result"] == "changed", check["note"]
    seg_q, cust_q = fake.queries
    assert "FROM comp_segments_hist_daily.wrds_segmerged" in seg_q and "stype IN ('BUSSEG', 'GEOSEG', 'OPSEG')" in seg_q
    assert "FROM comp_segments_hist_daily.seg_customer" in cust_q
    # The newest source filing of a segment; the state segment is not kept; Compustat's millions in USD.
    assert fx.q("SELECT axis, segment, member, value, period_end, source FROM segment_revenue ORDER BY axis") == [
        ("business", "Intelligent Cloud", "BUSSEG 1", 106_265.0, FY25, wrds_fund.SEGMENTS.name),
        ("geographic", "United States", "GEOSEG 2", 144_546.0, FY25, wrds_fund.SEGMENTS.name)]
    # Customer A's 33,720 of sales over the SEC's 281,000 of revenue.
    rows = fx.q("SELECT customer, customer_type, sales, share, benchmark, source FROM customer_share")
    assert [r[:3] + (round(r[3], 4),) + r[4:] for r in rows] == [
        ("Customer A", "COMPANY", 33_720.0, 0.12, "revenue", wrds_fund.SEGMENTS.name)]
    assert "1 of 4 Tier A companies with segments" in check["note"]
