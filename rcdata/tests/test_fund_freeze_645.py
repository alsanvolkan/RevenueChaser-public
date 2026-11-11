"""The fundamentals freeze (#645, avo 10 Nov 2026; fundamentals serving version 3): the three SEC fields the Financials
engines read (G1), the filing list's company header on ``fundamental_company`` (G2, SEC SIC = D3), and the
first-reported value beside the served one (#277 A2)."""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.fundamentals import common, edgar_codes, facts, filings

from . import fund_fixture as fx

K24, K25 = "0000000001-25-000001", "0000000001-26-000001"


def _facts(gaap: dict) -> dict:
    return {"cik": 1, "entityName": "X", "facts": {"us-gaap": gaap}}


def _flow(*facts_):
    return {"units": {"USD": list(facts_)}}


def _by_key(rows):
    return {(r["field"], r["period_type"], r["period_end"]): r for r in rows}


# ── G1: tax, pre-tax income, interest ─────────────────────────────────────────────────────────────────────────

def test_the_three_sec_fields_are_served_as_rows_and_a_tax_benefit_stays_negative():
    fy = ("2025-12-31", K25, "2026-02-01", "10-K", "FY", 2025, "2025-01-01")
    rows = _by_key(facts.fact_rows(_facts({
        "Revenues": _flow(fx._fact(1_000, *fy)),
        "IncomeTaxExpenseBenefit": _flow(fx._fact(-30, *fy)),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
            _flow(fx._fact(200, *fy)),
        "InterestExpenseNonoperating": _flow(fx._fact(15, *fy)),
    }), "1")[0])
    end = date(2025, 12, 31)
    assert rows[("income_tax_expense", "annual", end)]["value"] == -30
    assert rows[("pretax_income", "annual", end)]["value"] == 200
    assert rows[("interest_expense", "annual", end)]["value"] == 15
    assert {"income_tax_expense", "pretax_income", "interest_expense"} <= set(facts.FIELDS)


def test_interest_expense_takes_its_tags_in_priority_order_never_merged():
    fy = ("2025-12-31", K25, "2026-02-01", "10-K", "FY", 2025, "2025-01-01")
    rows = _by_key(facts.fact_rows(_facts({
        "Revenues": _flow(fx._fact(1_000, *fy)),
        "InterestExpense": _flow(fx._fact(20, *fy)),
        "InterestExpenseDebt": _flow(fx._fact(18, *fy)),
    }), "1")[0])
    assert rows[("interest_expense", "annual", date(2025, 12, 31))]["value"] == 20


def test_the_three_fields_never_make_a_period_of_their_own():
    assert {"income_tax_expense", "pretax_income", "interest_expense"} <= sec_edgar._NEVER_A_RECORD
    assert not {"income_tax_expense", "pretax_income", "interest_expense"} & sec_edgar._NON_NEGATIVE_FLOWS


# ── #277 A2: the first-reported value ─────────────────────────────────────────────────────────────────────────

def test_a_restated_value_keeps_the_value_first_reported_beside_it():
    # FY2024 revenue first reported as 1,000 in the FY2024 10-K, restated to 950 in the FY2025 10-K.
    rows = _by_key(facts.fact_rows(_facts({"Revenues": _flow(
        fx._fact(1_000, "2024-12-31", K24, "2025-02-01", "10-K", "FY", 2024, "2024-01-01"),
        fx._fact(950, "2024-12-31", K25, "2026-02-01", "10-K", "FY", 2025, "2024-01-01"),
        fx._fact(1_100, "2025-12-31", K25, "2026-02-01", "10-K", "FY", 2025, "2025-01-01"),
    )}), "1")[0])
    restated = rows[("revenue", "annual", date(2024, 12, 31))]
    assert (restated["value"], restated["accn"]) == (950, K25)
    assert (restated["first_value"], restated["first_accn"], restated["first_filed"]) == (1_000, K24, date(2025, 2, 1))
    newest = rows[("revenue", "annual", date(2025, 12, 31))]
    assert (newest["first_value"], newest["first_accn"]) == (1_100, K25)          # never restated: the same value


