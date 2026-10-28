"""``sec/13f``'s first fill from the SEC's Form 13F data sets (avo, 2026-10-28): the page's links resolved against
it, only the sets whose filing window meets the range downloaded, and each once; every 13F-HR and 13F-HR/A typed as
the EDGAR path types it (an amendment by the form's ``/A``, the CIK without its zeros, the confidential flag, the
holdings in ``INFOTABLE_SK`` order); the days written and marked done, so the forms walk reads only the days after
the newest set; a day the walk finished is not written again; an answer that is not a zip is not kept."""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.ownership import common, sec, sec13f, sec13f_sets

from . import ownership_fixture as ox

SUMMER, SPRING = "01jun2026-31aug2026_form13f.zip", "01mar2026-31may2026_form13f.zip"
HREFS = [f"/files/datastandardsinnovation/data/form-13f-data-sets/{SUMMER}",
         f"/files/structureddata/data/form-13f-data-sets/{SPRING}",
         "/files/structureddata/data/form-13f-data-sets/2013q1_form13f.zip"]
Q1, Q2 = date(2026, 3, 31), date(2026, 6, 30)


def _summer() -> bytes:
    h = ox.f13_holding
    return ox.f13_set([
        ox.set_filing("0001000001-26-000101", 1000001, "31-JUL-2026", [
            h("MICROSOFT CORP", "594918104", 400_000, 1000), h("MICROSOFT CORP", "594918104", 90_000, 400,
                                                                put_call="Put"),
            h("EATON CORP PLC", "G29183103", 150_000, 500)]),
        # The restatement: ISAMENDMENT blank, as on half the real rows; the form's /A says it amends.
        ox.set_filing("0001000001-26-000102", 1000001, "05-AUG-2026", [h("MICROSOFT CORP", "594918104", 440_000, 1100)],
                      form="13F-HR/A", amendment_type="RESTATEMENT", amendment_no="1"),
        ox.set_filing("0001000009-26-000103", 1000009, "31-JUL-2026", [], form="13F-NT", report_type="13F NOTICE"),
        ox.set_filing("0001000005-26-000104", 1000005, "31-AUG-2026", [h("MICROSOFT CORP", "594918100", 6, 100)],
                      period="30-SEP-2001"),
        ox.set_filing("0001000002-26-000105", 1000002, "14-AUG-2026", [h("WELLS FARGO", "949746101", 10_000, 200)],
                      entries=3)], folder="01JUN2026-31AUG2026_FORM13F")


def _spring() -> bytes:
    return ox.f13_set([
        ox.set_filing("0001000003-26-000050", 1000003, "15-MAY-2026", [ox.f13_holding("NVR INC", "62944T105",
                                                                                      250_000, 50)],
                      period="31-MAR-2026", confidential="Y"),
        ox.set_filing("0001000004-26-000040", 1000004, "16-MAR-2026", [ox.f13_holding("NVR INC", "62944T105",
                                                                                      200_000, 40)],
                      period="31-DEC-2025")])


@pytest.fixture
def feed(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec, "_shared_pace", lambda: True)
    monkeypatch.setenv("RC_HTTP_CONTACT", "ops@example.org")
    http_mock.add(ox.SETS_PAGE_PATH, ox.sets_page(HREFS))
    http_mock.add(HREFS[0], _summer(), headers={"last-modified": "Mon, 14 Sep 2026 16:43:16 GMT"})
    http_mock.add(HREFS[1], _spring())
    e = ox.Edgar()
    e.add_day("2026-09-01", [(ox.idx_row(1000006, "MANAGER 1000006", "13F-HR", "2026-09-01", "0001000006-26-000201"),
                              ox.f13_filing("0001000006-26-000201", 1000006, "2026-09-01",
                                            [ox.f13_holding("NVR INC", "62944T105", 260_000, 50)], "06-30-2026"))])
    e.install(http_mock)
    return e


def _backfill(start: str, end: str) -> dict:
    return base.run_request({"dataset": sec13f.NAME, "params": {"mode": "backfill", "from": start, "to": end}},
                            sleep=lambda s: None)


def _served(sql: str, *args) -> list[tuple]:
    with serving.open_read("ownership") as con:
        return con.execute(sql, list(args)).fetchall()


def _holdings(accession: str) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(f"SELECT line, cusip, value, value_usd, shares, sh_type, put_call, vote_sole, title_of_class "
                           f"FROM {common.files_sql(sec13f.DATASET.holding_files())} WHERE accession = ? "
                           "ORDER BY line", [accession]).fetchall()
    finally:
        con.close()


def test_the_window_of_a_set_is_read_from_its_name():
    assert sec13f_sets.window(SUMMER) == (date(2026, 6, 1), date(2026, 8, 31))
    assert sec13f_sets.window("01jan2024-29feb2024_form13f.zip") == (date(2024, 1, 1), date(2024, 2, 29))
    assert sec13f_sets.window("2023q4_form13f.zip") == (date(2023, 10, 1), date(2023, 12, 31))
    assert sec13f_sets.window("2013q1_form13f.zip") == (date(2013, 1, 1), date(2013, 3, 31))
    assert sec13f_sets.window("form13f_readme.zip") is None


