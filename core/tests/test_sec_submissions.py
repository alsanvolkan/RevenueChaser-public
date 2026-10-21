"""SEC submissions writer (#299, D15): filings append once per accession, the header appends on change (rule A)."""
from __future__ import annotations

import datetime as dt

import pytest

from RevenueChaser.core import calendar, rawstore
from RevenueChaser.core import sec_submissions as ss

# 2026-10-07 is a Wednesday; 18:00 New York is after the close + 30 min, so the run's session is that day.
RUN1 = dt.datetime(2026, 10, 7, 22, 0, tzinfo=dt.timezone.utc).timestamp()
RUN2 = RUN1 + 86400
RUN3 = RUN2 + 86400


def _payload(accessions=("0001-26-000001",), sic="3674", name="NVIDIA CORP", tickers=("NVDA",)):
    n = len(accessions)
    return {
        "cik": "0001045810", "name": name, "sic": sic, "sicDescription": "Semiconductors",
        "entityType": "operating", "tickers": list(tickers), "exchanges": ["Nasdaq"] * len(tickers),
        "filings": {"recent": {
            "accessionNumber": list(accessions),
            "form": ["10-K", "8-K", "10-Q"][:n] + ["4"] * max(0, n - 3),
            "filingDate": ["2026-02-25", "2026-03-01", "2026-05-28"][:n] + ["2026-06-01"] * max(0, n - 3),
            "primaryDocument": [f"d{i}.htm" for i in range(n)],
            "items": ["", "2.02,9.01", ""][:n] + [""] * max(0, n - 3),
        }},
    }


def _record(answers, run_at, run_id="r1"):
    cap = ss.Capture(run_at=run_at)
    for cik, ans in answers.items():
        if ans is None:
            cap.add_missing(cik)
        else:
            cap.add(cik, ans)
    return cap.flush(run_id=run_id)


def _headers(cik="1045810"):
    return rawstore.query("SELECT * FROM sec_submissions_cik WHERE cik = ? ORDER BY run_at", [cik], db="edgar")


def _filings(cik="1045810"):
    return rawstore.query("SELECT * FROM sec_submissions WHERE cik = ? ORDER BY accession", [cik], db="edgar")


class TestMapping:
    def test_header_and_filings_come_from_the_reply(self):
        header, filings = ss.rows_from_payload("0001045810", _payload(("a-1", "a-2")))
        assert header == {"cik": "1045810", "name": "NVIDIA CORP", "sic": "3674",
                          "sic_description": "Semiconductors", "entity_type": "operating",
                          "tickers": ["NVDA"], "exchanges": ["Nasdaq"]}
        assert filings == [
            {"cik": "1045810", "accession": "a-1", "form": "10-K", "filing_date": dt.date(2026, 2, 25),
             "primary_document": "d0.htm", "items": None},
            {"cik": "1045810", "accession": "a-2", "form": "8-K", "filing_date": dt.date(2026, 3, 1),
             "primary_document": "d1.htm", "items": "2.02,9.01"},
        ]

    def test_a_blank_sic_and_a_bad_date_are_null_not_text(self):
        p = _payload(("a-1",), sic="")
        p["sicDescription"] = ""
        p["filings"]["recent"]["filingDate"] = ["not a date"]
        header, filings = ss.rows_from_payload(1045810, p)
        assert header["sic"] is None and header["sic_description"] is None
        assert filings[0]["filing_date"] is None

    def test_a_null_list_element_stays_null(self):
        p = _payload()
        p["exchanges"] = ["Nasdaq", None]
        header, _ = ss.rows_from_payload(1045810, p)
        assert header["exchanges"] == ["Nasdaq", None]

    def test_a_row_without_accession_is_dropped(self):
        p = _payload(("a-1", "a-2"))
        p["filings"]["recent"]["accessionNumber"][1] = ""
        _, filings = ss.rows_from_payload(1045810, p)
        assert [f["accession"] for f in filings] == ["a-1"]


