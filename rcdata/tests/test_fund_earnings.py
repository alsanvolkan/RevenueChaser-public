"""``earnings_event`` (build plan PR 12): Alpha Vantage's coming report dates (its key by name only, never stored or
logged) and the SEC's 8-K item 2.02 reports, with the old ``earnings_calendar`` view."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.fundamentals import earnings, filings

from . import fund_fixture as fx

KEY = "av-test-key-not-a-real-one"
CSV_DAY1 = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\r\n"
            "MSFT,Microsoft Corp,2026-10-29,2026-09-30,3.85,USD,post-market\r\n"
            "HEI,HEICO Corp,2026-12-18,2026-10-31,1.10,USD,\r\n"
            "ZZZZ,Not Tier A,2026-11-01,2026-09-30,,USD,pre-market\r\n")
CSV_DAY2 = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\r\n"
            "HEI,HEICO Corp,2026-12-17,2026-10-31,1.12,USD,pre-market\r\n")


@pytest.fixture
def av(monkeypatch, http_mock):
    monkeypatch.setenv("RC_ALPHAVANTAGE_BASE", "https://av.test")
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", KEY)
    fx.spine()
    return http_mock


def _raw_bytes() -> bytes:
    return b"".join(p.read_bytes() for p in base.raw_root().rglob("*") if p.is_file())


def test_no_key_asks_nothing_and_says_so(av, monkeypatch):
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY")
    check = fx.run(earnings.NAME, "2026-10-28")
    assert check["result"] == "unchanged" and "no ALPHAVANTAGE_API_KEY" in check["note"]
    assert not av.requests
    assert [i["kind"] for i in ops.open_issues(earnings.NAME)[1]] == ["missing_key"]


def test_the_calendar_serves_the_tier_a_reports_and_never_keeps_the_key(av):
    av.add("av.test/query", CSV_DAY1)
    check = fx.run(earnings.NAME, "2026-10-28")
    assert check["result"] == "changed", check["note"]
    (req,) = av.requests
    assert req.url.params["function"] == "EARNINGS_CALENDAR" and req.url.params["horizon"] == "3month"
    assert KEY.encode() not in _raw_bytes() and KEY not in (check["note"] or "")
    rows = fx.q("SELECT cik, ticker, report_date, report_time, fiscal_period_end, eps_estimate, confirmed, source "
                "FROM earnings_event ORDER BY report_date")
    assert rows == [("789019", "MSFT", date(2026, 10, 29), "post-market", date(2026, 9, 30), 3.85, False,
                     earnings.NAME),
                    ("46619", "HEI", date(2026, 12, 18), None, date(2026, 10, 31), 1.10, False, earnings.NAME)]
    assert fx.q("SELECT ticker, earnings_date, report_session, street_mean_est, session_date FROM earnings_calendar "
                "ORDER BY earnings_date") == [("MSFT", date(2026, 10, 29), "afterhours", 3.85, date(2026, 10, 28)),
                                              ("HEI", date(2026, 12, 18), None, 1.10, date(2026, 10, 28))]
    assert "2 of 4 Tier A companies have a coming report date" in check["note"]    # MSFT and HEI; HEI-A is HEI


def test_a_report_that_left_the_calendar_keeps_its_last_snapshot(av):
    av.add("av.test/query", CSV_DAY1)
    fx.run(earnings.NAME, "2026-10-28")
    av.routes.clear()
    av.add("av.test/query", CSV_DAY2)
    fx.run(earnings.NAME, "2026-10-30")
    assert fx.q("SELECT ticker, report_date, eps_estimate, source_as_of FROM earnings_event ORDER BY report_date") == [
        ("MSFT", date(2026, 10, 29), 3.85, date(2026, 10, 28)),
        ("HEI", date(2026, 12, 17), 1.12, date(2026, 10, 30))]          # moved a day: the newer snapshot wins


@pytest.mark.parametrize("body", ['{"Information": "We have detected your API key as %s ..."}' % KEY,
                                  '{"Note": "Thank you for using Alpha Vantage!"}', "symbol,name\r\nMSFT,x\r\n"])
def test_a_rate_limit_or_error_answer_is_a_failed_run_that_keeps_nothing(av, body):
    av.add("av.test/query", body)
    check = fx.run(earnings.NAME, "2026-10-28")
    assert check["result"] == "failed" and "alphavantage earnings calendar" in check["note"]
    assert KEY not in check["note"] and not list(base.raw_root().rglob("*.csv.gz"))


def test_a_403_from_the_calendar_points_at_the_key_not_at_the_sec_contact(av):
    """``common.get`` serves both the SEC (keyless, a 403 means the User-Agent) and Alpha Vantage (the key is in the
    query), so the note a 403 carries has to follow the request, not the module."""
    av.add("EARNINGS_CALENDAR", b"forbidden", status=403)
    check = fx.run(earnings.NAME, "2026-10-28")
    assert check["result"] == "failed" and "check the key" in check["note"], check["note"]
    assert "RC_HTTP_CONTACT" not in check["note"] and KEY not in check["note"]


def test_an_8k_with_item_202_is_a_confirmed_report(av, monkeypatch):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    other_8k = ("0001193125-25-260001", "8-K", "2025-11-05", "2025-11-03", "msft-8k-2.htm", "5.02,9.01")
    fx.filing_routes(av, {"789019": fx.submissions(789019, [other_8k] + fx.MSFT_FILINGS)},
                     {"msft-10k.htm": fx.ten_k(), "msft-def14a.htm": fx.proxy()})
    fx.run(filings.NAME, mode="backfill", to="2026-10-28")
    assert fx.q("SELECT cik, report_date, confirmed, accession, source FROM earnings_event") == [
        ("789019", date(2025, 10, 29), True, fx.EIGHT_K, filings.NAME)]
    assert fx.q("SELECT ticker, earnings_date, confirmed FROM earnings_calendar") == [
        ("MSFT", date(2025, 10, 29), True)]
