"""Tests for the SEC Form 13F ZIP -> raw.duckdb ingest pipeline (engine.py)."""
import csv
import io
import zipfile

import pytest

from RevenueChaser.module_sec_13f import engine as sf


def _write_zip(path, submission_rows, infotable_rows=None):
    """Build a synthetic ``*_form13f.zip`` with SUBMISSION.tsv (+ optional INFOTABLE.tsv)."""
    with zipfile.ZipFile(path, "w") as z:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=[
            "ACCESSION_NUMBER", "CIK", "FILING_DATE", "SUBMISSIONTYPE", "PERIODOFREPORT"],
            delimiter="\t")
        w.writeheader()
        w.writerows(submission_rows)
        z.writestr("SUBMISSION.tsv", buf.getvalue())

        if infotable_rows is not None:
            buf2 = io.StringIO()
            w2 = csv.DictWriter(buf2, fieldnames=[
                "ACCESSION_NUMBER", "NAMEOFISSUER", "CUSIP", "VALUE",
                "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"], delimiter="\t")
            w2.writeheader()
            w2.writerows(infotable_rows)
            z.writestr("INFOTABLE.tsv", buf2.getvalue())
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


def _row(acc, name, cusip, shares, value="1000", typ="SH", putcall=""):
    return {"ACCESSION_NUMBER": acc, "NAMEOFISSUER": name, "CUSIP": cusip, "VALUE": value,
            "SSHPRNAMT": shares, "SSHPRNAMTTYPE": typ, "PUTCALL": putcall}


def _sub(acc, cik, filed, kind, period):
    return {"ACCESSION_NUMBER": acc, "CIK": cik, "FILING_DATE": filed,
            "SUBMISSIONTYPE": kind, "PERIODOFREPORT": period}


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
        zip — and ``sec13f_holdings`` is append-only with no key, so every
        share/value/holder figure would DOUBLE. The zip's own rows are cleared
        before they are re-inserted.
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

        # the resumed run must REPLACE those rows, not append a second copy
        assert sf.ingest_zips([zip1]) == 1
        assert rawstore.count("sec13f_holdings") == partial

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
        """The zip is streamed, not held in memory: a small chunk size splits the write."""
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
        assert seen == [1, 1] and rawstore.count("sec13f_holdings") == 2

    def test_reingesting_the_same_zip_content_is_not_additive(self, isolated_store, tmp_path):
        """Same accessions arriving again (e.g. a re-downloaded zip under a new
        name) replace their rows rather than stacking on top of them."""
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
        assert sf.ingest_zips([same_content_new_name]) == 1
        assert rawstore.count("sec13f_holdings") == first
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
        assert sf.recompute_totals() is None
        assert "NVDA" in sf.load_totals()


class TestPartialIngestNeverReachesTotals:
    """B1: a half-written zip must not replace good totals with partial ones."""

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

    def test_reingest_drops_the_zips_old_submissions_with_its_holdings(
            self, isolated_store, tmp_path, monkeypatch):
        from RevenueChaser.core import rawstore
        self._crash_reingest(tmp_path, monkeypatch)
        accs = {r["accession"] for r in rawstore.query("SELECT accession FROM sec13f_submissions")}
        assert "acc1" not in accs and "acc2" in accs

    def test_recompute_refuses_orphan_holdings_and_keeps_the_totals(
            self, isolated_store, tmp_path, monkeypatch):
        good = self._crash_reingest(tmp_path, monkeypatch)
        with pytest.raises(sf.DeriveRefused, match="orphan"):
            sf.recompute_totals()
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
            {"accession": "ghost", "cusip": "111111111", "cik": "", "shares": 5.0, "value": 1.0}])
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
