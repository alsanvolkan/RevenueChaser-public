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


def _row(acc, name, cusip, shares, value="1000", typ="SH", putcall="", line_no=None,
         other_manager=""):
    row = {"ACCESSION_NUMBER": acc, "NAMEOFISSUER": name, "CUSIP": cusip, "VALUE": value,
           "SSHPRNAMT": shares, "SSHPRNAMTTYPE": typ, "PUTCALL": putcall,
           "TITLEOFCLASS": "COM", "INVESTMENTDISCRETION": "SOLE", "OTHERMANAGER": other_manager,
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


class TestAmendmentsV12:
    """brief.md §3 tests 5 and 21-22, the S3 walk's conservative and normalised branches."""

    def test_unknown_amendment_type_replaces(self, store, tmp_path):
        """brief.md §1 S3: an unrecognised AMENDMENTTYPE never unions on a guess."""
        orig = _write_zip(
            tmp_path / "orig_form13f.zip",
            [_sub("o", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("o", "ALPHA INC", "111111111", "100", "1000")],
            coverpage_rows=[_cover("o", "31-MAR-2026")])
        amend = _write_zip(
            tmp_path / "amend_form13f.zip",
            [_sub("a", "1000", "15-MAY-2026", "13F-HR/A", "31-MAR-2026")],
            [_row("a", "ALPHA INC", "111111111", "40", "400")],
            coverpage_rows=[_cover("a", "31-MAR-2026", "13F-HR/A", "Y", "1", "REPLACES PRIOR")])
        engine.ingest_zips([orig, amend])
        engine.save_cusip_ticker({"111111111": "A"})
        totals = engine.recompute_totals()
        assert totals["A"]["2026-03-31"]["shares"] == 40.0

    def test_amendment_type_is_matched_after_case_and_whitespace_collapse(self, store, tmp_path):
        """brief.md §7: the real file spells it "NEW HOLDINGS"; case and runs of spaces must not matter."""
        orig = _write_zip(
            tmp_path / "orig_form13f.zip",
            [_sub("o", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("o", "ALPHA INC", "111111111", "100", "1000")],
            coverpage_rows=[_cover("o", "31-MAR-2026")])
        amend = _write_zip(
            tmp_path / "amend_form13f.zip",
            [_sub("a", "1000", "15-MAY-2026", "13F-HR/A", "31-MAR-2026")],
            [_row("a", "ALPHA INC", "111111111", "40", "400")],
            coverpage_rows=[_cover("a", "31-MAR-2026", "13F-HR/A", "Y", "1", "  new \t holdings ")])
        engine.ingest_zips([orig, amend])
        engine.save_cusip_ticker({"111111111": "A"})
        totals = engine.recompute_totals()
        assert totals["A"]["2026-03-31"]["shares"] == 140.0

    def test_amendment_on_the_same_day_wins_over_its_original(self, store, tmp_path):
        """Same FILING_DATE and no AMENDMENTNO: ``filing_rank`` still puts the /A last, so it survives."""
        orig = _write_zip(
            tmp_path / "orig_form13f.zip",
            # the original's accession sorts after the amendment's
            [_sub("z-orig", "1000", "15-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("z-orig", "ALPHA INC", "111111111", "100", "1000")],
            coverpage_rows=[_cover("z-orig", "31-MAR-2026")])
        amend = _write_zip(
            tmp_path / "amend_form13f.zip",
            [_sub("a-amend", "1000", "15-MAY-2026", "13F-HR/A", "31-MAR-2026")],
            [_row("a-amend", "ALPHA INC", "111111111", "40", "400")],
            coverpage_rows=[_cover("a-amend", "31-MAR-2026", "13F-HR/A", "Y", "", "NEW HOLDINGS")])
        engine.ingest_zips([orig, amend])
        engine.save_cusip_ticker({"111111111": "A"})
        totals = engine.recompute_totals()
        assert totals["A"]["2026-03-31"]["shares"] == 140.0


class TestValueUnitsV12:
    """S4: the VALUE-units check's edge cases (brief.md §3 tests 11-14 live in test_ingest.py)."""

    def _seed(self, tmp_path, *, closes):
        rows = [_row("a", f"NAME{i}", f"11111111{i}", "1000", "100", line_no=i + 1) for i in range(5)]
        z = _write_zip(
            tmp_path / "scale_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            rows)
        rawstore.write_rows("price_bars_daily", [
            {"ticker": "TICK", "session_date": d, "close": c, "source": "test"} for d, c in closes])
        engine.ingest_zips([z])
        engine.save_cusip_ticker({f"11111111{i}": "TICK" for i in range(5)})

    def test_summary_does_not_carry_the_previous_run(self, store, tmp_path):
        """A derive with nothing to derive from reports zero, not the last run's counts: no run state is kept."""
        self._seed(tmp_path, closes=[("2026-03-31", 100.0)])
        engine.recompute_totals()
        _totals, factors = engine.compute_totals(engine.load_cusip_ticker())
        assert engine.value_scale_summary(factors)["rescaled"] == 1
        rawstore.execute("DELETE FROM sec13f_holdings")
        rawstore.execute("DELETE FROM sec13f_submissions")
        totals, factors = engine.compute_totals(engine.load_cusip_ticker())
        assert totals == {}
        assert engine.value_scale_summary(factors) == {"checked": 0, "rescaled": 0, "periods": {}}
        assert not hasattr(engine.compute_totals, "_last_scale_factors"), "the factors are returned, not stored"

    def test_a_missing_final_close_falls_back_to_the_last_usable_one(self, store, tmp_path):
        """A halted name with a NULL close on the period end still has a reference: its last real close."""
        self._seed(tmp_path, closes=[("2026-03-30", 100.0), ("2026-03-31", None)])
        totals = engine.recompute_totals()
        assert totals["TICK"]["2026-03-31"]["value"] == 500_000.0
        _totals, factors = engine.compute_totals(engine.load_cusip_ticker())
        assert engine.value_scale_summary(factors)["rescaled"] == 1

    def test_five_rows_of_one_name_do_not_pass_the_name_floor(self, store, tmp_path):
        """``_VALUE_SCALE_MIN_NAMES`` counts distinct names (CUSIPs), not holdings rows: one name is not five."""
        rows = [_row("a", "ALPHA INC", "111111111", "1000", "100", line_no=i + 1) for i in range(5)]
        z = _write_zip(
            tmp_path / "one_name_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            rows)
        rawstore.write_rows("price_bars_daily", [
            {"ticker": "TICK", "session_date": "2026-03-31", "close": 100.0, "source": "test"}])
        engine.ingest_zips([z])
        engine.save_cusip_ticker({"111111111": "TICK"})
        totals = engine.recompute_totals()
        assert totals["TICK"]["2026-03-31"]["value"] == 500.0          # 5 x 100, not rescaled
        _totals, factors = engine.compute_totals(engine.load_cusip_ticker())
        assert set(factors.values()) == {1.0}

    def test_an_implied_price_merely_off_by_a_factor_is_not_read_as_thousands(self, store, tmp_path):
        """The band's upper edge is what makes the check specific: only ~1/1000 is thousands.

        Five mapped names reported in dollars, against a reference close twice their implied price (a
        stale close, or a name that halved during the quarter): ratio 0.5. That is nowhere near 1/1000,
        so the filer is left alone. Without the upper edge every ratio under 1 would be rescaled by
        1000, which would multiply an honest dollar filer's value by a thousand.
        """
        rows = [_row("a", f"NAME{i}", f"11111111{i}", "1000", "50000", line_no=i + 1) for i in range(5)]
        z = _write_zip(
            tmp_path / "dollars_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            rows)
        rawstore.write_rows("price_bars_daily", [
            {"ticker": "TICK", "session_date": "2026-03-31", "close": 100.0, "source": "test"}])
        engine.ingest_zips([z])
        engine.save_cusip_ticker({f"11111111{i}": "TICK" for i in range(5)})
        totals = engine.recompute_totals()
        assert totals["TICK"]["2026-03-31"]["value"] == 250_000.0       # 5 x 50,000, not rescaled
        _totals, factors = engine.compute_totals(engine.load_cusip_ticker())
        assert set(factors.values()) == {1.0}
        assert engine.value_scale_summary(factors)["rescaled"] == 0

    def test_a_ratio_under_the_lower_edge_is_not_read_as_thousands(self, store, tmp_path):
        """The band is bounded below too: a ratio far under 1/1000 is not a units error either.

        Five names whose implied price is ~1/30,000 of the close. Reading that as "thousands" and
        multiplying by 1000 would still leave it 30x off, so it is a different problem (a bad
        close, a wrong CUSIP map) and the check declines to guess.
        """
        rows = [_row("a", f"NAME{i}", f"11111111{i}", "1000", "3", line_no=i + 1) for i in range(5)]
        z = _write_zip(
            tmp_path / "tiny_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            rows)
        rawstore.write_rows("price_bars_daily", [
            {"ticker": "TICK", "session_date": "2026-03-31", "close": 100.0, "source": "test"}])
        engine.ingest_zips([z])
        engine.save_cusip_ticker({f"11111111{i}": "TICK" for i in range(5)})
        totals = engine.recompute_totals()
        assert totals["TICK"]["2026-03-31"]["value"] == 15.0            # 5 x 3, not rescaled
        _totals, factors = engine.compute_totals(engine.load_cusip_ticker())
        assert set(factors.values()) == {1.0}

    def test_names_with_an_unparseable_value_do_not_count_toward_the_floor(self, store, tmp_path):
        """A NULL VALUE is skipped by median(), so it must not be counted as a name either.

        Five mapped names, four of them with VALUE "n/a" (stored NULL by ``_float_or_none``). The
        median would then rest on ONE name while the count said five, clearing
        ``_VALUE_SCALE_MIN_NAMES`` on exactly the one-off the floor exists to stop.
        """
        rows = [_row("a", "NAME0", "111111110", "1000", "100", line_no=1)]
        rows += [_row("a", f"NAME{i}", f"11111111{i}", "1000", "n/a", line_no=i + 1)
                 for i in range(1, 5)]
        z = _write_zip(
            tmp_path / "badvalue_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            rows)
        rawstore.write_rows("price_bars_daily", [
            {"ticker": "TICK", "session_date": "2026-03-31", "close": 100.0, "source": "test"}])
        engine.ingest_zips([z])
        engine.save_cusip_ticker({f"11111111{i}": "TICK" for i in range(5)})
        _totals, factors = engine.compute_totals(engine.load_cusip_ticker())
        assert set(factors.values()) == {1.0}, "one usable name is not five"

    def test_the_board_note_is_on_the_same_scale_as_the_totals(self, store, tmp_path):
        """MUST (round 2): ``_13f_unmatched`` prints dollars beside the totals, so it scales them.

        The runner used to call ``unmatched_summary`` with no factors, so a rescaled filer's
        "N CUSIPs with no ticker ($X held in ...)" line was 1000x smaller than the totals printed
        next to it.
        """
        from RevenueChaser.app import runners
        rows = [_row("a", f"NAME{i}", f"11111111{i}", "1000", "100", line_no=i + 1) for i in range(5)]
        rows.append(_row("a", "OMEGA INC", "999999999", "1000", "100", line_no=6))   # no ticker
        z = _write_zip(
            tmp_path / "note_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            rows)
        rawstore.write_rows("price_bars_daily", [
            {"ticker": "TICK", "session_date": "2026-03-31", "close": 100.0, "source": "test"}])
        engine.ingest_zips([z])
        engine.save_cusip_ticker({f"11111111{i}": "TICK" for i in range(5)})
        engine.recompute_totals()
        note, summary = runners._13f_unmatched()
        assert summary["value"] == 100_000.0, "the note's held value is on the raw basis"
        assert "$100,000" in note          # 100 raw x the filer's 1000 factor, as printed

    def test_held_value_and_the_unmatched_note_use_the_totals_scale(self, store, tmp_path):
        """MUST-2: a rescaled filer's held value and its left-out value are on the totals' basis, not the raw one."""
        rows = [_row("a", f"NAME{i}", f"11111111{i}", "1000", "100", line_no=i + 1) for i in range(5)]
        rows.append(_row("a", "OMEGA INC", "999999999", "1000", "100", line_no=6))   # no ticker
        z = _write_zip(
            tmp_path / "scale_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            rows)
        rawstore.write_rows("price_bars_daily", [
            {"ticker": "TICK", "session_date": "2026-03-31", "close": 100.0, "source": "test"}])
        engine.ingest_zips([z])
        cusip_map = {f"11111111{i}": "TICK" for i in range(5)}
        engine.save_cusip_ticker(cusip_map)

        totals, factors = engine.compute_totals(cusip_map)
        assert totals["TICK"]["2026-03-31"]["value"] == 500_000.0
        assert factors[("1000", "2026-03-31")] == 1000.0
        assert engine.held_value("2026-03-31", factors) == 600_000.0   # 6 x 100 on the totals' basis
        assert engine.held_value("2026-03-31") == 600.0                # the default: no scaling
        summary = engine.unmatched_summary(cusip_map, factors)
        assert summary["cusips"] == 1 and summary["value"] == 100_000.0
        assert engine.unmatched_summary(cusip_map)["value"] == 100.0

    def test_the_guard_still_refuses_a_large_name_dropped_from_the_map(self, store, tmp_path):
        """13F-6 with scaled values: one big ticker leaves the map, the ticker count holds, the dollars say so."""
        rows, full_map = [], {}
        for i in range(12):
            for j in range(5):
                cusip = f"{i + 1:03d}{j:03d}000"
                rows.append(_row(f"a{i}", f"NAME{i}{j}", cusip, "1000", "100", line_no=j + 1))
                full_map[cusip] = f"T{i}"
        z = _write_zip(
            tmp_path / "many_form13f.zip",
            [_sub(f"a{i}", str(1000 + i), "01-MAY-2026", "13F-HR", "31-MAR-2026") for i in range(12)],
            rows)
        rawstore.write_rows("price_bars_daily", [
            {"ticker": f"T{i}", "session_date": "2026-03-31", "close": 100.0, "source": "test"}
            for i in range(12)])
        engine.ingest_zips([z])
        engine.save_cusip_ticker(full_map)

        totals, _factors = engine.compute_totals(full_map)
        dropped = {c: t for c, t in full_map.items() if t != "T0"}
        smaller, smaller_factors = engine.compute_totals(dropped)
        assert len(totals) == 12 and len(smaller) == 11       # the ticker-count guard does not see the drop
        assert set(smaller_factors.values()) == {1000.0}
        with pytest.raises(engine.DeriveRefused, match="unmapped share"):
            engine.guard(totals, smaller, smaller_factors)


class TestHoldersV12:
    """brief.md §3 tests 19-20: holder counts are distinct filing CIKs; no behaviour change (defect E)."""

    def test_an_other_manager_name_never_becomes_a_holder(self, store, tmp_path):
        """``OTHERMANAGER`` names an included manager, not a filer: the holder count comes from ``h.cik``.

        The named manager is the other real filer of the same CUSIP, so counting the name instead of
        the filing CIK would merge the two filers into one holder.
        """
        z = _write_zip(
            tmp_path / "othermgr_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026"),
             _sub("b", "2000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("a", "ALPHA INC", "111111111", "100", "1000", other_manager="2000"),
             _row("b", "ALPHA INC", "111111111", "200", "2000")])
        engine.ingest_zips([z])
        stored = {r["accession"]: r["other_manager"] for r in rawstore.query(
            "SELECT accession, other_manager FROM sec13f_holdings")}
        assert stored["a"] == "2000"
        engine.save_cusip_ticker({"111111111": "A"})
        totals = engine.recompute_totals()
        assert totals["A"]["2026-03-31"]["holders"] == 2
        assert totals["A"]["2026-03-31"]["shares"] == 300.0

    def test_new_holdings_by_the_same_cik_counts_once(self, store, tmp_path):
        """A NEW HOLDINGS /A by the same CIK unions its rows, and the CIK is still one holder."""
        orig = _write_zip(
            tmp_path / "orig_form13f.zip",
            [_sub("o", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [_row("o", "ALPHA INC", "111111111", "100", "1000")],
            coverpage_rows=[_cover("o", "31-MAR-2026")])
        add = _write_zip(
            tmp_path / "nh_form13f.zip",
            [_sub("nh", "1000", "15-MAY-2026", "13F-HR/A", "31-MAR-2026")],
            [_row("nh", "ALPHA INC", "111111111", "50", "500")],
            coverpage_rows=[_cover("nh", "31-MAR-2026", "13F-HR/A", "Y", "1", "NEW HOLDINGS")])
        engine.ingest_zips([orig, add])
        engine.save_cusip_ticker({"111111111": "A"})
        totals = engine.recompute_totals()
        assert totals["A"]["2026-03-31"]["holders"] == 1
        assert totals["A"]["2026-03-31"]["shares"] == 150.0


class TestMigrationV12:
    """brief.md §3 tests 15-18: the one-time v12 reset (``scripts/migrate_sec13f_v12.py``)."""

    V11_DDL = ("CREATE TABLE sec13f_holdings "
               "(accession TEXT, cusip TEXT, cik TEXT, shares DOUBLE, value DOUBLE)")

    def _v11_shape(self, rows=()):
        rawstore.execute("DROP TABLE sec13f_holdings")
        rawstore.execute(self.V11_DDL)
        for row in rows:
            rawstore.execute("INSERT INTO sec13f_holdings VALUES (?, ?, ?, ?, ?)", list(row))
        # ``_ensure_table`` is memoised per (file, table) for the life of the process, so a second
        # ensure here would be a no-op and ``_init_db`` could not ALTER anything whatever the order
        # of the guard. A live v11 store is met by a fresh process with empty memos: reset them, or
        # "the refusal came before ``_init_db``" is a claim no test can fail.
        rawstore._TABLE_READY.clear()
        rawstore._ENSURED.clear()

    def _live_key(self):
        with rawstore.connect() as conn:
            return rawstore._live_primary_keys(conn, "sec13f_holdings").get("sec13f_holdings", [])

    def _columns(self):
        return [r["column_name"] for r in rawstore.query(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'main' "
            "AND table_name = 'sec13f_holdings' ORDER BY ordinal_position")]

    def test_preflight_refuses_a_missing_zip_and_writes_nothing(self, store, tmp_path):
        """brief.md §3 test 15, mutation 9: a missing file would cost a multi-GB re-download."""
        from RevenueChaser.scripts import migrate_sec13f_v12 as migrate
        self._v11_shape([("a", "111111111", "1000", 100.0, 1000.0)])
        rawstore.write_rows("sec13f_ingested_zips",
                            [{"filename": "gone_form13f.zip", "ingested_at": "2026-10-01"}])
        before = (rawstore.count("sec13f_holdings"), rawstore.count("sec13f_submissions"), self._live_key())
        assert migrate.main(["--dry-run"]) == 1
        after = (rawstore.count("sec13f_holdings"), rawstore.count("sec13f_submissions"), self._live_key())
        assert after == before

    def test_a_full_run_rekeys_refills_and_recomputes(self, store, tmp_path):
        """brief.md §3 test 16: v12 key, markers cleared then refilled, holdings refilled, totals derived."""
        from RevenueChaser.scripts import migrate_sec13f_v12 as migrate
        z = _write_zip(
            tmp_path / "2026q2_form13f.zip",
            [_sub("a", "1000", "01-AUG-2026", "13F-HR", "30-JUN-2026")],
            [_row("a", "ALPHA INC", "111111111", "100", "1000")])
        self._v11_shape([("a", "111111111", "1000", 999.0, 999.0)])   # the v11 row the reset replaces
        rawstore.write_rows("sec13f_ingested_zips",
                            [{"filename": "2026q2_form13f.zip", "ingested_at": "2026-10-01"}])
        engine.save_cusip_ticker({"111111111": "A"})
        result = migrate.run(ownership_dir=tmp_path, out=lambda *_: None)
        assert self._live_key() == ["accession", "line_no"]
        assert [r["filename"] for r in rawstore.query("SELECT filename FROM sec13f_ingested_zips")] == \
            ["2026q2_form13f.zip"]
        # cleared then refilled by the re-ingest, not the seeded marker
        assert rawstore.query("SELECT ingested_at FROM sec13f_ingested_zips")[0]["ingested_at"] != "2026-10-01"
        shares = rawstore.query("SELECT shares FROM sec13f_holdings")
        assert [r["shares"] for r in shares] == [100.0]
        assert engine.load_totals()["A"]["2026-06-30"]["shares"] == 100.0
        assert result["after"]["holdings"] == 1

    def test_a_second_run_refuses(self, store, tmp_path):
        """brief.md §3 test 17: never rebuild a table that is already at the v12 key."""
        from RevenueChaser.scripts import migrate_sec13f_v12 as migrate
        assert self._live_key() == ["accession", "line_no"]        # the fixture store is already v12
        with pytest.raises(migrate.MigrationRefused, match="already at v12"):
            migrate.run(ownership_dir=tmp_path, out=lambda *_: None)

    def test_ingest_refuses_a_v11_holdings_table_before_writing(self, store, tmp_path):
        """brief.md §3 test 18, mutation 10: a bare ON CONFLICT error on the live table is not acceptable."""
        z = _write_zip(
            tmp_path / "2026q2_form13f.zip",
            [_sub("a", "1000", "01-AUG-2026", "13F-HR", "30-JUN-2026")],
            [_row("a", "ALPHA INC", "111111111", "100", "1000")])
        self._v11_shape()
        with pytest.raises(RuntimeError, match="v11 13F holdings shape"):
            engine.ingest_zips([z])
        assert rawstore.count("sec13f_holdings") == 0
        assert rawstore.count("sec13f_submissions") == 0
        # the refusal comes before ``_init_db``: not one of the ten v12 columns landed
        assert self._columns() == ["accession", "cusip", "cik", "shares", "value"]

    def test_the_derive_refuses_a_v11_holdings_table_before_writing(self, store, tmp_path):
        """A v11 store refuses the derive with the migration's name and writes nothing at all."""
        engine.save_totals({"NVDA": {"2026-03-31": {"shares": 5.0, "holders": 1}}})
        self._v11_shape([("a", "111111111", "1000", 100.0, 1000.0)])
        before = (rawstore.count("sec13f_holdings"), rawstore.count("sec13f_submissions"), self._live_key())
        with pytest.raises(engine.DeriveRefused, match="migrate_sec13f_v12.py"):
            engine.recompute_totals()
        assert (rawstore.count("sec13f_holdings"), rawstore.count("sec13f_submissions"),
                self._live_key()) == before
        assert self._columns() == ["accession", "cusip", "cik", "shares", "value"]
        assert "NVDA" in engine.load_totals()

    def test_a_store_with_no_holdings_table_is_not_refused(self, store, tmp_path):
        """A fresh store is not a v11 store: the derive creates the v12 table and derives nothing."""
        rawstore.execute("DROP TABLE IF EXISTS sec13f_holdings")
        rawstore._TABLE_READY.clear()       # as a fresh process sees it: the next store call creates the table
        assert engine.recompute_totals() == {}
        assert self._live_key() == ["accession", "line_no"]


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

    def test_a_non_numeric_numeric_field_is_null_not_zero_and_does_not_abort(self, store, tmp_path):
        """SHOULD-1: an absent field and a reported 0 differ; SEC text ("n/a") must not abort the zip."""
        row = _row("a", "ALPHA INC", "111111111", "100", "1000")
        row["VOTING_AUTH_SOLE"] = "n/a"
        z = _write_zip(
            tmp_path / "text_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            [row])
        assert engine.ingest_zips([z]) == 1
        stored = rawstore.query("SELECT voting_sole, voting_shared, shares, value FROM sec13f_holdings")[0]
        assert stored["voting_sole"] is None
        assert stored["voting_shared"] == 0.0                     # a reported 0 stays a 0
        assert stored["shares"] == 100.0 and stored["value"] == 1000.0

    def test_a_non_finite_numeric_field_is_null_too(self, store, tmp_path):
        """S-D: ``float("nan")`` and ``float("inf")`` do not raise, so ValueError alone is not enough.

        One stored NaN would poison ``SUM(h.value * factor)`` for the whole ticker-period — every
        comparison against NaN is false, so it would not even show up as a large number.
        """
        rows = [_row("a", "ALPHA INC", "111111111", "100", "nan", line_no=1),
                _row("a", "BETA INC", "222222222", "100", "inf", line_no=2)]
        z = _write_zip(
            tmp_path / "nan_form13f.zip",
            [_sub("a", "1000", "01-MAY-2026", "13F-HR", "31-MAR-2026")],
            rows)
        assert engine.ingest_zips([z]) == 1
        stored = rawstore.query("SELECT value FROM sec13f_holdings ORDER BY line_no")
        assert [r["value"] for r in stored] == [None, None]
