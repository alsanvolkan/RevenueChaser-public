"""``sec/13f``'s first fill from the SEC's Form 13F data sets (avo, 2026-10-28): the page's links resolved against
it, only the sets whose filing window meets the range downloaded, and each once; every 13F-HR and 13F-HR/A typed as
the EDGAR path types it (an amendment by the form's ``/A``, the CIK without its zeros, the confidential flag, the
holdings in ``INFOTABLE_SK`` order); the days written and marked done, so the forms walk reads only the days after
the newest set; a day the walk finished is not written again; an answer that is not a zip is not kept."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.ownership import common, edgar, sec, sec13f, sec13f_sets

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


def _backfill(start: str, end: str, **params) -> dict:
    return base.run_request({"dataset": sec13f.NAME, "params": {"mode": "backfill", "from": start, "to": end,
                                                                **params}}, sleep=lambda s: None)


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
    # The walk reads the days after the newest set, and the spring set's days after its newest filing (15 May).
    assert [str(c.url).rsplit(".", 2)[1] for c in http_mock.calls("/daily-index/")] == [
        "20260902", "20260901", "20260529", "20260528", "20260527", "20260526", "20260525", "20260522", "20260521",
        "20260520", "20260519", "20260518"]
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


def test_a_set_is_downloaded_once_asked_again_only_if_modified_and_a_walked_day_is_kept(feed, http_mock):
    feed.add_day("2026-07-31", [(ox.idx_row(1000007, "MANAGER 1000007", "13F-HR", "2026-07-31", "0001000007-26-000099"),
                                 ox.f13_filing("0001000007-26-000099", 1000007, "2026-07-31",
                                               [ox.f13_holding("NVR INC", "62944T105", 1_000, 1)], "06-30-2026"))])
    base.run_request({"dataset": sec13f.NAME, "params": {"as_of": "2026-08-03"}}, sleep=lambda s: None)
    assert sec13f.DATASET.is_done(date(2026, 7, 31))
    _backfill("2026-06-01", "2026-08-31")
    http_mock.requests.clear()
    _backfill("2026-06-01", "2026-08-31")
    # The kept summer set is asked whether it changed (the spring one carried no Last-Modified to ask with).
    assert [c.headers.get("if-modified-since") for c in http_mock.calls("_form13f.zip")] == [
        "Mon, 14 Sep 2026 16:43:16 GMT"]
    assert len(http_mock.calls(ox.SETS_PAGE_PATH)) == 1
    assert _served(f"SELECT accession, origin FROM {sec13f.TABLE} WHERE filed = '2026-07-31'") == [
        ("0001000007-26-000099", "edgar")]
    assert sec13f_sets.raw_path(SUMMER).exists() and sec13f_sets.raw_path(SPRING).exists() is False


def test_an_answer_that_is_not_a_zip_is_refused_and_not_kept(feed, http_mock):
    http_mock.routes.insert(0, (HREFS[0], b"<html>Access Denied</html>", 200, {}))
    row = _backfill("2026-06-01", "2026-08-31")
    assert row["result"] == "failed" and "is not a zip" in row["note"]
    assert not sec13f_sets.raw_path(SUMMER).exists()


def _early_summer() -> bytes:
    """The summer set as it might be published while its window is still open: nothing filed after 5 August."""
    return ox.f13_set([ox.set_filing("0001000001-26-000101", 1000001, "31-JUL-2026",
                                     [ox.f13_holding("MICROSOFT CORP", "594918104", 400_000, 1000)]),
                       ox.set_filing("0001000008-26-000110", 1000008, "05-AUG-2026",
                                     [ox.f13_holding("NVR INC", "62944T105", 100_000, 20)])])


def test_a_set_marks_done_only_the_days_up_to_its_newest_filing(feed, http_mock):
    http_mock.routes.insert(0, (HREFS[0], _early_summer(), 200, {"last-modified": "Mon, 10 Aug 2026 09:00:00 GMT"}))
    row = _backfill("2026-06-01", "2026-09-02")
    assert row["result"] == "changed", row["note"]
    origin = [json.loads((sec13f.DATASET.day_dir(date(2026, 8, d)) / edgar.DONE).read_text()).get("origin")
              for d in (5, 6)]
    assert origin == [f"set:{SUMMER}", None]                    # 6 August is the walk's, not the set's
    walked = {str(c.url).rsplit(".", 2)[1] for c in http_mock.calls("/daily-index/")}
    assert {"20260806", "20260831", "20260901"} <= walked and "20260805" not in walked
    assert "13F data set 01jun2026-31aug2026_form13f.zip: its newest filing is 2026-08-05; 18 later days left " \
           "to the forms walk" in row["note"]


def test_a_set_the_sec_publishes_again_reopens_the_days_it_filled(feed, http_mock):
    versions = {"Mon, 10 Aug 2026 09:00:00 GMT": _early_summer(), "Mon, 14 Sep 2026 16:43:16 GMT": _summer()}
    current = ["Mon, 10 Aug 2026 09:00:00 GMT"]

    def answer(request):
        if request.headers.get("if-modified-since") == current[0]:
            return 304, b"", {"last-modified": current[0]}
        return 200, versions[current[0]], {"last-modified": current[0]}

    http_mock.routes.insert(0, (HREFS[0], answer, 200, {}))
    _backfill("2026-06-01", "2026-08-31")
    assert _served(f"SELECT count(*) FROM {sec13f.TABLE} WHERE filed = '2026-07-31'") == [(1,)]
    http_mock.requests.clear()
    row = _backfill("2026-06-01", "2026-08-31")                 # unchanged: asked, 304, nothing typed again
    assert row["result"] != "failed", row["note"]
    assert "reopened" not in row["note"], row["note"]
    asked = http_mock.calls("_form13f.zip")
    assert [c.headers.get("if-modified-since") for c in asked] == ["Mon, 10 Aug 2026 09:00:00 GMT"]
    current[0] = "Mon, 14 Sep 2026 16:43:16 GMT"
    row = _backfill("2026-06-01", "2026-08-31", allow_shrink=True)        # the second version drops NVR's filing
    assert "13F data set 01jun2026-31aug2026_form13f.zip: changed since it was read, 48 days reopened" in row["note"]
    # The days the first version filled are typed again (000102 is new, 000110 is gone); the days after its newest
    # filing were the walk's (the mock index has none of them) and stay the walk's.
    assert _served(f"SELECT accession FROM {sec13f.TABLE} WHERE filed BETWEEN '2026-07-31' AND '2026-08-31' "
                   "ORDER BY accession") == [("0001000001-26-000101",), ("0001000001-26-000102",)]
    assert sec13f.DATASET.is_done(date(2026, 8, 31))


def test_a_report_with_no_information_table_is_typed_alike_on_both_paths(feed, http_mock):
    """Review S3: one rule for both paths. A holdings report with no holdings read is unparsed unless its summary
    says 0 entries (all left out in confidence: it is the filer's report for the quarter); a notice is a notice."""
    sets = [ox.set_filing("0001000011-26-000301", 1000011, "31-JUL-2026", [], entries=2),
            ox.set_filing("0001000012-26-000302", 1000012, "31-JUL-2026", [], entries=0, confidential="Y"),
            ox.set_filing("0001000013-26-000303", 1000013, "31-JUL-2026", [], report_type="13F NOTICE")]
    http_mock.routes.insert(0, (HREFS[0], ox.f13_set(sets), 200, {}))
    feed.add_day("2026-09-02", [
        (ox.idx_row(1000021, "MANAGER 1000021", "13F-HR", "2026-09-02", "0001000021-26-000401"),
         ox.f13_filing("0001000021-26-000401", 1000021, "2026-09-02", None, "06-30-2026", entries=2)),
        (ox.idx_row(1000022, "MANAGER 1000022", "13F-HR", "2026-09-02", "0001000022-26-000402"),
         ox.f13_filing("0001000022-26-000402", 1000022, "2026-09-02", None, "06-30-2026", entries=0,
                       confidential=True)),
        (ox.idx_row(1000023, "MANAGER 1000023", "13F-HR", "2026-09-02", "0001000023-26-000403"),
         ox.f13_filing("0001000023-26-000403", 1000023, "2026-09-02", [], "06-30-2026", report_type="13F NOTICE"))])
    _backfill("2026-06-01", "2026-09-02")
    typed = _served(f"SELECT substr(cik, -1), origin = 'edgar', status, holdings, detail FROM {sec13f.TABLE} "
                    "WHERE cik > '1000010' ORDER BY 2, 1")
    expected = [("1", "unparsed", 0, "no information table"), ("2", "ok", 0, None), ("3", "notice", 0, None)]
    assert [(c, s, n, d) for c, edgar_path, s, n, d in typed if not edgar_path] == expected
    assert [(c, s, n, d) for c, edgar_path, s, n, d in typed if edgar_path] == expected


def test_a_short_backfill_reads_the_forms_and_never_the_sets(feed, http_mock):
    """Review S6: re-walking a few days from EDGAR must not fetch the sets page or a 100 MB zip, nor mark the days
    done from a set."""
    feed.add_day("2026-07-31", [(ox.idx_row(1000007, "MANAGER 1000007", "13F-HR", "2026-07-31", "0001000007-26-000099"),
                                 ox.f13_filing("0001000007-26-000099", 1000007, "2026-07-31",
                                               [ox.f13_holding("NVR INC", "62944T105", 1_000, 1)], "06-30-2026"))])
    row = _backfill("2026-07-29", "2026-07-31")
    assert not http_mock.calls(ox.SETS_PAGE_PATH) and not http_mock.calls("_form13f.zip")
    assert [str(c.url).rsplit(".", 2)[1] for c in http_mock.calls("/daily-index/")] == ["20260731", "20260730",
                                                                                        "20260729"]
    assert _served(f"SELECT accession, origin FROM {sec13f.TABLE}") == [("0001000007-26-000099", "edgar")]
    assert "a backfill of 3 weekdays reads the forms; the data sets fill a range of 20 weekdays or more" in row["note"]


def _with_repeated_rows(zip_bytes: bytes, members: tuple[str, ...], again=lambda line: line) -> bytes:
    """The zip with each member's first data row written twice, as a malformed set might; ``again`` rewrites the
    repeat (a conflicting row)."""
    src, out = zipfile.ZipFile(io.BytesIO(zip_bytes)), io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in src.namelist():
            body = src.read(name).decode()
            if name.rsplit("/", 1)[-1].split(".")[0] in members:
                lines = body.splitlines(keepends=True)
                body = "".join(lines[:2] + [again(lines[1])] + lines[2:])
            z.writestr(name, body)
    return out.getvalue()


def test_a_cover_or_summary_row_written_twice_does_not_count_a_filing_twice(feed, http_mock):
    """Review N4: COVERPAGE and SUMMARYPAGE are joined on the accession; a repeated row must not fan the filing and
    its holdings out."""
    one = ox.f13_set([ox.set_filing("0001000001-26-000101", 1000001, "31-JUL-2026",
                                    [ox.f13_holding("MICROSOFT CORP", "594918104", 400_000, 1000)])])
    http_mock.routes.insert(0, (HREFS[0], _with_repeated_rows(one, ("COVERPAGE", "SUMMARYPAGE")), 200, {}))
    _backfill("2026-06-01", "2026-08-31")
    assert _served(f"SELECT accession, holdings FROM {sec13f.TABLE}") == [("0001000001-26-000101", 1)]
    assert len(_holdings("0001000001-26-000101")) == 1


def test_of_two_conflicting_summary_rows_the_first_the_set_wrote_is_kept(feed, http_mock):
    """Delta review D6: when a repeated SUMMARYPAGE row disagrees with the first, the first row of the file is kept,
    the same one on every run."""
    one = ox.f13_set([ox.set_filing("0001000001-26-000101", 1000001, "31-JUL-2026",
                                    [ox.f13_holding("MICROSOFT CORP", "594918104", 400_000, 1000)])])
    cols = ox.SET_COLUMNS["SUMMARYPAGE"]

    def conflicting(line: str) -> str:
        cells = line.rstrip("\n").split("\t")
        cells[cols.index("TABLEENTRYTOTAL")] = "7"
        return "\t".join(cells) + "\n"
    http_mock.routes.insert(0, (HREFS[0], _with_repeated_rows(one, ("SUMMARYPAGE",), conflicting), 200, {}))
    _backfill("2026-06-01", "2026-08-31")
    assert _served(f"SELECT entry_total, detail FROM {sec13f.TABLE}") == [(1, None)]


def _with_bad_rows(zip_bytes: bytes) -> bytes:
    """The zip with its INFOTABLE's first data row cut short (a tab too few) and its second carrying a byte that is
    not UTF-8 in the issuer's name, as a damaged set might."""
    src, out = zipfile.ZipFile(io.BytesIO(zip_bytes)), io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in src.namelist():
            body = src.read(name)
            if name.rsplit("/", 1)[-1] == "INFOTABLE.tsv":
                lines = body.splitlines(keepends=True)
                lines[1] = b"\t".join(lines[1].split(b"\t")[:3]) + b"\n"
                lines[2] = lines[2].replace(b"MICROSOFT", b"MICROSOFT \xc9", 1)
                body = b"".join(lines)
            z.writestr(name, body)
    return out.getvalue()


def test_a_row_that_does_not_read_is_rejected_and_counted_not_the_whole_set(feed, http_mock):
    """13F review B3 (2026-10-30): one damaged INFOTABLE row (a tab too few, a byte that is not UTF-8) failed the
    read of the whole set, and with it the year's fill. The row is left out and counted in the run's note; the
    filing it belonged to says in its detail that fewer holdings were read than its summary counts."""
    h = ox.f13_holding
    one = ox.f13_set([
        ox.set_filing("0001000001-26-000101", 1000001, "31-JUL-2026", [
            h("MICROSOFT CORP", "594918104", 400_000, 1000), h("MICROSOFT CORP", "594918104", 90_000, 400,
                                                                put_call="Put"),
            h("EATON CORP PLC", "G29183103", 150_000, 500)]),
        ox.set_filing("0001000002-26-000105", 1000002, "14-AUG-2026", [h("WELLS FARGO", "949746101", 10_000, 200)])])
    http_mock.routes.insert(0, (HREFS[0], _with_bad_rows(one), 200, {}))
    row = _backfill("2026-06-01", "2026-08-31")
    assert row["result"] == "changed", row["note"]
    assert (f"13F data set {SUMMER}: 2 rows of INFOTABLE.tsv rejected (first: line 2, MISSING COLUMNS)"
            in row["note"]), row["note"]
    assert _served(f"SELECT accession, holdings, status, detail FROM {sec13f.TABLE} ORDER BY accession") == [
        ("0001000001-26-000101", 1, "ok", "1 holdings read, the summary says 3"),
        ("0001000002-26-000105", 1, "ok", None)]
    assert [r[1] for r in _holdings("0001000001-26-000101")] == ["594918104"]


def test_the_cover_pages_period_wins_on_both_paths_and_a_cusip_placeholder_is_no_cusip(feed, http_mock):
    """13F review D2 and D3 (2026-10-30). The EDGAR path takes the period from the cover page and falls back on
    the submission header; the sets path did the opposite, so one filing could land in two quarters. And a CUSIP
    written as ``000000000`` or ``N/A`` is none (the old engine's rule), not a name no source maps."""
    h = ox.f13_holding
    sets = [ox.set_filing("0001000031-26-000501", 1000031, "31-JUL-2026", [h("MICROSOFT CORP", "594918104", 400, 1)],
                          period="30-JUN-2026", cover_period="31-MAR-2026"),
            ox.set_filing("0001000032-26-000502", 1000032, "31-JUL-2026", [
                h("NO CUSIP FUND", "000000000", 100, 1), h("ANOTHER", "n/a", 100, 1), h("EATON CORP PLC", "G29183103",
                                                                                         150_000, 500)])]
    http_mock.routes.insert(0, (HREFS[0], ox.f13_set(sets), 200, {}))
    feed.add_day("2026-09-02", [
        (ox.idx_row(1000033, "MANAGER 1000033", "13F-HR", "2026-09-02", "0001000033-26-000503"),
         ox.f13_filing("0001000033-26-000503", 1000033, "2026-09-02", [h("NO CUSIP FUND", "000000000", 100, 1),
                                                                        h("ANOTHER", "N/A", 100, 1)], "06-30-2026"))])
    _backfill("2026-06-01", "2026-09-02")
    assert _served(f"SELECT period FROM {sec13f.TABLE} WHERE accession = '0001000031-26-000501'") == [(Q1,)]
    assert [r[1] for r in _holdings("0001000032-26-000502")] == [None, None, "G29183103"]
    assert [r[1] for r in _holdings("0001000033-26-000503")] == [None, None]


def test_a_set_before_2023_has_no_figi_column_and_still_reads(feed, http_mock):
    """13F review D4 (2026-10-30): FIGI came to Form 13F in 2023; a set before it has no such column, and the
    typing SQL named it."""
    one = ox.f13_set([ox.set_filing("0001000001-26-000101", 1000001, "31-JUL-2026",
                                    [ox.f13_holding("MICROSOFT CORP", "594918104", 400_000, 1000)])],
                     without=("FIGI",))
    http_mock.routes.insert(0, (HREFS[0], one, 200, {}))
    row = _backfill("2026-06-01", "2026-08-31")
    assert row["result"] == "changed", row["note"]
    con = duckdb.connect()
    try:
        assert con.execute(f"SELECT cusip, figi FROM {common.files_sql(sec13f.DATASET.holding_files())}").fetchall() \
            == [("594918104", None)]
    finally:
        con.close()


@pytest.mark.parametrize("start, end", [("2026-06-01", "2026-08-31"), ("2026-07-29", "2026-07-31")])
def test_a_killed_runs_typing_scratch_is_removed_by_the_next_fill(feed, http_mock, start, end):
    """Review (deploy risk): a set is typed in a scratch folder beside the raw zips, several GB; a killed run leaves
    it behind where clean-scratch does not look, so the next backfill removes it, a short one too (delta D4)."""
    feed.add_day("2026-07-31", [(ox.idx_row(1000007, "MANAGER 1000007", "13F-HR", "2026-07-31", "0001000007-26-000099"),
                                 ox.f13_filing("0001000007-26-000099", 1000007, "2026-07-31",
                                               [ox.f13_holding("NVR INC", "62944T105", 1_000, 1)], "06-30-2026"))])
    stale = sec13f_sets.raw_dir() / ".typing-killed"
    stale.mkdir(parents=True)
    (stale / "set.duckdb").write_bytes(b"x" * 10)
    row = _backfill(start, end)
    assert not stale.exists() and not list(sec13f_sets.raw_dir().glob(".typing-*"))
    assert "13F data sets: 1 typing folder of a killed run removed" in row["note"]
