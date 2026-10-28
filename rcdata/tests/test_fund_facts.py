"""``sec/companyfacts`` and ``fundamental_fact`` (build plan PR 12): every fact with its accession, filed date and
period type, total debt from three terms, the re-read on the filing day (#43 C14), and the 120-day check."""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build.fundamentals import companyfacts, facts, tables

from . import fund_fixture as fx


@pytest.fixture
def sec(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    fx.spine()
    return http_mock


def _by_key(rows):
    return {(r["field"], r["period_type"], r["period_end"]): r for r in rows}


# ── The rows of one company ───────────────────────────────────────────────────────────────────────────────────

def test_every_fact_has_an_accession_a_filed_date_a_form_and_a_period_type():
    rows, company, counts = facts.fact_rows(fx.msft_facts(), "789019")
    assert rows and counts == {}
    assert all(r["accn"] and r["filed"] and r["form"] in ("10-K", "10-Q") for r in rows)
    assert all(r["period_type"] in ("annual", "quarterly") for r in rows)
    assert {r["period_type"] for r in rows} == {"annual", "quarterly"}
    revenue = _by_key(rows)[("revenue", "annual", date(2025, 6, 30))]
    assert (revenue["accn"], revenue["filed"], revenue["form"], revenue["fiscal_year"], revenue["period_start"]) == (
        fx.K25, date(2025, 7, 30), "10-K", 2025, date(2024, 7, 1))
    assert company == {"cik": "789019", "entity_name": "MICROSOFT CORPORATION", "is_us": True,
                       "non_usd_units": None, "us_gaap": True}


def test_total_debt_sums_long_term_debt_its_current_portion_and_short_term_borrowings():
    rows = _by_key(facts.fact_rows(fx.msft_facts(), "789019")[0])
    annual = rows[("total_debt", "annual", date(2025, 6, 30))]
    assert annual["value"] == 40_000 + 3_000 + 500 and annual["derived"] and annual["accn"] == fx.K25
    quarter = rows[("total_debt", "quarterly", date(2025, 9, 30))]
    assert quarter["value"] == 39_000 + 2_500 + 400
    assert (quarter["accn"], quarter["filed"], quarter["form"]) == (fx.Q26, date(2025, 10, 29), "10-Q")


def test_values_the_extraction_keeps_no_provenance_for_get_it_here():
    rows = _by_key(facts.fact_rows(fx.msft_facts(), "789019")[0])
    gross = rows[("gross_profit", "annual", date(2025, 6, 30))]
    assert gross["value"] == 281_000 - 87_000 and gross["derived"] and gross["accn"] == fx.K25
    sti = rows[("short_term_investments", "quarterly", date(2025, 9, 30))]
    assert sti["value"] == 60_000 and sti["accn"] == fx.Q26 and sti["filed"] == date(2025, 10, 29)


def test_eps_is_read_per_period():
    rows = _by_key(facts.fact_rows(fx.msft_facts(), "789019")[0])
    assert rows[("eps_diluted", "annual", date(2025, 6, 30))]["value"] == 13.64
    assert rows[("eps_diluted", "quarterly", date(2025, 9, 30))]["unit"] == "USD/shares"


def test_an_ifrs_filer_has_no_rows_and_says_so():
    rows, company, _ = facts.fact_rows(fx.tm_facts(), "1094517")
    assert rows == [] and company["us_gaap"] is False and company["is_us"] is True


def test_the_served_table_refuses_a_fact_without_its_accession_or_period_type():
    con = duckdb.connect()
    tables.create(con, "fundamental_fact")

    def insert(period_type="'annual'", accn="'a'", filed="DATE '2025-07-30'", preliminary="false"):
        con.execute(f"INSERT INTO fundamental_fact VALUES ('1', 'revenue', {period_type}, 2025, NULL, "
                    f"DATE '2025-06-30', 1.0, 'USD', {accn}, {filed}, '10-K', false, {preliminary}, "
                    "'sec/companyfacts', NULL, NULL, NULL)")
    for bad in ({"accn": "NULL"}, {"filed": "NULL"}, {"period_type": "'ttm'"}, {"period_type": "NULL"}):
        with pytest.raises(duckdb.ConstraintException):
            insert(**bad)
    insert(accn="NULL", filed="NULL", preliminary="true")                 # a preliminary row has neither


# ── Through the builder ───────────────────────────────────────────────────────────────────────────────────────

def _answers(sec, msft=None):
    fx.sec_routes(sec, {"789019": msft or fx.msft_facts(), "46619": fx.hei_facts(), "1094517": fx.tm_facts()})


def test_the_backfill_reads_every_tier_a_company_once_and_serves_its_facts(sec):
    _answers(sec)
    check = fx.run(companyfacts.NAME, mode="backfill", to="2026-10-28")
    assert check["result"] == "changed", check["note"]
    assert len(sec.calls("companyfacts")) == 4                         # MSFT, ORCL (404), HEI, TM; HEI-A shares HEI's
    assert all(r.headers["user-agent"] for r in sec.calls("companyfacts"))
    assert fx.q("SELECT count(*) FROM fundamental_fact WHERE accn IS NULL OR filed IS NULL") == [(0,)]
    assert fx.q("SELECT DISTINCT period_type FROM fundamental_fact ORDER BY 1") == [("annual",), ("quarterly",)]
    assert fx.q("SELECT value FROM fundamental_fact WHERE cik = '789019' AND field = 'total_debt' "
                "AND period_type = 'annual'") == [(43_500.0,)]
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-29")
    assert len(sec.calls("companyfacts")) == 4                         # resumable: nobody is read twice


def test_requests_to_the_sec_are_spaced_under_five_a_second(sec):
    _answers(sec)
    slept = []
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-28", sleep=slept.append, clock=lambda: 100.0)
    assert len(slept) == len(sec.requests) - 1 and set(slept) == {companyfacts.common.SEC_INTERVAL_S}
    assert 1 / companyfacts.common.SEC_INTERVAL_S < 5


def test_a_statement_filed_yesterday_is_read_again_today(sec):
    _answers(sec)
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")
    sec.routes.clear()
    _answers(sec, msft=fx.msft_facts(with_new_quarter=True))
    fx.sec_routes(sec, {}, {"20261027": fx.daily_index([("789019", "MICROSOFT CORP", "10-Q", "20261027", fx.Q27),
                                                         ("72971", "WELLS FARGO", "10-Q", "20261027", "x-1")])})
    before = len(sec.calls("companyfacts"))
    check = fx.run(companyfacts.NAME, "2026-10-28")
    assert check["result"] == "changed", check["note"]
    assert [str(r.url) for r in sec.calls("companyfacts")[before:]] == [
        "https://data.sec.test/api/xbrl/companyfacts/CIK0000789019.json"]   # only the company that filed
    assert fx.q("SELECT value, accn, filed, form FROM fundamental_fact WHERE cik = '789019' AND field = 'revenue' "
                "AND period_type = 'quarterly' AND period_end = DATE '2026-09-30'") == [
        (80_000.0, fx.Q27, date(2026, 10, 27), "10-Q")]
    days = [str(r.url).rsplit("master.", 1)[1][:8] for r in sec.calls("daily-index")]
    assert days == ["20261021", "20261022", "20261023", "20261026", "20261027", "20261028"]   # weekdays only
    fx.run(companyfacts.NAME, "2026-10-29")
    again = [str(r.url).rsplit("master.", 1)[1][:8] for r in sec.calls("daily-index")][len(days):]
    assert again == ["20261028", "20261029"]          # a past holiday's 404 is not asked again; the last two are


def test_an_accession_not_in_the_xbrl_api_yet_is_asked_again_the_next_day(sec):
    _answers(sec)
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")
    fx.sec_routes(sec, {}, {"20261027": fx.daily_index([("789019", "MICROSOFT CORP", "10-Q", "20261027", fx.Q27)])})
    check = fx.run(companyfacts.NAME, "2026-10-28")
    assert "not in the XBRL API yet" in check["note"] and f"789019:{fx.Q27}" in check["note"]
    sec.routes.clear()
    _answers(sec, msft=fx.msft_facts(with_new_quarter=True))
    fx.sec_routes(sec, {}, {"20261027": fx.daily_index([("789019", "MICROSOFT CORP", "10-Q", "20261027", fx.Q27)])})
    fx.run(companyfacts.NAME, "2026-10-29")
    assert fx.q("SELECT accn FROM fundamental_fact WHERE cik = '789019' AND period_end = DATE '2026-09-30' "
                "AND field = 'revenue'") == [(fx.Q27,)]


def test_the_statement_older_than_120_days_check(sec):
    _answers(sec, msft=fx.msft_facts(with_new_quarter=True))
    check = fx.run(companyfacts.NAME, mode="backfill", to="2026-10-28")
    assert check["coverage"] == 0.5                                     # MSFT fresh, HEI not; TM and ORCL apart
    assert "statements <= 120 days: 1 of 2 quarterly filers" in check["note"]
    _, issues = ops.open_issues(tables.NAME, limit=50)
    found = {(i["kind"], i["key"]) for i in issues}
    assert ("statement_older_than_120_days", "46619") in found and ("no_us_gaap_facts", "1094517") in found
    assert ("no_statement", "1341439") in found
    assert not [k for k in found if k[1] == "789019"]


def test_the_views_keep_the_old_shapes(sec):
    import json
    _answers(sec)
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-28")
    (annual, quarterly, meta, fetched_at), = fx.q("SELECT annual, quarterly, meta, fetched_at FROM edgar_financials "
                                                  "WHERE ticker = 'MSFT'")
    annual, quarterly, meta = json.loads(annual), json.loads(quarterly), json.loads(meta)
    year = annual[-1]
    assert year["fiscal_year"] == "2025" and year["revenue"] == 281_000 and year["total_debt"] == 43_500
    assert year["filed"]["revenue"] == "2025-07-30" and year["accn"]["net_income"] == fx.K25
    assert year["period_ends"]["revenue"] == "2025-06-30"
    assert quarterly[-1]["period"] == "2025-09-30" and quarterly[-1]["revenue"] == 77_000
    assert meta["extract_version"] == sec_edgar.EXTRACT_VERSION and meta["available"] is True and fetched_at
    assert [r[0] for r in fx.q("SELECT ticker FROM edgar_financials WHERE ticker LIKE 'HEI%' ORDER BY 1")] == [
        "HEI", "HEI-A"]
    assert fx.q("SELECT revenue, total_debt, filed FROM fundamental_quarter WHERE cik = '789019' "
                "AND period_end = DATE '2025-09-30'") == [(77_000.0, 41_900.0, date(2025, 10, 29))]
    assert fx.q("SELECT newest_filed, entity_name, us_gaap FROM fundamental_company WHERE ticker = 'MSFT'") == [
        (date(2025, 10, 29), "MICROSOFT CORPORATION", True)]