def test_the_sets_fill_their_days_and_the_forms_walk_reads_only_the_days_after_the_newest(feed, http_mock):
    row = _backfill("2026-04-01", "2026-09-02")
    assert row["result"] == "changed", row["note"]
    asked = [str(c.url) for c in http_mock.calls("_form13f.zip")]
    assert sorted(u.rsplit("/", 1)[1] for u in asked) == [SUMMER, SPRING]          # not 2013q1
    assert f"https://sec.test{HREFS[0]}" in asked                   # resolved against the page, its own folder
    assert [str(c.url).rsplit(".", 2)[1] for c in http_mock.calls("/daily-index/")] == ["20260902", "20260901"]
    spring, summer = f"set:{SPRING}", f"set:{SUMMER}"
    assert _served(f"SELECT accession, cik, form, period, filed, is_amendment, amendment_type, amendment_no, "
                   f"confidential_omitted, status, holdings, detail, origin, filer_name FROM {sec13f.TABLE} "
                   "ORDER BY filed, accession") == [
        ("0001000003-26-000050", "1000003", "13F-HR", Q1, date(2026, 5, 15), False, None, None, True, "ok", 1, None,
         spring, "MANAGER 1000003"),
        ("0001000001-26-000101", "1000001", "13F-HR", Q2, date(2026, 7, 31), False, None, None, False, "ok", 3, None,
         summer, "MANAGER 1000001"),
        ("0001000001-26-000102", "1000001", "13F-HR/A", Q2, date(2026, 8, 5), True, "RESTATEMENT", 1, False, "ok", 1,
         None, summer, "MANAGER 1000001"),
        ("0001000002-26-000105", "1000002", "13F-HR", Q2, date(2026, 8, 14), False, None, None, False, "ok", 1,
         "1 holdings read, the summary says 3", summer, "MANAGER 1000002"),
        ("0001000005-26-000104", "1000005", "13F-HR", date(2001, 9, 30), date(2026, 8, 31), False, None, None, False,
         "ok", 1, None, summer, "MANAGER 1000005"),
        ("0001000006-26-000201", "1000006", "13F-HR", Q2, date(2026, 9, 1), False, None, None, None, "ok", 1, None,
         "edgar", "MANAGER 1000006")]
    assert _holdings("0001000001-26-000101") == [
        (0, "594918104", 400_000.0, 400_000.0, 1000.0, "SH", None, 1000, 'COM "A"'),
        (1, "594918104", 90_000.0, 90_000.0, 400.0, "SH", "Put", 400, 'COM "A"'),
        (2, "G29183103", 150_000.0, 150_000.0, 500.0, "SH", None, 500, 'COM "A"')]
    feed13f = sec13f.DATASET
    assert feed13f.is_done(date(2026, 7, 1)) and feed13f.is_done(date(2026, 4, 1))      # no 13F that day: done too
    assert not feed13f.is_done(date(2026, 3, 31)) and feed13f.first_day() == date(2026, 4, 1)
    assert f"13F data set {SUMMER}: 4 days written, 2026-06-01 to 2026-08-31" in row["note"]


def test_a_set_is_downloaded_once_and_a_day_the_walk_finished_is_not_written_again(feed, http_mock):
    feed.add_day("2026-07-31", [(ox.idx_row(1000007, "MANAGER 1000007", "13F-HR", "2026-07-31", "0001000007-26-000099"),
                                 ox.f13_filing("0001000007-26-000099", 1000007, "2026-07-31",
                                               [ox.f13_holding("NVR INC", "62944T105", 1_000, 1)], "06-30-2026"))])
    base.run_request({"dataset": sec13f.NAME, "params": {"as_of": "2026-08-03"}}, sleep=lambda s: None)
    assert sec13f.DATASET.is_done(date(2026, 7, 31))
    _backfill("2026-06-01", "2026-08-31")
    http_mock.requests.clear()
    _backfill("2026-06-01", "2026-08-31")
    assert not http_mock.calls("_form13f.zip") and len(http_mock.calls(ox.SETS_PAGE_PATH)) == 1
    assert _served(f"SELECT accession, origin FROM {sec13f.TABLE} WHERE filed = '2026-07-31'") == [
        ("0001000007-26-000099", "edgar")]
    assert sec13f_sets.raw_path(SUMMER).exists() and sec13f_sets.raw_path(SPRING).exists() is False


def test_an_answer_that_is_not_a_zip_is_refused_and_not_kept(feed, http_mock):
    http_mock.routes.insert(0, (HREFS[0], b"<html>Access Denied</html>", 200, {}))
    row = _backfill("2026-06-01", "2026-08-31")
    assert row["result"] == "failed" and "is not a zip" in row["note"]
    assert not sec13f_sets.raw_path(SUMMER).exists()
