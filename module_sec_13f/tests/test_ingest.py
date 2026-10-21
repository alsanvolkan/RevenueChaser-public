"""Tests for the SEC Form 13F ZIP -> raw.duckdb ingest pipeline (engine.py)."""
import csv
import io
import zipfile

import pytest

from RevenueChaser.module_sec_13f import engine as sf


def _write_zip(path, submission_rows, infotable_rows=None, coverpage_rows=None,
               summarypage_rows=None, member_prefix=""):
    """Build a synthetic ``*_form13f.zip`` with SUBMISSION.tsv (+ optional INFOTABLE.tsv).

    Supports COVERPAGE.tsv and SUMMARYPAGE.tsv rows. INFOTABLE rows get
    ``INFOTABLE_SK`` assigned sequentially per accession when it is missing, so
    callers that only pass the legacy seven fields still produce a valid v12 row.
    """
    with zipfile.ZipFile(path, "w") as z:
        def writestr(name, content):
            z.writestr(member_prefix + name, content)

        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=[
            "ACCESSION_NUMBER", "CIK", "FILING_DATE", "SUBMISSIONTYPE", "PERIODOFREPORT"],
            delimiter="\t")
        w.writeheader()
        w.writerows(submission_rows)
        writestr("SUBMISSION.tsv", buf.getvalue())

        if coverpage_rows is not None:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=[
                "ACCESSION_NUMBER", "REPORTCALENDARORQUARTER", "ISAMENDMENT",
                "AMENDMENTNO", "AMENDMENTTYPE", "REPORTTYPE"], delimiter="\t")
            w.writeheader()
            w.writerows(coverpage_rows)
            writestr("COVERPAGE.tsv", buf.getvalue())

        if summarypage_rows is not None:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=[
                "ACCESSION_NUMBER", "OTHERINCLUDEDMANAGERSCOUNT", "TABLEENTRYTOTAL",
                "TABLEVALUETOTAL", "ISCONFIDENTIALOMITTED"], delimiter="\t")
            w.writeheader()
            w.writerows(summarypage_rows)
            writestr("SUMMARYPAGE.tsv", buf.getvalue())

        if infotable_rows is not None:
            buf2 = io.StringIO()
            base_fields = [
                "ACCESSION_NUMBER", "NAMEOFISSUER", "CUSIP", "VALUE",
                "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"]
            extra_fields = [
                "TITLEOFCLASS", "INVESTMENTDISCRETION", "OTHERMANAGER",
                "VOTING_AUTH_SOLE", "VOTING_AUTH_SHARED", "VOTING_AUTH_NONE"]
            # INFOTABLE_SK is the first column in real files; keep the same order.
            fieldnames = ["INFOTABLE_SK"] + base_fields + extra_fields
            w2 = csv.DictWriter(buf2, fieldnames=fieldnames, delimiter="\t")
            w2.writeheader()
            per_acc = {}
            for row in infotable_rows:
                acc = row["ACCESSION_NUMBER"]
                per_acc[acc] = per_acc.get(acc, 0) + 1
                if "INFOTABLE_SK" not in row or not row["INFOTABLE_SK"]:
                    row = dict(row)
                    row["INFOTABLE_SK"] = str(per_acc[acc])
                w2.writerow(row)
            writestr("INFOTABLE.tsv", buf2.getvalue())
    return path


@pytest.fixture
def isolated_store(rawstore_tmp_db):
    """Point the SEC-13F store at the per-test raw.duckdb."""
    return rawstore_tmp_db


