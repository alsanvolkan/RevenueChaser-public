"""The fundamentals layer's read API (build plan PR 12): statements, earnings events, estimates, filing texts,
segments, customers, peers and revisions, each row with its provenance and the newest check of its own source."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import ops, read
from RevenueChaser.rcdata.build.fundamentals import companyfacts, filings

from . import fund_fixture as fx


def test_nothing_built_yet_is_no_rows():
    assert read.statements("789019") == [] and read.peers("789019") == [] and read.filing_text("789019") is None


@pytest.fixture
def built(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    fx.spine()
    fx.sec_routes(http_mock, {"789019": fx.msft_facts()})
    fx.filing_routes(http_mock, {"789019": fx.submissions(789019, fx.MSFT_FILINGS)},
                     {"msft-10k.htm": fx.ixbrl_10k(), "msft-def14a.htm": fx.proxy()})
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")
    fx.run(filings.NAME, mode="backfill", to="2026-10-27")


@pytest.mark.usefixtures("built")
def test_statements_newest_periods_first_with_provenance_and_status():
    rows = read.statements(789019, "quarterly", n=1)
    assert {r["period_end"] for r in rows} == {date(2025, 9, 30)}
    revenue = next(r for r in rows if r["field"] == "revenue")
    assert (revenue["value"], revenue["accn"], revenue["filed"]) == (77_000.0, fx.Q26, date(2025, 10, 29))
    assert revenue["status"] == ops.latest_check(companyfacts.NAME)["result"]
    annual = read.statements("789019", "annual", fields=["total_debt"])
    assert [(r["fiscal_year"], r["value"]) for r in annual] == [(2025, 43_500.0)]


@pytest.mark.usefixtures("built")
def test_the_filing_side():
    assert [(r["report_date"], r["confirmed"]) for r in read.earnings_events("789019")] == [
        (date(2025, 10, 29), True)]
    text = read.filing_text("789019")
    assert text["accession"] == fx.K25 and text["quality"] == "ok" and text["status"] == \
        ops.latest_check(filings.NAME)["result"]
    assert read.filing_text("789019", "proxy")["form"] == "DEF 14A"
    assert [r["segment"] for r in read.segments("789019", axis="business")] == [
        "Intelligent Cloud", "More Personal Computing"]                   # the biggest first
    assert [(r["customer"], r["share"]) for r in read.customers("789019")] == [("Customer A", 0.12)]
    assert [(r["peer_ticker"], r["level"]) for r in read.peers("789019")] == [("ORCL", "gsubind")]
    assert read.estimates(["MSFT"]) == [] and read.revisions("789019") == []
    # A row's status is its own source's newest check (peer groups are the tables build's).
    assert {r["status"] for r in read.peers("789019")} == {read.NEVER_CHECKED}
