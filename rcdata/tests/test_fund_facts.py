"""``sec/companyfacts`` and ``fundamental_fact`` (build plan PR 12): every fact with its accession, filed date and
period type, total debt from the filer's debt lines, the re-read on the filing day (#43 C14), and the 120-day check."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build.fundamentals import companyfacts, facts, sec_index, tables

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


def _balance_sheet(**tags):
    """One FY2025 10-K whose balance sheet carries ``tags`` (USD), and a revenue line so the year is a record."""
    accn, filed, end = "0000000001-26-000001", "2026-02-01", "2025-12-31"
    gaap = {"Revenues": {"units": {"USD": [fx._fact(1_000, end, accn, filed, "10-K", "FY", 2025, "2025-01-01")]}}}
    for tag, val in tags.items():
        gaap[tag] = {"units": {"USD": [fx._fact(val, end, accn, filed, "10-K", "FY", 2025)]}}
    return {"cik": 1, "entityName": "X", "facts": {"us-gaap": gaap}}


@pytest.mark.parametrize("tags, total", [
    # The balance sheets of the real-data check of 2026-10-28 (PR 249), values in USD millions or thousands.
    ({"LongTermDebtNoncurrent": 78_328, "LongTermDebtCurrent": 12_350, "CommercialPaper": 7_979}, 98_657),  # AAPL
    ({"LongTermNotesPayable": 122_342, "DebtCurrent": 7_199}, 129_541),                                       # ORCL
    ({"LongTermDebtAndCapitalLeaseObligations": 2_164_587, "LongTermDebtCurrent": 3_358,
      "LineOfCredit": 960_000}, 2_167_945),                                    # HEI: the revolver is inside the total
    ({"LongTermDebtNoncurrent": 7_469, "LongTermDebtCurrent": 999, "DebtCurrent": 999}, 8_468),            # NVDA
    ({"LongTermDebt": 400_000, "LongTermDebtCurrent": 50_000, "ShortTermBorrowings": 64_776}, 464_776),  # one line
    ({"LongTermDebtNoncurrent": 100, "ShortTermBorrowings": 30, "CommercialPaper": 20}, 130),   # paper inside STB
    ({"LongTermDebtNoncurrent": 100, "DebtCurrent": 45, "LongTermDebtCurrent": 10, "ShortTermBorrowings": 30}, 145),
    ({"LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities": 460_523,
      "ShortTermBorrowings": 72_430}, 532_953),                                                                   # JPM
    ({"ShortTermBorrowings": 64_776}, None),           # JPM before: a current line alone is not the whole, so unknown
    ({"NotesPayableCurrent": 7_625, "LongTermDebtCurrent": 500}, None),     # ORCL's 10-Q: no long-term line tagged
])
def test_total_debt_reads_the_debt_lines_the_filer_uses(tags, total):
    rows = _by_key(facts.fact_rows(_balance_sheet(**tags), "1")[0])
    row = rows.get(("total_debt", "annual", date(2025, 12, 31)))
    assert (row and row["value"]) == total


def test_commercial_paper_filed_later_still_gives_a_whole_annual_row():
    # A later filing (a 10-K/A) restated the paper line only; the total takes its provenance and stays annual.
    sheet = _balance_sheet(LongTermDebtNoncurrent=78_328, LongTermDebtCurrent=12_350)
    sheet["facts"]["us-gaap"]["CommercialPaper"] = {"units": {"USD": [
        fx._fact(7_979, "2025-12-31", "0000000001-26-000009", "2026-03-01", "10-K/A", "FY", 2025)]}}
    row = _by_key(facts.fact_rows(sheet, "1")[0])[("total_debt", "annual", date(2025, 12, 31))]
    assert (row["value"], row["period_type"], row["fiscal_year"], row["unit"]) == (98_657, "annual", 2025, "USD")
    assert (row["accn"], row["filed"], row["form"], row["derived"]) == (
        "0000000001-26-000009", date(2026, 3, 1), "10-K/A", True)


def test_total_debt_never_adds_lines_of_two_balance_sheet_dates():
    # A filer that moved its year end: two FY balance sheets in calendar 2025 (the Opus review's M1 probe).
    def fact(val, end, accn, filed, form="10-K", start=None):
        return {"units": {"USD": [fx._fact(val, end, accn, filed, form, "FY", 2025, start)]}}
    sheet = {"cik": 1, "entityName": "X", "facts": {"us-gaap": {
        "Revenues": fact(5_000, "2025-12-31", "a-2", "2026-02-01", "10-KT", "2025-07-01"),
        "LongTermDebtNoncurrent": fact(1_000, "2025-06-30", "a-1", "2025-08-01"),
        "DebtCurrent": fact(300, "2025-12-31", "a-2", "2026-02-01", "10-KT"),
        "Assets": fact(9_000, "2025-12-31", "a-2", "2026-02-01", "10-KT")}}}
    rows = facts.fact_rows(sheet, "1")[0]
    debt = [r for r in rows if r["field"] == "total_debt" and r["period_type"] == "annual"]
    assert [(r["period_end"], r["value"]) for r in debt if r["period_end"].year == 2025] == []   # unknown, not 1,300
    ends = {r["field"]: r["period_end"] for r in rows if r["period_type"] == "annual" and r["fiscal_year"] == 2025}
    assert ends["lt_debt"] != ends["total_assets"]                       # the fixture holds the two balance dates


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


def test_a_fallback_provenance_is_the_fact_of_the_same_span():
    # The Opus review's N5: an annual fact, a quarterly fact and an instant fact can end on one day (a fiscal year's
    # last day). The provenance of a value the extraction keeps none for is the fact of its own span; a span the
    # facts do not hold falls back to the newest fact ending that day, as before.
    def f(accn, filed, start=None):
        return fx._fact(1.0, "2025-06-30", accn, filed, "10-K", "FY", 2025, start)
    index = facts._Index({"facts": {"us-gaap": {"X": {"units": {"USD": [
        f("q-1", "2025-07-30", "2025-04-01"), f("i-1", "2025-07-31"), f("k-1", "2025-08-01", "2024-07-01")]}}}}})
    assert index.newest(["X"], "2025-06-30", "2025-04-01") == ("2025-07-30", "q-1")     # the quarter
    assert index.newest(["X"], "2025-06-30") == ("2025-07-31", "i-1")                   # an instant
    assert index.newest(["X"], "2025-06-30", "2025-01-01") == ("2025-08-01", "k-1")     # no such span: the newest


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


def test_a_restated_value_replaces_the_one_it_restates():
    # The Opus review's S2: the served table keeps, per company, field, period type and period end, the row of the
    # newest filing (then the larger accession on one day); the older value is the one the filer corrected.
    con = duckdb.connect()
    tables._ensure_staging(con)
    stage = companyfacts.STAGING["facts"]
    for value, accn, filed in ((100.0, "b-1", "2026-04-30"), (105.0, "a-2", "2026-07-30"), (110.0, "a-3", "2026-07-30"),
                               (90.0, "c-0", "2026-02-01")):           # accessions do not sort with the filed day
        con.execute(f"INSERT INTO {stage} (cik, field, period_type, fiscal_year, period_end, value, unit, accn, "
                    f"filed, form, derived, preliminary, source) VALUES ('1', 'revenue', 'quarterly', 2026, "
                    f"DATE '2026-03-31', {value}, 'USD', '{accn}', DATE '{filed}', '10-Q', false, false, "
                    "'sec/companyfacts')")
    tables._facts(con)
    assert con.execute("SELECT value, accn FROM fundamental_fact").fetchall() == [(110.0, "a-3")]


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


def test_a_newer_filing_that_restates_an_old_quarter_is_served_with_its_accession(sec):
    msft = fx.msft_facts(with_new_quarter=True)
    msft["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(   # the 2026 10-Q restates the year-ago quarter
        fx._fact(77_500, "2025-09-30", fx.Q27, "2026-10-27", "10-Q", "Q1", 2027, "2025-07-01"))
    _answers(sec, msft=msft)
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-28")
    assert fx.q("SELECT value, accn, filed FROM fundamental_fact WHERE cik = '789019' AND field = 'revenue' "
                "AND period_type = 'quarterly' AND period_end = DATE '2025-09-30'") == [
        (77_500.0, fx.Q27, date(2026, 10, 27))]


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


def test_todays_index_not_published_yet_answers_403_and_the_run_still_reads_yesterdays(sec):
    # Seen on the server on 29 Oct 2026 09:07 UTC: master.20261029.idx answered 403 (not 404) while the 27th and
    # 28th answered 200. Today's and yesterday's index may not be out yet; neither may fail the whole run.
    _answers(sec)
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-23")
    sec.routes.clear()
    _answers(sec, msft=fx.msft_facts(with_new_quarter=True))
    sec.add("master.20261027.idx", b"Forbidden", status=403)              # yesterday
    sec.add("master.20261028.idx", b"Forbidden", status=403)              # today
    fx.sec_routes(sec, {}, {"20261026": fx.daily_index([("789019", "MICROSOFT CORP", "10-Q", "20261026", fx.Q27)])})
    check = fx.run(companyfacts.NAME, "2026-10-28")
    assert check["result"] == "changed", check["note"]
    assert fx.q("SELECT accn FROM fundamental_fact WHERE cik = '789019' AND field = 'revenue' "
                "AND period_type = 'quarterly' AND period_end = DATE '2026-09-30'") == [(fx.Q27,)]
    for day in (date(2026, 10, 27), date(2026, 10, 28)):                   # asked again by the next run
        assert not sec_index.absent_path(day).exists() and not sec_index.raw_path(day).exists()


def test_a_403_for_an_older_index_still_fails_the_run(sec):
    # An index older than yesterday is published, so a 403 there is a real block, not "not out yet".
    _answers(sec)
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")
    sec.add("master.20261026.idx", b"Forbidden", status=403)              # two days before the run
    check = fx.run(companyfacts.NAME, "2026-10-28")
    assert check["result"] == "failed" and "HTTP 403" in check["note"], check["note"]


@pytest.mark.parametrize("last, days, note", [
    ("2026-10-27", 7, None),                                              # yesterday's run: the 7-day floor
    ("2026-09-01", 56, "daily index read for 56 days (last whole scan 2026-09-01)"),
    ("2026-03-01", 120, "daily index read for 120 days only (last whole scan 2026-03-01): a filing before "
                        "2026-06-30 is not read"),
])
def test_the_index_is_read_from_the_day_after_the_last_whole_scan(last, days, note):
    ctx = SimpleNamespace(notes=[], run_id="test")
    sec_index.mark_scanned(ctx, "sec/scan_test", date.fromisoformat(last))
    assert sec_index.lookback(ctx, "sec/scan_test", date(2026, 10, 28), lambda: date(2020, 1, 1)) == days
    assert ctx.notes == ([note] if note else [])


def test_without_a_scan_mark_the_index_is_read_from_the_oldest_newest_read():
    ctx = SimpleNamespace(notes=[], run_id="test")
    assert sec_index.lookback(ctx, "sec/no_mark", date(2026, 10, 28), lambda: date(2026, 10, 1)) == 26
    assert sec_index.lookback(ctx, "sec/no_mark", date(2026, 10, 28), lambda: None) == sec_index.LOOKBACK_DAYS


def test_a_filing_made_during_an_outage_longer_than_the_lookback_is_still_read(sec):
    # The Opus review's S1: the facts were read on 1 Sep, the builder stopped, MSFT filed on 13 Oct, and the next
    # run is 28 Oct (15 days later, past the 7-day lookback). That run still reads the 13 Oct index.
    _answers(sec)
    fx.run(companyfacts.NAME, mode="backfill", to="2026-09-01")
    sec.routes.clear()
    _answers(sec, msft=fx.msft_facts(with_new_quarter=True))
    fx.sec_routes(sec, {}, {"20261013": fx.daily_index([("789019", "MICROSOFT CORP", "10-Q", "20261013", fx.Q27)])})
    check = fx.run(companyfacts.NAME, "2026-10-28")
    assert check["result"] == "changed", check["note"]
    assert "daily index read for 56 days (last whole scan 2026-09-01)" in check["note"]
    assert fx.q("SELECT accn FROM fundamental_fact WHERE cik = '789019' AND field = 'revenue' "
                "AND period_type = 'quarterly' AND period_end = DATE '2026-09-30'") == [(fx.Q27,)]
    days = [str(r.url).rsplit("master.", 1)[1][:8] for r in sec.calls("daily-index")]
    assert days[0] == "20260902" and days[-1] == "20261028"              # from the day after the last read
    assert "daily index read for" not in fx.run(companyfacts.NAME, "2026-10-29")["note"]
    again = [str(r.url).rsplit("master.", 1)[1][:8] for r in sec.calls("daily-index")][len(days):]
    assert again == ["20261027", "20261028", "20261029"]   # the scan is recorded: the 7-day window again (the 27th
    #                                                        answered 404 as yesterday, so it is asked again)


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


def test_an_annual_only_filer_past_400_days_is_a_build_issue(sec):
    # TM's real shape on 2026-10-28: us-gaap statements up to the 20-F for March 2020, IFRS facts after its switch.
    tm = fx.tm_facts()
    tm["facts"]["us-gaap"] = {"Revenues": {"units": {"USD": [
        fx._fact(275_000, "2020-03-31", "0001193125-20-170000", "2020-06-24", "20-F", "FY", 2020, "2019-04-01")]}}}
    fx.sec_routes(sec, {"789019": fx.msft_facts(with_new_quarter=True), "46619": fx.hei_facts(), "1094517": tm})
    check = fx.run(companyfacts.NAME, mode="backfill", to="2026-10-28")
    assert "annual-only filers 0 of 1 within 400 days" in check["note"]
    _, issues = ops.open_issues(tables.NAME, limit=50)
    found = {(i["kind"], i["key"]): i["detail"] for i in issues}
    assert "newest statement filed 2020-06-24" in found[("annual_statement_older_than_400_days", "1094517")]
    assert ("no_us_gaap_facts", "1094517") not in found


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
