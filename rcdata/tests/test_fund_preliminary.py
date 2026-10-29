"""``uw/financials`` (build plan PR 12): UW's numbers of a quarter a company has reported but not yet filed, served as
``preliminary`` rows (no accession, no filed date) only while the SEC has no value for that quarter, and only with
``RC_UW_PRELIMINARY=1`` (the decision to serve them is open)."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar, unusual_whales
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build.fundamentals import companyfacts, earnings, uw_financials

from . import fund_fixture as fx

AV_CSV = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\r\n"
          "MSFT,Microsoft Corp,2026-10-29,2026-09-30,3.85,USD,post-market\r\n")


def _uw_body() -> dict:
    """MSFT's ``/stock/MSFT/financials`` answer: the reported quarter to 30 Sep 2026, a quarter the SEC already
    has, an annual row on the same date as a quarter (Earnings Radar review S3) and a row in another currency."""
    def row(end, report_type, **values):
        return {"ticker": "MSFT", "fiscal_date_ending": end, "report_type": report_type, "reported_currency": "USD",
                "inserted_at": "2026-10-30T01:00:00Z", "updated_at": "2026-10-30T01:00:00Z",
                **{k: str(v) for k, v in values.items()}}
    income = [row("2026-09-30", "quarterly", total_revenue=80_500, net_income=28_100, operating_income="None"),
              row("2025-09-30", "quarterly", total_revenue=77_100, net_income=27_100),
              row("2026-06-30", "annual", total_revenue=290_000, net_income=104_000),
              {**row("2026-06-30", "quarterly", total_revenue=1), "reported_currency": "EUR"}]
    balance = [row("2026-09-30", "quarterly", long_term_debt_noncurrent=38_100, current_long_term_debt=2_100,
                   short_term_debt=2_400)]
    return {"data": {"income_statements": income, "balance_sheets": balance, "cash_flows": [], "earnings": []}}


@pytest.fixture
def uw(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "uw-test-key")
    monkeypatch.setenv("RC_ALPHAVANTAGE_BASE", "https://av.test")
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "av-test-key")
    monkeypatch.setenv("RC_UW_PRELIMINARY", "1")
    fx.spine()
    fx.sec_routes(http_mock, {"789019": fx.msft_facts()})
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")          # the SEC has MSFT to 30 Sep 2025
    http_mock.add("av.test/query", AV_CSV)
    fx.run(earnings.NAME, "2026-10-28")                                   # MSFT reports on 29 Oct
    http_mock.add("uw.test/api/stock/MSFT/financials", _uw_body())
    return http_mock


def _preliminary() -> list[tuple]:
    return fx.q("SELECT field, period_type, period_end, value, accn, filed, source FROM fundamental_fact "
                "WHERE preliminary ORDER BY field")


def test_a_reported_quarter_is_preliminary_until_the_sec_value_replaces_it(uw):
    check = fx.run(uw_financials.NAME, "2026-10-30")
    assert check["result"] == "changed", check["note"]
    assert [str(r.url) for r in uw.calls("uw.test")] == ["https://uw.test/api/stock/MSFT/financials"]  # HEI: no report
    end = date(2026, 9, 30)
    # The reported quarter only: not the quarter the SEC has, not the annual row, not the other currency; the debt
    # terms without a sum (UW's short-term debt may hold the current portion already).
    assert _preliminary() == [("lt_debt", "quarterly", end, 38_100.0, None, None, uw_financials.NAME),
                              ("lt_debt_current", "quarterly", end, 2_100.0, None, None, uw_financials.NAME),
                              ("net_income", "quarterly", end, 28_100.0, None, None, uw_financials.NAME),
                              ("revenue", "quarterly", end, 80_500.0, None, None, uw_financials.NAME)]
    assert fx.q("SELECT revenue, preliminary FROM fundamental_quarter WHERE cik = '789019' AND period_end = ?",
                end) == [(80_500.0, True)]
    assert "1 preliminary quarter(s) of 1 companies" in check["note"]
    # The old edgar_financials shape is the SEC's only.
    assert "2026-09-30" not in fx.q("SELECT quarterly FROM edgar_financials WHERE ticker = 'MSFT'")[0][0]

    # The 10-Q arrives: the SEC value replaces the preliminary one.
    uw.routes.clear()
    fx.sec_routes(uw, {"789019": fx.msft_facts(with_new_quarter=True)},
                  {"20261027": fx.daily_index([("789019", "MICROSOFT CORP", "10-Q", "20261027", fx.Q27)])})
    fx.run(companyfacts.NAME, "2026-10-30")
    assert _preliminary() == []
    assert fx.q("SELECT value, accn, preliminary FROM fundamental_fact WHERE cik = '789019' AND field = 'revenue' "
                "AND period_type = 'quarterly' AND period_end = ?", end) == [(80_000.0, fx.Q27, False)]


def test_off_unless_turned_on(uw, monkeypatch):
    fx.run(uw_financials.NAME, "2026-10-30")
    assert _preliminary()
    monkeypatch.delenv("RC_UW_PRELIMINARY")
    # 4 of 26 rows go here, so the swap's shrink guard needs the request's word; in a real file it is far under 1%.
    check = fx.run(uw_financials.NAME, "2026-10-31", allow_shrink=True)
    assert "off" in check["note"] and len(uw.calls("uw.test")) == 1
    assert _preliminary() == []                                  # the stored rows are not served either


def test_no_key_asks_nothing(uw, monkeypatch):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY")
    check = fx.run(uw_financials.NAME, "2026-10-30")
    assert not uw.calls("uw.test") and "no UNUSUAL_WHALES_API_KEY" in check["note"]
    assert "missing_key" in [i["kind"] for i in ops.open_issues(uw_financials.NAME)[1]]


def test_a_quarter_after_the_window_is_not_asked(uw):
    fx.run(uw_financials.NAME, "2026-11-20")                     # 22 days after the report: not a candidate
    assert not uw.calls("uw.test")
