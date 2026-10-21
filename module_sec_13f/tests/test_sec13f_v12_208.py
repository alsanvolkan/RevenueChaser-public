"""Schema v12 / #208 (F3): amendments, non-lossy capture, VALUE units and the v12 reset.

Every test of `brief.md` §3 that was not already added to ``test_ingest.py`` lives here, in
one file, so the slices stay reviewable together. All tests run against the per-test
``raw.duckdb`` (the ``rawstore_tmp_db`` fixture); nothing here touches a real store.
"""
import csv
import io
import zipfile

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_sec_13f import engine


@pytest.fixture
def store(rawstore_tmp_db):
    return rawstore_tmp_db


def _sub(acc, cik, filed, kind, period):
    return {"ACCESSION_NUMBER": acc, "CIK": cik, "FILING_DATE": filed,
            "SUBMISSIONTYPE": kind, "PERIODOFREPORT": period}


def _row(acc, name, cusip, shares, value="1000", typ="SH", putcall="", line_no=None):
    row = {"ACCESSION_NUMBER": acc, "NAMEOFISSUER": name, "CUSIP": cusip, "VALUE": value,
           "SSHPRNAMT": shares, "SSHPRNAMTTYPE": typ, "PUTCALL": putcall,
           "TITLEOFCLASS": "COM", "INVESTMENTDISCRETION": "SOLE", "OTHERMANAGER": "",
           "VOTING_AUTH_SOLE": "0", "VOTING_AUTH_SHARED": "0", "VOTING_AUTH_NONE": "0"}
    if line_no is not None:
        row["INFOTABLE_SK"] = str(line_no)
    return row


def _cover(acc, period, report_type="13F-HR", is_amendment="", amendment_no="", amendment_type=""):
    return {"ACCESSION_NUMBER": acc, "REPORTCALENDARORQUARTER": period,
            "ISAMENDMENT": is_amendment, "AMENDMENTNO": amendment_no,
            "AMENDMENTTYPE": amendment_type, "REPORTTYPE": report_type}


def _write_zip(path, submission_rows, infotable_rows=(), coverpage_rows=None,
               prefix="", upper=False):
    """A synthetic ``*_form13f.zip``; ``prefix`` nests the members, ``upper`` upper-cases their names."""
    with zipfile.ZipFile(path, "w") as z:
        def write(name, text):
            z.writestr(prefix + (name.upper() if upper else name), text)

        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["ACCESSION_NUMBER", "CIK", "FILING_DATE",
                                            "SUBMISSIONTYPE", "PERIODOFREPORT"], delimiter="\t")
        w.writeheader()
        w.writerows(submission_rows)
        write("SUBMISSION.tsv", buf.getvalue())

        if coverpage_rows is not None:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=["ACCESSION_NUMBER", "REPORTCALENDARORQUARTER",
                                                "ISAMENDMENT", "AMENDMENTNO", "AMENDMENTTYPE",
                                                "REPORTTYPE"], delimiter="\t")
            w.writeheader()
            w.writerows(coverpage_rows)
            write("COVERPAGE.tsv", buf.getvalue())

        if infotable_rows:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=[
                "INFOTABLE_SK", "ACCESSION_NUMBER", "NAMEOFISSUER", "CUSIP", "VALUE", "SSHPRNAMT",
                "SSHPRNAMTTYPE", "PUTCALL", "TITLEOFCLASS", "INVESTMENTDISCRETION", "OTHERMANAGER",
                "VOTING_AUTH_SOLE", "VOTING_AUTH_SHARED", "VOTING_AUTH_NONE"], delimiter="\t")
            w.writeheader()
            seen = {}
            for row in infotable_rows:
                row = dict(row)
                acc = row["ACCESSION_NUMBER"]
                seen[acc] = seen.get(acc, 0) + 1
                row.setdefault("INFOTABLE_SK", str(seen[acc]))
                w.writerow(row)
            write("INFOTABLE.tsv", buf.getvalue())
    return path