class TestRecord:
    def test_first_run_stores_header_and_every_filing_with_stamps(self, rawstore_tmp_db):
        out = _record({"1045810": _payload(("a-1", "a-2", "a-3"))}, RUN1)
        assert out["filings_appended"] == 3 and out["headers_appended"] == 1
        h = _headers()
        assert len(h) == 1
        assert h[0]["status"] == "present" and h[0]["source"] == "sec_submissions"
        assert h[0]["valid_from"] == calendar.session_for(RUN1).session_date
        assert h[0]["run_id"] == "r1" and h[0]["run_at"] == RUN1 and h[0]["sic"] == "3674"
        f = _filings()
        assert [r["accession"] for r in f] == ["a-1", "a-2", "a-3"]
        assert {r["source"] for r in f} == {"sec_submissions"}
        assert {r["run_at"] for r in f} == {RUN1} and {r["run_id"] for r in f} == {"r1"}
        assert all(r["fetched_at"] for r in f)

    def test_an_unchanged_header_appends_nothing(self, rawstore_tmp_db):
        _record({"1045810": _payload()}, RUN1)
        out = _record({"1045810": _payload()}, RUN2, run_id="r2")
        assert out["headers_appended"] == 0 and out["filings_appended"] == 0
        assert out["headers_unchanged"] == 1
        assert len(_headers()) == 1 and len(_filings()) == 1

    def test_a_new_accession_appends_one_filing_row(self, rawstore_tmp_db):
        _record({"1045810": _payload(("a-1",))}, RUN1)
        out = _record({"1045810": _payload(("a-1", "a-2"))}, RUN2, run_id="r2")
        assert out["filings_appended"] == 1
        f = _filings()
        assert [(r["accession"], r["run_id"]) for r in f] == [("a-1", "r1"), ("a-2", "r2")]

    def test_a_stored_filing_is_not_rewritten(self, rawstore_tmp_db):
        _record({"1045810": _payload(("a-1",))}, RUN1)
        p = _payload(("a-1",))
        p["filings"]["recent"]["form"] = ["10-K/A"]
        _record({"1045810": p}, RUN2, run_id="r2")
        f = _filings()
        assert len(f) == 1 and f[0]["form"] == "10-K" and f[0]["run_id"] == "r1"

    def test_a_changed_header_appends_one_row_on_the_new_run(self, rawstore_tmp_db):
        _record({"1045810": _payload(sic="3674")}, RUN1)
        out = _record({"1045810": _payload(sic="3672")}, RUN2, run_id="r2")
        assert out["headers_appended"] == 1
        h = _headers()
        assert [(r["sic"], r["run_at"]) for r in h] == [("3674", RUN1), ("3672", RUN2)]

    @pytest.mark.parametrize("field,value", [("name", "NVIDIA CORPORATION"), ("tickers", ("NVDA", "NVDB")),
                                             ("sicDescription", "Chips"), ("entityType", "other")])
    def test_each_tracked_field_is_a_change(self, rawstore_tmp_db, field, value):
        _record({"1045810": _payload()}, RUN1)
        p = _payload()
        if field == "tickers":
            p["tickers"] = list(value)                       # exchanges stay as they were
        elif field == "name":
            p["name"] = value
        elif field != "tickers":
            p[field] = value
        assert _record({"1045810": p}, RUN2, run_id="r2")["headers_appended"] == 1

    def test_a_cik_sec_no_longer_serves_gets_one_absent_row(self, rawstore_tmp_db):
        _record({"1045810": _payload()}, RUN1)
        out = _record({"1045810": None}, RUN2, run_id="r2")
        assert out["headers_absent"] == 1
        _record({"1045810": None}, RUN3, run_id="r3")       # still gone: no second row
        h = _headers()
        assert [r["status"] for r in h] == ["present", "absent"]
        assert h[1]["name"] is None and h[1]["run_id"] == "r2"
        assert len(_filings()) == 1                          # the filings stay

    def test_a_cik_back_after_absent_is_present_again(self, rawstore_tmp_db):
        _record({"1045810": _payload()}, RUN1)
        _record({"1045810": None}, RUN2, run_id="r2")
        _record({"1045810": _payload()}, RUN3, run_id="r3")
        assert [r["status"] for r in _headers()] == ["present", "absent", "present"]

    def test_an_empty_header_back_after_absent_is_present_again(self, rawstore_tmp_db):
        bare = {"filings": {"recent": {}}}                   # every tracked field NULL, as on the absent row
        _record({"1045810": bare}, RUN1)
        _record({"1045810": None}, RUN2, run_id="r2")
        _record({"1045810": bare}, RUN3, run_id="r3")
        assert [r["status"] for r in _headers()] == ["present", "absent", "present"]

    def test_a_cik_never_seen_that_sec_does_not_know_writes_nothing(self, rawstore_tmp_db):
        out = _record({"1045810": None}, RUN1)
        assert out["headers_absent"] == 0 and _headers() == []

    def test_answered_ciks_get_a_watermark_with_the_run_id(self, rawstore_tmp_db):
        _record({"1045810": _payload(), "320193": None}, RUN1, run_id="r1")
        _record({"1045810": _payload()}, RUN2, run_id="r2")
        rows = {r["key"]: r for r in rawstore.query(
            "SELECT key, checked_at, run_id FROM source_checks WHERE dataset = 'sec_submissions' "
            "AND source = 'sec_submissions'")}
        assert rows["1045810"]["run_id"] == "r2" and rows["1045810"]["checked_at"] > RUN1
        assert rows["320193"]["run_id"] == "r1"

    def test_a_run_outside_the_holiday_table_still_stores(self, rawstore_tmp_db):
        far = dt.datetime(2031, 1, 6, 22, 0, tzinfo=dt.timezone.utc).timestamp()
        out = _record({"1045810": _payload()}, far)
        assert out["headers_appended"] == 1 and _headers()[0]["valid_from"] is None

    def test_a_cik_twice_in_one_flush_is_stored_once(self, rawstore_tmp_db):
        cap = ss.Capture(run_at=RUN1)
        cap.add("1045810", _payload(sic="3674"))
        cap.add("0001045810", _payload(sic="3672"))
        out = cap.flush(run_id="r1")
        assert out["headers_appended"] == 1 and _headers()[0]["sic"] == "3674"

    def test_flush_clears_and_a_store_error_is_a_result_not_an_exception(self, rawstore_tmp_db, monkeypatch):
        cap = ss.Capture(run_at=RUN1)
        cap.add("1045810", _payload())

        def boom(*a, **k):
            raise rawstore.RawStoreError("disk full")

        monkeypatch.setattr(ss.rawstore, "atomic", boom)
        out = cap.flush(run_id="r1")
        assert "disk full" in out["error"]
        assert cap.flush(run_id="r1")["headers_appended"] == 0   # cleared: nothing left to store

    def test_record_without_answers_opens_no_store(self, isolate_rawstore):
        assert ss.Capture(run_at=RUN1).flush(run_id="r1")["headers_appended"] == 0
        assert not rawstore.edgar_db_path().exists()


class TestHealth:
    def test_empty_store(self, rawstore_tmp_db):
        h = ss.health_summary()
        assert h["filings"] == 0 and h["last_run_ciks"] == 0 and h["newest_filing_date"] is None

    def test_counts_the_last_run_and_the_newest_dates(self, rawstore_tmp_db):
        _record({"1045810": _payload(("a-1", "a-2", "a-3")), "320193": _payload(("b-1",))}, RUN1, run_id="r1")
        _record({"1045810": _payload(("a-1", "a-2", "a-3"))}, RUN2, run_id="r2")
        h = ss.health_summary()
        assert h["filings"] == 4 and h["ciks"] == 2
        assert h["last_run_id"] == "r2" and h["last_run_ciks"] == 1
        assert h["newest_filing_date"] == dt.date(2026, 5, 28)
        assert h["checked_ciks"] == {"1045810", "320193"}
        assert h["last_run_checked"] == {"1045810"}
