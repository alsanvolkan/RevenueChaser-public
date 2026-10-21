"""Data Health: the "SEC submissions" row and the corpus documents note (#299, D15/D16). Store only."""
from __future__ import annotations

import datetime as dt

from RevenueChaser.core import edgar_store, rawstore, sec_submissions
from RevenueChaser.module_settings import inventory

RUN = dt.datetime(2026, 10, 7, 22, 0, tzinfo=dt.timezone.utc).timestamp()


def _rows():
    return {r["key"]: r for r in inventory.collect()["rows"]}


def _payload(acc, filed="2026-05-28"):
    return {"name": "X", "sic": "1", "filings": {"recent": {
        "accessionNumber": [acc], "form": ["10-K"], "filingDate": [filed], "primaryDocument": ["d.htm"]}}}


def _seed_universe():
    rawstore.write_rows("equity_universe", [{"ticker": t, "market_cap": 1e9, "name": t} for t in ("A", "B", "C")])
    rawstore.write_rows("sec_company_tickers", [{"ticker": "A", "cik": "1", "name": "A"},
                                                {"ticker": "B", "cik": "0000000002", "name": "B"},
                                                {"ticker": "C", "cik": "3", "name": "C"}])


def _store(answers, run_id, run_at=RUN):
    cap = sec_submissions.Capture(run_at=run_at)
    for cik, p in answers.items():
        cap.add(cik, p)
    assert "error" not in cap.flush(run_id=run_id)


def test_no_sweep_yet_says_so(rawstore_tmp_db):
    row = _rows()["sec_submissions"]
    assert row["label"] == "SEC submissions" and row["holes"] is None
    assert row["note"] == "no submissions yet (the daily EDGAR sync stores them)"
    assert row["fetch"] == {"route": "/run/edgar-sync", "label": "Fetch 10-K sync"}


def test_the_last_run_against_the_universe_ciks(rawstore_tmp_db):
    _seed_universe()
    _store({"1": _payload("a-1"), "2": _payload("b-1", "2026-06-02"), "3": _payload("c-1")}, "r1")
    _store({"1": _payload("a-1"), "2": _payload("b-1", "2026-06-02")}, "r2", RUN + 86400)   # C failed in r2
    row = _rows()["sec_submissions"]
    assert row["rows"] == 3 and row["last_date"] == "2026-06-02"
    assert row["holes"] == 1
    assert row["note"].startswith("last run 20")
    assert "2 of 3 universe CIKs refreshed" in row["note"]
    assert "3 CIKs with filings; newest filing 2026-06-02" in row["note"]


def test_a_summary_failure_is_recorded_and_the_page_still_renders(rawstore_tmp_db, monkeypatch):
    _store({"1": _payload("a-1")}, "r1")

    def boom():
        raise RuntimeError("edgar locked")

    monkeypatch.setattr(sec_submissions, "health_summary", boom)
    payload = inventory.collect()
    row = {r["key"]: r for r in payload["rows"]}["sec_submissions"]
    assert row["note"] == "no submissions yet (the daily EDGAR sync stores them)"
    assert any(e.get("table") == "sec_submissions" for e in payload.get("errors", []))


def test_corpus_note_counts_documents_per_form(rawstore_tmp_db, tmp_path, monkeypatch):
    from RevenueChaser.core import edgar_docs
    monkeypatch.setattr(edgar_docs, "EDGAR_DOCS_DIR", tmp_path / "docs")
    for acc, form in (("a", "10-K"), ("b", "10-K"), ("c", "20-F")):
        edgar_store.save_document(edgar_store.document_row(acc, "1", "A", form, "2026-01-01", "<html/>"))
    note = _rows()["corpus"]["note"]
    assert "documents 10-K 2, 20-F 1; newest fetched 20" in note


def test_the_new_queries_are_pinned():
    pairs = {("sec_submissions", "filing_date"), ("sec_company_tickers", "cik"), ("edgar_documents", "form"),
             ("edgar_documents", "fetched_at"), ("source_checks", "run_id"), ("source_checks", "key")}
    assert pairs <= set(inventory.QUERIED_COLUMNS)