def _two_holder_zips(tmp_path):
    """Two synthetic zips: 2 holders of AAPL (300 sh combined) + 1 holder of MSFT."""
    zip1 = _write_zip(
        tmp_path / "2026q2_form13f.zip",
        [{"ACCESSION_NUMBER": "acc1", "CIK": "1000", "FILING_DATE": "01-MAY-2026",
          "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "31-MAR-2026"}],
        [{"ACCESSION_NUMBER": "acc1", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
          "VALUE": "1000", "SSHPRNAMT": "100", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
         {"ACCESSION_NUMBER": "acc1", "NAMEOFISSUER": "MICROSOFT CORP", "CUSIP": "222222222",
          "VALUE": "500", "SSHPRNAMT": "50", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
         # noise that must be excluded from aggregation:
         {"ACCESSION_NUMBER": "acc1", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
          "VALUE": "9999", "SSHPRNAMT": "9999", "SSHPRNAMTTYPE": "SH", "PUTCALL": "CALL"},
         {"ACCESSION_NUMBER": "acc1", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
          "VALUE": "9999", "SSHPRNAMT": "9999", "SSHPRNAMTTYPE": "PRN", "PUTCALL": ""}])
    zip2 = _write_zip(
        tmp_path / "2026q2b_form13f.zip",
        [{"ACCESSION_NUMBER": "acc2", "CIK": "2000", "FILING_DATE": "01-MAY-2026",
          "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "31-MAR-2026"}],
        [{"ACCESSION_NUMBER": "acc2", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
          "VALUE": "2000", "SSHPRNAMT": "200", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""}])
    return zip1, zip2


def _row(acc, name, cusip, shares, value="1000", typ="SH", putcall="",
         line_no=None, title_of_class="COM", investment_discretion="SOLE",
         other_manager="", voting_sole="0", voting_shared="0", voting_none="0"):
    row = {"ACCESSION_NUMBER": acc, "NAMEOFISSUER": name, "CUSIP": cusip, "VALUE": value,
           "SSHPRNAMT": shares, "SSHPRNAMTTYPE": typ, "PUTCALL": putcall,
           "TITLEOFCLASS": title_of_class, "INVESTMENTDISCRETION": investment_discretion,
           "OTHERMANAGER": other_manager, "VOTING_AUTH_SOLE": voting_sole,
           "VOTING_AUTH_SHARED": voting_shared, "VOTING_AUTH_NONE": voting_none}
    if line_no is not None:
        row["INFOTABLE_SK"] = str(line_no)
    return row


def _sub(acc, cik, filed, kind, period):
    return {"ACCESSION_NUMBER": acc, "CIK": cik, "FILING_DATE": filed,
            "SUBMISSIONTYPE": kind, "PERIODOFREPORT": period}


def _cover(acc, period, report_type="13F-HR", is_amendment="", amendment_no="",
           amendment_type=""):
    return {"ACCESSION_NUMBER": acc, "REPORTCALENDARORQUARTER": period,
            "ISAMENDMENT": is_amendment, "AMENDMENTNO": amendment_no,
            "AMENDMENTTYPE": amendment_type, "REPORTTYPE": report_type}


def _summary(acc, other_managers_count="1"):
    return {"ACCESSION_NUMBER": acc, "OTHERINCLUDEDMANAGERSCOUNT": other_managers_count,
            "TABLEENTRYTOTAL": "", "TABLEVALUETOTAL": "", "ISCONFIDENTIALOMITTED": ""}


def _two_quarter_zips(tmp_path):
    """Two quarters, an amendment, a two-CUSIP ticker, churn and a notice filing.

    Q1 (2026-03-31): holders 1000 (AAPL 100 + AAPL-B 10), 2000 (AAPL 200), 3000 (MSFT 40).
    Q2 (2026-06-30): 1000 amended (150 AAPL), 2000 gone, 4000 new (AAPL 70), 3000 (MSFT 60).
    """
    q1 = _write_zip(
        tmp_path / "2026q1_form13f.zip",
        [_sub("a1", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026"),
         _sub("a2", "2000", "01-MAY-2026", "13F-HR", "31-MAR-2026"),
         _sub("a3", "3000", "01-MAY-2026", "13F-HR", "31-MAR-2026"),
         _sub("n1", "5000", "01-MAY-2026", "13F-NT", "31-MAR-2026")],
        [_row("a1", "APPLE INC", "111111111", "100", "1000"),
         _row("a1", "APPLE INC CL B", "111111112", "10", "50"),
         _row("a2", "APPLE INC", "111111111", "200", "2000"),
         _row("a3", "MICROSOFT CORP", "222222222", "40", "400"),
         _row("a3", "MICROSOFT CORP", "222222222", "9999", "9", typ="PRN"),
         _row("a3", "MICROSOFT CORP", "222222222", "9999", "9", putcall="PUT")])
    q2 = _write_zip(
        tmp_path / "2026q2_form13f.zip",
        [_sub("b1", "1000", "01-AUG-2026", "13F-HR", "30-JUN-2026"),
         _sub("b1a", "1000", "15-AUG-2026", "13F-HR/A", "30-JUN-2026"),
         _sub("b3", "3000", "01-AUG-2026", "13F-HR", "30-JUN-2026"),
         _sub("b4", "4000", "01-AUG-2026", "13F-HR", "30-JUN-2026")],
        [_row("b1", "APPLE INC", "111111111", "999", "9999"),
         _row("b1a", "APPLE INC", "111111111", "150", "1500"),
         _row("b3", "MICROSOFT CORP", "222222222", "60", "600"),
         _row("b4", "APPLE INC", "111111111", "70", "700")])
    return q1, q2


class TestIngestZips:
    def test_ingest_is_idempotent(self, isolated_store, tmp_path):
        zip1, zip2 = _two_holder_zips(tmp_path)

        assert sf.ingest_zips([zip1, zip2]) == 2
        # re-ingesting the same filenames is a no-op (tracked via ingested_zips table)
        assert sf.ingest_zips([zip1, zip2]) == 0

    def test_interrupted_ingest_does_not_duplicate_holdings(self, isolated_store, tmp_path):
        """Regression: holdings are written in chunks and the zip's submissions,
        names and ``ingested_zips`` marker land last, in one transaction. If the
        run dies after the holdings but before that, the next run re-ingests the
        zip — with the v12 (accession, line_no) key the second run is a no-op
        (``ON CONFLICT DO NOTHING``), so the row count stays the same and no
        ``DELETE`` runs.
        """
        from RevenueChaser.core import rawstore
        zip1, _ = _two_holder_zips(tmp_path)

        real = rawstore._Tx.write_rows

        def die_before_marker(self, name, rows):
            if name == "sec13f_ingested_zips":
                raise RuntimeError("interrupted")
            return real(self, name, rows)

        rawstore._Tx.write_rows = die_before_marker
        try:
            with pytest.raises(RuntimeError):
                sf.ingest_zips([zip1])
        finally:
            rawstore._Tx.write_rows = real

        partial = rawstore.count("sec13f_holdings")
        assert partial > 0, "holdings were written before the interruption"
        assert rawstore.count("sec13f_ingested_zips") == 0, "zip was not marked ingested"

        deletes = []
        raw_delete = rawstore.execute

        def spy_delete(sql, *args, **kwargs):
            if isinstance(sql, str) and sql.strip().upper().startswith("DELETE"):
                deletes.append(sql)
            return raw_delete(sql, *args, **kwargs)

        rawstore.execute = spy_delete
        try:
            assert sf.ingest_zips([zip1]) == 1
        finally:
            rawstore.execute = raw_delete

        assert rawstore.count("sec13f_holdings") == partial
        assert not deletes, "v12 ingest must not DELETE holdings"

        totals = sf.compute_totals({"111111111": "AAPL"})
        assert totals["AAPL"]["2026-03-31"]["shares"] == 100.0
        assert totals["AAPL"]["2026-03-31"]["holders"] == 1

    def test_crash_mid_zip_leaves_no_submissions_marker_or_visible_holdings(
            self, isolated_store, tmp_path):
        """O16: the submissions row is what makes a zip's holdings count (the derive
        joins through it), and it is written together with the marker. A crash
        anywhere before that leaves nothing the derive can see."""
        from RevenueChaser.core import rawstore
        zip1, _ = _two_holder_zips(tmp_path)

        real = rawstore._Tx.write_rows

        def die_on_names(self, name, rows):
            if name == "sec13f_cusip_names":
                raise RuntimeError("interrupted")
            return real(self, name, rows)

        rawstore._Tx.write_rows = die_on_names
        try:
            with pytest.raises(RuntimeError):
                sf.ingest_zips([zip1])
        finally:
            rawstore._Tx.write_rows = real

        assert rawstore.count("sec13f_submissions") == 0
        assert rawstore.count("sec13f_ingested_zips") == 0
        assert sf.compute_totals({"111111111": "AAPL"}) == {}  # holdings are not visible yet

        assert sf.ingest_zips([zip1]) == 1  # a rerun is clean
        assert rawstore.count("sec13f_submissions") == 1
        assert sf.compute_totals({"111111111": "AAPL"})["AAPL"]["2026-03-31"]["shares"] == 100.0

    def test_holdings_are_written_in_chunks(self, isolated_store, tmp_path, monkeypatch):
        """The zip is streamed, not held in memory: a small chunk size splits the write.
        Non-lossy ingest keeps puts/PRNs too, so the stored row count is 4."""
        from RevenueChaser.core import rawstore
        monkeypatch.setattr(sf, "_HOLDINGS_CHUNK", 1)
        seen = []
        real = rawstore.write_rows

        def spy(name, rows):
            if name == "sec13f_holdings":
                seen.append(len(rows))
            return real(name, rows)

        monkeypatch.setattr(rawstore, "write_rows", spy)
        zip1, _ = _two_holder_zips(tmp_path)
        sf.ingest_zips([zip1])
        assert seen == [1, 1, 1, 1] and rawstore.count("sec13f_holdings") == 4

    def test_reingesting_the_same_zip_content_is_not_additive(self, isolated_store, tmp_path):
        """Same accessions arriving again (e.g. a re-downloaded zip under a new
        name) are deduped by the (accession, line_no) key rather than stacking on
        top of them. No DELETE runs."""
        from RevenueChaser.core import rawstore
        zip1, _ = _two_holder_zips(tmp_path)
        sf.ingest_zips([zip1])
        first = rawstore.count("sec13f_holdings")

        same_content_new_name = _write_zip(
            tmp_path / "2026q2_copy_form13f.zip",
            [{"ACCESSION_NUMBER": "acc1", "CIK": "1000", "FILING_DATE": "01-MAY-2026",
              "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "31-MAR-2026"}],
            [{"ACCESSION_NUMBER": "acc1", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
              "VALUE": "1000", "SSHPRNAMT": "100", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
             {"ACCESSION_NUMBER": "acc1", "NAMEOFISSUER": "MICROSOFT CORP", "CUSIP": "222222222",
              "VALUE": "500", "SSHPRNAMT": "50", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""}])

        deletes = []
        raw_delete = rawstore.execute

        def spy_delete(sql, *args, **kwargs):
            if isinstance(sql, str) and sql.strip().upper().startswith("DELETE"):
                deletes.append(sql)
            return raw_delete(sql, *args, **kwargs)

        rawstore.execute = spy_delete
        try:
            assert sf.ingest_zips([same_content_new_name]) == 1
        finally:
            rawstore.execute = raw_delete

        assert rawstore.count("sec13f_holdings") == first
        assert not deletes, "v12 ingest must not DELETE holdings"
        assert sf.compute_totals({"111111111": "AAPL"})["AAPL"]["2026-03-31"]["shares"] == 100.0

    def test_one_filing_may_list_the_same_cusip_twice(self, isolated_store, tmp_path):
        """So ``sec13f_holdings`` must NOT get an (accession, cusip) primary key —
        a filer legitimately reports the same issuer on several lines."""
        from RevenueChaser.core import rawstore
        z = _write_zip(
            tmp_path / "2026q3_form13f.zip",
            [{"ACCESSION_NUMBER": "accX", "CIK": "1000", "FILING_DATE": "01-MAY-2026",
              "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "31-MAR-2026"}],
            [{"ACCESSION_NUMBER": "accX", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
              "VALUE": "1000", "SSHPRNAMT": "100", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
             {"ACCESSION_NUMBER": "accX", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
              "VALUE": "2000", "SSHPRNAMT": "200", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""}])
        sf.ingest_zips([z])
        assert rawstore.count("sec13f_holdings") == 2
        assert sf.compute_totals({"111111111": "AAPL"})["AAPL"]["2026-03-31"]["shares"] == 300.0

    def test_zip_missing_infotable_is_skipped_not_marked_ingested(self, isolated_store, tmp_path):
        bad_zip = _write_zip(
            tmp_path / "broken_form13f.zip",
            [{"ACCESSION_NUMBER": "acc9", "CIK": "9999", "FILING_DATE": "01-MAY-2026",
              "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "31-MAR-2026"}],
            infotable_rows=None)

        assert sf.ingest_zips([bad_zip]) == 0
        # not marked ingested, so a later re-run with a fixed zip of the same name would retry
        assert sf.ingest_zips([bad_zip]) == 0

    def test_amendment_supersedes_original_in_compute_totals(self, isolated_store, tmp_path):
        original = _write_zip(
            tmp_path / "orig_form13f.zip",
            [{"ACCESSION_NUMBER": "acc1", "CIK": "1000", "FILING_DATE": "01-MAY-2026",
              "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "31-MAR-2026"}],
            [{"ACCESSION_NUMBER": "acc1", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
              "VALUE": "1000", "SSHPRNAMT": "100", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""}])
        amendment = _write_zip(
            tmp_path / "amend_form13f.zip",
            [{"ACCESSION_NUMBER": "acc1a", "CIK": "1000", "FILING_DATE": "15-MAY-2026",
              "SUBMISSIONTYPE": "13F-HR/A", "PERIODOFREPORT": "31-MAR-2026"}],
            [{"ACCESSION_NUMBER": "acc1a", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
              "VALUE": "1500", "SSHPRNAMT": "150", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""}])

        sf.ingest_zips([original, amendment])
        totals = sf.compute_totals({"111111111": "AAPL"})
        # only the later amendment's 150 shares count, not 100 + 150
        assert totals["AAPL"]["2026-03-31"]["shares"] == 150.0
        assert totals["AAPL"]["2026-03-31"]["holders"] == 1

    def test_same_day_amendment_supersedes_original(self, isolated_store, tmp_path):
        """A same-day 13F-HR/A must win over its original.

        SEC filers routinely amend on the same day, and the original can carry a
        wildly wrong figure (e.g. value/shared columns swapped). The dedup key
        must not depend on query row order.
        """
        original = _write_zip(
            tmp_path / "orig_form13f.zip",
            [{"ACCESSION_NUMBER": "acc1", "CIK": "1000", "FILING_DATE": "24-AUG-2026",
              "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "30-JUN-2026"}],
            [{"ACCESSION_NUMBER": "acc1", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
              "VALUE": "22278223", "SSHPRNAMT": "6446426607", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""}])
        amendment = _write_zip(
            tmp_path / "amend_form13f.zip",
            [{"ACCESSION_NUMBER": "acc1a", "CIK": "1000", "FILING_DATE": "24-AUG-2026",
              "SUBMISSIONTYPE": "13F-HR/A", "PERIODOFREPORT": "30-JUN-2026"}],
            [{"ACCESSION_NUMBER": "acc1a", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
              "VALUE": "6446426607", "SSHPRNAMT": "22278223", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""}])

        sf.ingest_zips([original, amendment])
        totals = sf.compute_totals({"111111111": "AAPL"})
        assert totals["AAPL"]["2026-06-30"]["shares"] == 22278223.0
        assert totals["AAPL"]["2026-06-30"]["value"] == 6446426607.0
        assert totals["AAPL"]["2026-06-30"]["holders"] == 1


class TestComputeTotals:
    def test_multi_holder_metrics(self, isolated_store, tmp_path):
        zip1, zip2 = _two_holder_zips(tmp_path)
        sf.ingest_zips([zip1, zip2])

        cusip_ticker = {"111111111": "AAPL", "222222222": "MSFT"}
        totals = sf.compute_totals(cusip_ticker)

        assert totals["AAPL"]["2026-03-31"]["shares"] == 300.0
        assert totals["AAPL"]["2026-03-31"]["holders"] == 2
        assert totals["AAPL"]["2026-03-31"]["value"] == 3000.0
        assert totals["MSFT"]["2026-03-31"]["shares"] == 50.0

    def test_periods_before_start_quarter_are_excluded(self, isolated_store, tmp_path):
        old_zip = _write_zip(
            tmp_path / "old_form13f.zip",
            [{"ACCESSION_NUMBER": "acc_old", "CIK": "5000", "FILING_DATE": "01-MAY-2024",
              "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "31-DEC-2024"}],
            [{"ACCESSION_NUMBER": "acc_old", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
              "VALUE": "1000", "SSHPRNAMT": "100", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""}])
        sf.ingest_zips([old_zip])
        totals = sf.compute_totals({"111111111": "AAPL"})
        assert totals == {}  # 2024-12-31 predates START_QUARTER (2025-03-31)


class TestUnmatchedSummary:
    """DF89: CUSIPs with no ticker are dropped from the totals; count what is left out."""

    def test_counts_cusips_without_a_ticker_in_the_latest_period(self, isolated_store, tmp_path):
        zip1, zip2 = _two_holder_zips(tmp_path)
        sf.ingest_zips([zip1, zip2])
        out = sf.unmatched_summary({"111111111": "AAPL"})
        assert out["period"] == "2026-03-31"
        assert out["cusips"] == 1                    # MSFT's CUSIP has no ticker
        assert out["value"] == 500.0
        assert out["top"] == [("MICROSOFT CORP", 500.0)]

    def test_only_the_latest_period_is_counted(self, isolated_store, tmp_path):
        zip1, _ = _two_holder_zips(tmp_path)   # 2026-03-31: AAPL + MSFT
        newer = _write_zip(
            tmp_path / "2026q3_form13f.zip",
            [{"ACCESSION_NUMBER": "acc9", "CIK": "1000", "FILING_DATE": "01-AUG-2026",
              "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "30-JUN-2026"}],
            [{"ACCESSION_NUMBER": "acc9", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
              "VALUE": "1000", "SSHPRNAMT": "100", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""}])
        sf.ingest_zips([zip1, newer])
        out = sf.unmatched_summary({"111111111": "AAPL"})
        assert out["period"] == "2026-06-30" and out["cusips"] == 0  # MSFT is Q1 only

    def test_nothing_unmatched_is_empty(self, isolated_store, tmp_path):
        zip1, zip2 = _two_holder_zips(tmp_path)
        sf.ingest_zips([zip1, zip2])
        out = sf.unmatched_summary({"111111111": "AAPL", "222222222": "MSFT"})
        assert out["cusips"] == 0 and out["top"] == []

    def test_a_dict_entry_counts_as_mapped(self, isolated_store, tmp_path):
        zip1, zip2 = _two_holder_zips(tmp_path)
        sf.ingest_zips([zip1, zip2])
        out = sf.unmatched_summary({"111111111": "AAPL", "222222222": {"ticker": "MSFT", "source": "openfigi"}})
        assert out["cusips"] == 0

    def test_empty_store_is_none(self, isolated_store):
        assert sf.unmatched_summary({"111111111": "AAPL"}) is None

    def test_note_text_names_count_value_and_top_issuer(self):
        note = sf.unmatched_note({"period": "2026-03-31", "cusips": 3, "value": 2.5e9,
                                  "top": [("MICROSOFT CORP", 2e9)]})
        assert "3" in note and "$2.50B" in note and "2026-03-31" in note
        assert "MICROSOFT CORP" in note and "ambiguous" not in note
        assert sf.unmatched_note({"cusips": 0}) is None
        assert sf.unmatched_note(None) is None


class TestBuildSec13f:
    def test_end_to_end_ingest_and_totals_with_the_stored_map(self, isolated_store, tmp_path):
        zip1, zip2 = _two_holder_zips(tmp_path)
        sf.save_cusip_ticker({"111111111": "AAPL", "222222222": "MSFT"})

        totals = sf.build_sec13f([zip1, zip2])

        assert totals["AAPL"]["2026-03-31"]["shares"] == 300.0
        assert totals["MSFT"]["2026-03-31"]["shares"] == 50.0
        assert sf.load_totals() == totals
        assert sf.load_cusip_ticker() == {"111111111": "AAPL", "222222222": "MSFT"}

    def test_the_stored_map_is_the_only_map(self, isolated_store, tmp_path):
        """#207: no issuer-name match fills a CUSIP the stored map lacks."""
        zip1, zip2 = _two_holder_zips(tmp_path)
        sf.save_cusip_ticker({"111111111": "AAPL2"})

        totals = sf.build_sec13f([zip1, zip2])

        assert set(totals) == {"AAPL2"}
        assert sf.load_cusip_ticker() == {"111111111": "AAPL2"}


# Totals the pre-O-1 ``compute_totals`` produced on ``_two_quarter_zips`` (captured
# from the code before the SQL was rewritten). The O6 rewrite must not change a figure.
GOLDEN_TOTALS = {
    "AAPL": {
        "2026-03-31": {"buy_flow": None, "exited_holders": None, "hhi": 5421.4, "holders": 2,
                       "new_holders": None, "sell_flow": None, "shares": 310.0,
                       "top10_pct": 100.0, "value": 3050.0},
        "2026-06-30": {"buy_flow": 110.0, "exited_holders": 1, "hhi": 5661.2, "holders": 2,
                       "new_holders": 1, "sell_flow": 200.0, "shares": 220.0,
                       "top10_pct": 100.0, "value": 2200.0},
    },
    "MSFT": {
        "2026-03-31": {"buy_flow": None, "exited_holders": None, "hhi": 10000.0, "holders": 1,
                       "new_holders": None, "sell_flow": None, "shares": 40.0,
                       "top10_pct": 100.0, "value": 400.0},
        "2026-06-30": {"buy_flow": 20.0, "exited_holders": 0, "hhi": 10000.0, "holders": 1,
                       "new_holders": 0, "sell_flow": 0.0, "shares": 60.0,
                       "top10_pct": 100.0, "value": 600.0},
    },
}
CUSIP_MAP = {"111111111": "AAPL", "111111112": "AAPL", "222222222": "MSFT"}


class TestComputeTotalsGolden:
    def test_sql_totals_equal_the_pre_rewrite_output(self, isolated_store, tmp_path):
        """O6: amendments, churn, a two-CUSIP ticker, PRN/option noise and a 13F-NT."""
        q1, q2 = _two_quarter_zips(tmp_path)
        sf.ingest_zips([q1, q2])
        assert sf.compute_totals(CUSIP_MAP) == GOLDEN_TOTALS

    def test_no_python_side_temp_table_inserts(self, isolated_store, tmp_path):
        """O6: the active/mapped sets reach DuckDB as one statement each, not row by row."""
        import inspect
        assert "executemany" not in inspect.getsource(sf.compute_totals)


class TestRecomputeTotals:
    """The derive stage: store only, no network."""

    def test_rebuilds_totals_from_the_store_with_no_network(self, isolated_store, tmp_path,
                                                            monkeypatch):
        from RevenueChaser.core.providers import sec_edgar
        q1, q2 = _two_quarter_zips(tmp_path)
        sf.ingest_zips([q1, q2])
        sf.save_totals({"STALE": {"2026-03-31": {"shares": 1.0}}})
        for name in ("get_text", "get_bytes"):
            monkeypatch.setattr(sec_edgar, name,
                                lambda *a, **k: pytest.fail("recompute reached the SEC"))
        monkeypatch.setattr("httpx.Client.send",
                            lambda *a, **k: pytest.fail("recompute reached the network"))

        sf.save_cusip_ticker(CUSIP_MAP)
        totals = sf.recompute_totals()

        assert set(totals) == {"AAPL", "MSFT"}  # the stored map found both, STALE is gone
        assert totals["AAPL"]["2026-06-30"]["shares"] == 150.0 + 70.0
        assert sf.load_totals() == totals

    def test_a_given_map_is_used_instead_of_the_stored_one(self, isolated_store, tmp_path):
        q1, q2 = _two_quarter_zips(tmp_path)
        sf.ingest_zips([q1, q2])
        sf.save_cusip_ticker({"111111111": "OLD"})
        assert set(sf.recompute_totals(CUSIP_MAP)) == {"AAPL", "MSFT"}
        assert sf.load_cusip_ticker() == CUSIP_MAP

    def test_keeps_stored_totals_when_there_is_nothing_to_derive_from(self, isolated_store):
        """An empty holdings table must not wipe the board's totals."""
        sf.save_totals({"NVDA": {"2026-03-31": {"shares": 5.0, "holders": 1}}})
        sf.save_cusip_ticker({"67066G104": "NVDA"})
        assert sf.recompute_totals() == {}
        assert "NVDA" in sf.load_totals()


class TestPartialIngestNeverReachesTotals:
    """B1 / v12: a half-written re-ingest must not create orphans or corrupt totals.

    With the (accession, line_no) key there is no per-accession DELETE. A crash
    during re-ingest leaves the previous good submissions and holdings in place,
    so the derive still sees a consistent (if stale) state.
    """

    def _crash_reingest(self, tmp_path, monkeypatch):
        """Ingest fully, drop the marker (as a forced re-ingest would), die on the 2nd chunk."""
        from RevenueChaser.core import rawstore
        zip1, zip2 = _two_holder_zips(tmp_path)
        sf.ingest_zips([zip1, zip2])
        sf.save_cusip_ticker({"111111111": "AAPL", "222222222": "MSFT"})
        good = sf.recompute_totals()
        rawstore.execute("DELETE FROM sec13f_ingested_zips")
        monkeypatch.setattr(sf, "_HOLDINGS_CHUNK", 1)
        real, calls = rawstore.write_rows, []

        def die_second(name, rows):
            if name == "sec13f_holdings":
                calls.append(1)
                if len(calls) == 2:
                    raise RuntimeError("chunk stream died")
            return real(name, rows)

        monkeypatch.setattr(rawstore, "write_rows", die_second)
        with pytest.raises(RuntimeError, match="chunk stream died"):
            sf.ingest_zips([zip1])
        monkeypatch.setattr(rawstore, "write_rows", real)
        return good

    def test_reingest_keeps_the_zips_old_submissions_after_a_crash(
            self, isolated_store, tmp_path, monkeypatch):
        """v12: no DELETE runs, so the previous submission rows survive the crash."""
        from RevenueChaser.core import rawstore
        self._crash_reingest(tmp_path, monkeypatch)
        accs = {r["accession"] for r in rawstore.query("SELECT accession FROM sec13f_submissions")}
        assert {"acc1", "acc2"} <= accs

    def test_recompute_still_succeeds_after_a_crash_and_keeps_totals(
            self, isolated_store, tmp_path, monkeypatch):
        good = self._crash_reingest(tmp_path, monkeypatch)
        totals = sf.recompute_totals()
        assert totals == good
        assert sf.load_totals() == good

    def test_a_clean_rerun_recovers(self, isolated_store, tmp_path, monkeypatch):
        self._crash_reingest(tmp_path, monkeypatch)
        monkeypatch.undo()
        assert sf.ingest_zips([tmp_path / "2026q2_form13f.zip"]) == 1
        totals = sf.recompute_totals()
        assert totals["AAPL"]["2026-03-31"]["shares"] == 300.0


class TestOrphanQueryFilter:
    def test_holdings_without_a_cik_and_without_a_submission_do_not_refuse(
            self, isolated_store, tmp_path):
        """The orphan check mirrors compute_totals: a row with an empty cik is never
        counted by the derive, so it must not block it either."""
        from RevenueChaser.core import rawstore
        zip1, _ = _two_holder_zips(tmp_path)
        sf.ingest_zips([zip1])
        rawstore.write_rows("sec13f_holdings", [
            {"accession": "ghost", "line_no": 1, "cusip": "111111111", "cik": "",
             "shares": 5.0, "value": 1.0}])
        assert sf._orphan_holdings() == 0
        sf.save_cusip_ticker({"111111111": "AAPL"})
        assert sf.recompute_totals()["AAPL"]["2026-03-31"]["shares"] == 100.0


class TestShrinkGuard:
    def _stored(self, n):
        return {f"T{i}": {"2026-03-31": {"shares": 1.0, "value": 1.0, "holders": 1}}
                for i in range(n)}

    def test_a_derive_far_smaller_than_the_stored_totals_is_refused(
            self, isolated_store, tmp_path):
        sf.save_totals(self._stored(20))
        zip1, _ = _two_holder_zips(tmp_path)
        sf.ingest_zips([zip1])
        sf.save_cusip_ticker({"111111111": "AAPL"})
        with pytest.raises(sf.DeriveRefused, match="shrink"):
            sf.recompute_totals()
        assert len(sf.load_totals()) == 20

    def test_force_overrides_the_guard(self, isolated_store, tmp_path):
        sf.save_totals(self._stored(20))
        zip1, _ = _two_holder_zips(tmp_path)
        sf.ingest_zips([zip1])
        sf.save_cusip_ticker({"111111111": "AAPL"})
        assert set(sf.recompute_totals(force=True)) == {"AAPL"}

    def test_small_stores_are_not_guarded(self, isolated_store, tmp_path):
        sf.save_totals(self._stored(3))
        zip1, _ = _two_holder_zips(tmp_path)
        sf.ingest_zips([zip1])
        sf.save_cusip_ticker({"111111111": "AAPL"})
        assert set(sf.recompute_totals()) == {"AAPL"}

    def test_the_before_and_after_counts_are_logged(self, isolated_store, tmp_path, caplog):
        import logging
        zip1, _ = _two_holder_zips(tmp_path)
        sf.ingest_zips([zip1])
        sf.save_cusip_ticker({"111111111": "AAPL"})
        with caplog.at_level(logging.INFO):
            sf.recompute_totals()
        assert any("0 -> 1 tickers" in r.getMessage() for r in caplog.records)


class TestDeadPathsAreGone:
    """O2, O3, O17, O21, O22: the zip-path duplicates, OpenFIGI client and EDGAR backfill."""

    @pytest.mark.parametrize("name", [
        "load_meta", "save_meta", "purge_backfilled_periods", "zip_accessions_by_period",
        "ensure_cusip_ticker", "build_cusip_ticker_map", "OPENFIGI_URL", "SEC_HEADERS",
        "load_submissions", "active_accessions", "aggregate", "build_sec_series",
        "build_sec_series_from_agg", "cusip_to_names", "build_cusip_ticker_by_name",
        # #207: the issuer-name match is retired
        "build_cusip_ticker_by_name_db", "build_ticker_name_index", "load_ticker_names", "save_ticker_names",
    ])
    def test_removed(self, name):
        assert not hasattr(sf, name), f"engine.{name} should be gone"

    @pytest.mark.parametrize("name", [
        "filing_rank", "load_cusip_ticker", "save_cusip_ticker", "load_sec13f_meta",
        "save_sec13f_meta", "active_accessions_db", "map_candidates", "map_identity", "ticker_of",
    ])
    def test_live_code_is_kept(self, name):
        assert hasattr(sf, name), f"engine.{name} is used and must stay"

    def test_edgar_backfill_module_is_gone(self):
        import importlib
        with pytest.raises(ImportError):
            importlib.import_module("RevenueChaser.module_sec_13f.edgar_backfill")

    def test_report_does_not_read_the_qa_file(self):
        import inspect
        from RevenueChaser.module_sec_13f import report
        src = inspect.getsource(report)
        assert "edgar_backfill" not in src and "qa_" not in src


class TestAmendments:
    """S3: RESTATEMENT replaces, NEW HOLDINGS unions onto the active set."""

    def _make_original(self, tmp_path, cik="1000", period="31-MAR-2026",
                       filed="01-MAY-2026", acc="orig"):
        return _write_zip(
            tmp_path / f"{acc}_form13f.zip",
            [_sub(acc, cik, filed, "13F-HR", period)],
            [_row(acc, "ALPHA INC", "111111111", "100", "1000")],
            coverpage_rows=[_cover(acc, period, "13F-HR", "", "", "")])

    def _make_amendment(self, tmp_path, acc, cik, period, filed, kind,
                        shares="50", value="500"):
        return _write_zip(
            tmp_path / f"{acc}_form13f.zip",
            [_sub(acc, cik, filed, "13F-HR/A", period)],
            [_row(acc, "ALPHA INC", "111111111", shares, value)],
            coverpage_rows=[_cover(acc, period, "13F-HR/A", "Y", "1", kind)])

    def test_new_holdings_unions_with_original(self, isolated_store, tmp_path):
        orig = self._make_original(tmp_path)
        amend = self._make_amendment(tmp_path, "amend", "1000", "31-MAR-2026",
                                     "15-MAY-2026", "NEW HOLDINGS")
        sf.ingest_zips([orig, amend])
        sf.save_cusip_ticker({"111111111": "A"})
        totals = sf.recompute_totals()
        assert totals["A"]["2026-03-31"]["shares"] == 150.0
        assert totals["A"]["2026-03-31"]["holders"] == 1

    def test_restatement_replaces_original(self, isolated_store, tmp_path):
        orig = self._make_original(tmp_path)
        amend = self._make_amendment(tmp_path, "amend", "1000", "31-MAR-2026",
                                     "15-MAY-2026", "RESTATEMENT", "200", "2000")
        sf.ingest_zips([orig, amend])
        sf.save_cusip_ticker({"111111111": "A"})
        totals = sf.recompute_totals()
        assert totals["A"]["2026-03-31"]["shares"] == 200.0
        assert totals["A"]["2026-03-31"]["holders"] == 1

    def test_chain_new_holdings_then_restatement(self, isolated_store, tmp_path):
        orig = self._make_original(tmp_path, acc="o")
        nh = self._make_amendment(tmp_path, "nh", "1000", "31-MAR-2026",
                                  "15-MAY-2026", "NEW HOLDINGS", "50", "500")
        rest = self._make_amendment(tmp_path, "rest", "1000", "31-MAR-2026",
                                    "20-MAY-2026", "RESTATEMENT", "75", "750")
        sf.ingest_zips([orig, nh, rest])
        sf.save_cusip_ticker({"111111111": "A"})
        totals = sf.recompute_totals()
        assert totals["A"]["2026-03-31"]["shares"] == 75.0

    def test_chain_restatement_then_new_holdings(self, isolated_store, tmp_path):
        orig = self._make_original(tmp_path, acc="o")
        rest = self._make_amendment(tmp_path, "rest", "1000", "31-MAR-2026",
                                    "15-MAY-2026", "RESTATEMENT", "75", "750")
        nh = self._make_amendment(tmp_path, "nh", "1000", "31-MAR-2026",
                                  "20-MAY-2026", "NEW HOLDINGS", "50", "500")
        sf.ingest_zips([orig, rest, nh])
        sf.save_cusip_ticker({"111111111": "A"})
        totals = sf.recompute_totals()
        assert totals["A"]["2026-03-31"]["shares"] == 125.0

    def test_empty_amendment_type_replaces(self, isolated_store, tmp_path):
        orig = self._make_original(tmp_path)
        amend = _write_zip(
            tmp_path / "amend_form13f.zip",
            [_sub("amend", "1000", "15-MAY-2026", "13F-HR/A", "31-MAR-2026")],
            [_row("amend", "ALPHA INC", "111111111", "200", "2000")],
            coverpage_rows=[{"ACCESSION_NUMBER": "amend",
                             "REPORTCALENDARORQUARTER": "31-MAR-2026",
                             "ISAMENDMENT": "Y", "AMENDMENTNO": "1",
                             "AMENDMENTTYPE": "", "REPORTTYPE": "13F-HR/A"}])
        sf.ingest_zips([orig, amend])
        sf.save_cusip_ticker({"111111111": "A"})
        totals = sf.recompute_totals()
        assert totals["A"]["2026-03-31"]["shares"] == 200.0

    def test_same_filing_date_ordered_by_amendment_no(self, isolated_store, tmp_path):
        """Two /A on one FILING_DATE: AMENDMENTNO decides, and a union-then-replace chain proves it.

        ``amendment 1`` restates (300 shares), ``amendment 2`` adds 50. Ordered by AMENDMENTNO the
        restatement lands first and the addition unions onto it (350). Ordered by accession instead
        — the reverse of AMENDMENTNO on purpose — the addition lands first and the restatement
        wipes it (300), so this test fails when AMENDMENTNO is dropped from the order.
        """
        self._make_original(tmp_path, acc="o", filed="01-MAY-2026")
        rest = _write_zip(
            tmp_path / "b_rest_form13f.zip",
            [_sub("b-rest", "1000", "15-MAY-2026", "13F-HR/A", "31-MAR-2026")],
            [_row("b-rest", "ALPHA INC", "111111111", "300", "3000")],
            coverpage_rows=[_cover("b-rest", "31-MAR-2026", "13F-HR/A", "Y", "1", "RESTATEMENT")])
        add = _write_zip(
            tmp_path / "a_add_form13f.zip",
            [_sub("a-add", "1000", "15-MAY-2026", "13F-HR/A", "31-MAR-2026")],
            [_row("a-add", "ALPHA INC", "111111111", "50", "500")],
            coverpage_rows=[_cover("a-add", "31-MAR-2026", "13F-HR/A", "Y", "2", "NEW HOLDINGS")])
        sf.ingest_zips([tmp_path / "o_form13f.zip", rest, add])
        sf.save_cusip_ticker({"111111111": "A"})
        totals = sf.recompute_totals()
        assert totals["A"]["2026-03-31"]["shares"] == 350.0

    def test_non_amendment_with_new_holdings_resets_active_set(self, isolated_store, tmp_path):
        """A 13F-HR (no /A) carrying AMENDMENTTYPE=NEW HOLDINGS still replaces."""
        orig = self._make_original(tmp_path, acc="o")
        weird = _write_zip(
            tmp_path / "weird_form13f.zip",
            [_sub("weird", "1000", "15-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("weird", "ALPHA INC", "111111111", "60", "600")],
            coverpage_rows=[{"ACCESSION_NUMBER": "weird",
                             "REPORTCALENDARORQUARTER": "31-MAR-2026",
                             "ISAMENDMENT": "N", "AMENDMENTNO": "",
                             "AMENDMENTTYPE": "NEW HOLDINGS", "REPORTTYPE": "13F-HR"}])
        sf.ingest_zips([orig, weird])
        sf.save_cusip_ticker({"111111111": "A"})
        totals = sf.recompute_totals()
        assert totals["A"]["2026-03-31"]["shares"] == 60.0

    def test_notice_filings_contribute_no_holdings(self, isolated_store, tmp_path):
        z = _write_zip(
            tmp_path / "nt_form13f.zip",
            [_sub("nt1", "1000", "01-MAY-2026", "13F-NT", "31-MAR-2026"),
             _sub("nt1a", "1000", "15-MAY-2026", "13F-NT/A", "31-MAR-2026")],
            [_row("nt1", "ALPHA INC", "111111111", "100", "1000")])
        sf.ingest_zips([z])
        sf.save_cusip_ticker({"111111111": "A"})
        assert sf.recompute_totals() == {}
        assert sf.active_accessions_db() == set()


class TestNonLossyIngest:
    """S2: every INFOTABLE row is stored; the derive filters long equity."""

    def test_put_and_prn_rows_are_stored_but_excluded(self, isolated_store, tmp_path):
        z = _write_zip(
            tmp_path / "nonlossy_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("a", "ALPHA INC", "111111111", "100", "1000", typ="SH"),
             _row("a", "ALPHA INC", "111111111", "99", "99", putcall="PUT"),
             _row("a", "ALPHA INC", "111111111", "88", "88", typ="PRN")])
        sf.ingest_zips([z])
        from RevenueChaser.core import rawstore
        assert rawstore.count("sec13f_holdings") == 3
        sf.save_cusip_ticker({"111111111": "A"})
        totals = sf.recompute_totals()
        assert totals["A"]["2026-03-31"]["shares"] == 100.0
        assert totals["A"]["2026-03-31"]["value"] == 1000.0

    def test_line_no_stable_on_reingest(self, isolated_store, tmp_path):
        z = _write_zip(
            tmp_path / "stable_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("a", "ALPHA INC", "111111111", "100", "1000", line_no=1),
             _row("a", "BETA INC", "222222222", "200", "2000", line_no=2)])
        sf.ingest_zips([z])
        from RevenueChaser.core import rawstore
        first = rawstore.count("sec13f_holdings")
        # Simulate the v12 reset minus the DROP: the marker goes, the holdings stay, so the
        # second ingest really re-inserts the same (accession, line_no) rows.
        rawstore.execute("DELETE FROM sec13f_ingested_zips")
        deletes = []
        raw_delete = rawstore.execute

        def spy_delete(sql, *args, **kwargs):
            if isinstance(sql, str) and sql.strip().upper().startswith("DELETE"):
                deletes.append(sql)
            return raw_delete(sql, *args, **kwargs)

        rawstore.execute = spy_delete
        try:
            assert sf.ingest_zips([z]) == 1
        finally:
            rawstore.execute = raw_delete
        assert rawstore.count("sec13f_holdings") == first
        assert not deletes

    def test_missing_coverpage_ingests_with_null_columns(self, isolated_store, tmp_path, caplog):
        z = _write_zip(
            tmp_path / "nocover_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("a", "ALPHA INC", "111111111", "100", "1000")])
        import logging
        with caplog.at_level(logging.WARNING):
            sf.ingest_zips([z])
        from RevenueChaser.core import rawstore
        row = rawstore.query("SELECT * FROM sec13f_submissions WHERE accession='a'")[0]
        assert row["report_type"] is None
        assert row["is_amendment"] is None
        assert row["amendment_type"] is None
        assert row["other_managers_count"] is None
        assert any("No COVERPAGE.tsv" in r.getMessage() for r in caplog.records)

    def test_option_only_cusip_is_not_named(self, isolated_store, tmp_path):
        z = _write_zip(
            tmp_path / "optiononly_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("a", "ALPHA INC", "111111111", "99", "99", putcall="PUT")])
        sf.ingest_zips([z])
        from RevenueChaser.core import rawstore
        assert rawstore.count("sec13f_cusip_names") == 0

    def test_summarypage_populates_other_managers_count(self, isolated_store, tmp_path):
        z = _write_zip(
            tmp_path / "summary_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("a", "ALPHA INC", "111111111", "100", "1000")],
            coverpage_rows=[_cover("a", "31-MAR-2026")],
            summarypage_rows=[_summary("a", "7")])
        sf.ingest_zips([z])
        from RevenueChaser.core import rawstore
        row = rawstore.query("SELECT * FROM sec13f_submissions WHERE accession='a'")[0]
        assert row["other_managers_count"] == 7

class TestValueUnits:
    """S4: VALUE thousands are detected from D01 closes and rescaled."""

    def _bar(self, ticker, session_date, close):
        return {"ticker": ticker, "session_date": session_date, "close": close,
                "source": "test"}

    def _filings_for_scale(self, tmp_path, cik="1000", period="31-MAR-2026",
                            ticker_cusip="111111111", shares="1000",
                            value="100", n_names=5):
        rows = []
        for i in range(n_names):
            cusip = f"{ticker_cusip[:-3]}{i:03d}"
            rows.append(_row("a", f"NAME{i}", cusip, shares, value, line_no=i + 1))
        z = _write_zip(
            tmp_path / "scale_form13f.zip",
            [_sub("a", cik, "01-MAY-2026", "13F-HR", period)],
            rows)
        # map each cusip to the same ticker
        cusip_map = {f"{ticker_cusip[:-3]}{i:03d}": "TICK" for i in range(n_names)}
        return z, cusip_map

    def test_rescale_when_implied_price_is_one_thousandth(self, isolated_store, tmp_path):
        from RevenueChaser.core import rawstore
        z, cmap = self._filings_for_scale(tmp_path, value="100", shares="1000", n_names=5)
        rawstore.write_rows("price_bars_daily", [
            self._bar("TICK", "2026-03-31", 100.0)])
        sf.ingest_zips([z])
        sf.save_cusip_ticker(cmap)
        totals = sf.recompute_totals()
        assert totals["TICK"]["2026-03-31"]["value"] == 500_000.0
        assert totals["TICK"]["2026-03-31"]["shares"] == 5000.0
        assert sf.value_scale_summary()["rescaled"] == 1

    def test_no_rescale_when_implied_price_matches_close(self, isolated_store, tmp_path):
        from RevenueChaser.core import rawstore
        z, cmap = self._filings_for_scale(tmp_path, value="100000", shares="1000", n_names=5)
        rawstore.write_rows("price_bars_daily", [
            self._bar("TICK", "2026-03-31", 100.0)])
        sf.ingest_zips([z])
        sf.save_cusip_ticker(cmap)
        totals = sf.recompute_totals()
        assert totals["TICK"]["2026-03-31"]["value"] == 500_000.0
        assert sf.value_scale_summary()["rescaled"] == 0

    def test_not_rescaled_with_too_few_names(self, isolated_store, tmp_path):
        from RevenueChaser.core import rawstore
        z, cmap = self._filings_for_scale(tmp_path, value="100", shares="1000", n_names=4)
        rawstore.write_rows("price_bars_daily", [
            self._bar("TICK", "2026-03-31", 100.0)])
        sf.ingest_zips([z])
        sf.save_cusip_ticker(cmap)
        totals = sf.recompute_totals()
        assert totals["TICK"]["2026-03-31"]["value"] == 400.0
        assert sf.value_scale_summary()["rescaled"] == 0

    def test_no_prices_skips_check(self, isolated_store, tmp_path):
        z, cmap = self._filings_for_scale(tmp_path, value="100", shares="1000", n_names=5)
        sf.ingest_zips([z])
        sf.save_cusip_ticker(cmap)
        totals = sf.recompute_totals()
        assert totals["TICK"]["2026-03-31"]["value"] == 500.0
        assert sf.value_scale_summary()["checked"] == 0


class TestHolders:
    """S5: holder counts stay correct."""

    def test_other_manager_name_is_not_a_holder(self, isolated_store, tmp_path):
        z = _write_zip(
            tmp_path / "othermgr_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("a", "ALPHA INC", "111111111", "100", "1000",
                  other_manager="9999")])
        sf.ingest_zips([z])
        sf.save_cusip_ticker({"111111111": "A"})
        totals = sf.recompute_totals()
        assert totals["A"]["2026-03-31"]["holders"] == 1

    def test_new_holdings_by_same_cik_counts_once(self, isolated_store, tmp_path):
        orig = _write_zip(
            tmp_path / "orig_form13f.zip",
            [_sub("o", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("o", "ALPHA INC", "111111111", "100", "1000")],
            coverpage_rows=[_cover("o", "31-MAR-2026")])
        nh = _write_zip(
            tmp_path / "nh_form13f.zip",
            [_sub("nh", "1000", "15-MAY-2026", "13F-HR/A", "31-MAR-2026")],
            [_row("nh", "BETA INC", "222222222", "50", "500")],
            coverpage_rows=[_cover("nh", "31-MAR-2026", "13F-HR/A", "Y", "1", "NEW HOLDINGS")])
        sf.ingest_zips([orig, nh])
        sf.save_cusip_ticker({"111111111": "A", "222222222": "B"})
        totals = sf.recompute_totals()
        assert totals["A"]["2026-03-31"]["holders"] == 1
        assert totals["B"]["2026-03-31"]["holders"] == 1