def test_a_derived_value_has_no_first_reported_value():
    rows = _by_key(facts.fact_rows(fx.msft_facts(), "789019")[0])
    debt = rows[("total_debt", "annual", date(2025, 6, 30))]
    assert debt["derived"] and debt.get("first_value") is None
    assert rows[("revenue", "annual", date(2025, 6, 30))]["first_value"] == 281_000


# ── G2: the company header ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code, country, state", [
    ("WA", "US", "WA"), ("DC", "US", "DC"), ("X1", "US", None), ("A6", "CA", "A6"), ("Z4", "CA", None),
    ("X0", "GB", None), ("E9", "KY", None), ("L2", "IE", None), ("K3", "HK", None), ("PR", "PR", None),
    ("XX", None, None), ("", None, None), (None, None, None), ("ZZ", None, None)])
def test_edgar_codes_become_iso_countries(code, country, state):
    assert (edgar_codes.country(code), edgar_codes.state(code)) == (country, state)


def test_every_edgar_country_code_maps_to_a_distinct_iso_code():
    iso = list(edgar_codes._OTHER.values())
    assert len(edgar_codes.US_STATES) == 51 and len(edgar_codes.CA_PROVINCES) == 11
    assert all(len(v) == 2 and v.isalpha() and v.isupper() for v in iso)
    assert len(set(iso) - {"CA"}) == len(iso) - 1                       # Z4 (Canada, federal) is the one repeat


def _arm_body():
    body = fx.submissions(1973239, [], "ARM HOLDINGS PLC")
    body.update({"sic": "3674", "sicDescription": "Semiconductors & Related Devices",
                 "stateOfIncorporation": "X0", "stateOfIncorporationDescription": "United Kingdom",
                 "addresses": {"business": {"stateOrCountry": "X0", "stateOrCountryDescription": "United Kingdom"}}})
    return body


def test_the_header_of_a_filing_list():
    assert filings.header("1973239", _arm_body()) == {
        "cik": "1973239", "sic": "3674", "sic_description": "Semiconductors & Related Devices",
        "state_of_incorporation": "X0", "state_of_incorporation_description": "United Kingdom",
        "business_state": None, "business_country": "GB"}
    assert filings.header("1", {"filings": {"recent": {}}}) == {
        "cik": "1", "sic": None, "sic_description": None, "state_of_incorporation": None,
        "state_of_incorporation_description": None, "business_state": None, "business_country": None}


@pytest.fixture
def sec(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    fx.spine()
    return http_mock


def test_the_header_reaches_fundamental_company(sec):
    body = fx.submissions(789019, fx.MSFT_FILINGS, "MICROSOFT CORP")
    body.update({"sic": "7372", "sicDescription": "Services-Prepackaged Software", "stateOfIncorporation": "WA",
                 "stateOfIncorporationDescription": "WA", "addresses": {"business": {"stateOrCountry": "WA"}}})
    fx.filing_routes(sec, {"789019": body}, {"msft-10k.htm": fx.ten_k(), "msft-def14a.htm": fx.proxy()})
    check = fx.run(filings.NAME, mode="backfill", to="2026-10-28")
    assert check["result"] == "changed", check["note"]
    assert fx.q("SELECT sic, sic_description, state_of_incorporation, business_state, business_country "
                "FROM fundamental_company WHERE ticker = 'MSFT'") == [
        ("7372", "Services-Prepackaged Software", "WA", "WA", "US")]
    assert fx.q("SELECT business_country FROM fundamental_company WHERE ticker = 'HEI'") == [(None,)]  # never read


# ── A Parquet file written before the freeze still stages ─────────────────────────────────────────────────────

def test_a_parquet_file_without_the_new_columns_stages_them_as_null(tmp_path):
    old = [c for c in facts.FACT if not c[0].startswith("first_")]
    con = duckdb.connect()
    base.rows_table(con, "rows", old, [{"cik": "1", "field": "revenue", "period_type": "annual",
                                         "period_end": date(2025, 12, 31), "value": 1.0}])
    base.write_parquet(con, "SELECT * FROM rows", tmp_path / "cik=1" / "part-0.parquet")
    for ciks in (None, ["1"]):
        common.stage_glob(con, "stg", facts.FACT, tmp_path, "cik=*/part-0.parquet", ciks=ciks)
        assert con.execute("SELECT value, first_value, first_accn FROM stg").fetchall() == [(1.0, None, None)]