class TestNonLossyIngest:
    """brief.md §3 tests 7-10 and 23."""

    def test_holdings_rows_carry_their_own_submission_period(self, store, tmp_path):
        """A filing-window zip holds several periods; each holding stores its own, not the zip's first."""
        z = _write_zip(
            tmp_path / "window_form13f.zip",
            [_sub("q1", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026"),
             _sub("q2", "2000", "01-AUG-2026", "13F-HR", "30-JUN-2026")],
            [_row("q1", "ALPHA INC", "111111111", "100", "1000"),
             _row("q2", "BETA INC", "222222222", "50", "500")])
        engine.ingest_zips([z])
        periods = {r["accession"]: r["period"] for r in rawstore.query(
            "SELECT accession, period FROM sec13f_holdings")}
        assert periods == {"q1": "2026-03-31", "q2": "2026-06-30"}

    def test_option_rows_are_excluded_from_every_read_site(self, store, tmp_path):
        """brief.md §3 test 7: stored, but out of the totals, the map list, the unmatched note and held value."""
        z = _write_zip(
            tmp_path / "opts_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("a", "ALPHA INC", "111111111", "100", "1000"),
             _row("a", "ALPHA INC", "111111111", "99", "9999", putcall="CALL"),
             _row("a", "ALPHA INC", "111111111", "88", "8888", typ="PRN"),
             # an option CUSIP and an unmapped CUSIP, both option/PRN only
             _row("a", "ALPHA INC", "333333909", "77", "7777", putcall="PUT"),
             _row("a", "ALPHA INC", "444444108", "66", "6666", typ="PRN")])
        engine.ingest_zips([z])
        assert rawstore.count("sec13f_holdings") == 5
        # map_candidates: only the long-equity CUSIP is proposed
        assert engine.map_candidates() == ["111111111"]
        engine.save_cusip_ticker({"111111111": "A"})
        totals = engine.recompute_totals()
        assert totals["A"]["2026-03-31"]["value"] == 1000.0
        # held_value: the denominators the guard divides by
        assert engine.held_value("2026-03-31") == 1000.0
        # unmatched_summary: the option-only CUSIPs are not reported as left out
        summary = engine.unmatched_summary({"111111111": "A"})
        assert summary["cusips"] == 0

    def test_duplicate_infotable_sk_falls_back_to_a_free_line_no(self, store, tmp_path):
        """brief.md §1 S2: the ordinal fallback must not collide with a real ``INFOTABLE_SK``."""
        z = _write_zip(
            tmp_path / "dupsk_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("a", "ALPHA INC", "111111111", "100", "1000", line_no=1),
             _row("a", "BETA INC", "222222222", "50", "500", line_no=2),
             _row("a", "GAMMA INC", "333333333", "10", "100", line_no=2)])
        engine.ingest_zips([z])
        line_nos = [r["line_no"] for r in rawstore.query(
            "SELECT line_no FROM sec13f_holdings ORDER BY line_no")]
        assert line_nos == [1, 2, 3]

    def test_nested_and_upper_case_members_ingest_identically(self, store, tmp_path):
        """brief.md §3 test 23: a nested zip and an upper-case-member zip read the same four tables."""
        shapes = {"flat": {}, "nested": {"prefix": "FOLDER/"}, "upper": {"upper": True}}
        for acc, shape in shapes.items():
            z = _write_zip(
                tmp_path / f"{acc}_form13f.zip",
                [_sub(acc, "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
                [_row(acc, "ALPHA INC", "111111111", "100", "1000")],
                coverpage_rows=[_cover(acc, "31-MAR-2026")],
                **shape)
            engine.ingest_zips([z])
        rows = {r["accession"]: r for r in rawstore.query("SELECT * FROM sec13f_submissions")}
        assert set(rows) == set(shapes)
        for acc in shapes:
            # COVERPAGE was found in every shape: report_type is populated, not NULL
            assert rows[acc]["report_type"] == "13F-HR"
        assert rawstore.count("sec13f_holdings") == 3
