"""The daily EDGAR sync stores the submissions it fetches (#299, D15) and documents once (D16). No network."""
from __future__ import annotations

import httpx
import pytest

from RevenueChaser.core import edgar_store, rawstore, sec_submissions
from RevenueChaser.module_value_chain import edgar_text, engine, sync


def _subs(accession="acc-1", form="10-K", filed="2026-02-01", sic="3674"):
    return {"name": "CO", "sic": sic, "sicDescription": "x", "entityType": "operating", "tickers": ["T"],
            "exchanges": ["Nasdaq"],
            "filings": {"recent": {"form": [form, "8-K"], "filingDate": [filed, "2026-03-01"],
                                   "accessionNumber": [accession, accession + "-8k"],
                                   "primaryDocument": ["doc.htm", "e.htm"], "items": ["", "2.02"]}}}


def _http(code):
    req = httpx.Request("GET", "https://data.sec.gov/x")
    return httpx.HTTPStatusError(str(code), request=req, response=httpx.Response(code, request=req))


@pytest.fixture
def env(rawstore_tmp_db, monkeypatch):
    """NVDA and its dual class NVDB (one CIK) at the classification floor, TINY below it."""
    rawstore.replace_all("equity_universe", [
        {"ticker": "NVDA", "market_cap": 2e9, "name": "N"}, {"ticker": "NVDB", "market_cap": 1e9, "name": "N"},
        {"ticker": "TINY", "market_cap": 1e6, "name": "T"}])
    rawstore.write_rows("sec_company_tickers", [
        {"ticker": "NVDA", "cik": "1045810", "name": "N"}, {"ticker": "NVDB", "cik": "1045810", "name": "N"},
        {"ticker": "TINY", "cik": "0000000077", "name": "T"}])
    e = {"urls": [], "downloads": 0, "subs": {1045810: _subs(), 77: _subs("tiny-1", sic="1000")}, "raise": {}}

    def _get_json(url, client=None):
        e["urls"].append(url)
        cik = int(url.rsplit("CIK", 1)[1].split(".")[0])
        if cik in e["raise"]:
            raise e["raise"][cik]
        return e["subs"][cik]

    def _get_text(url, client=None):
        e["downloads"] += 1
        return "<html>Item 1. Business We make chips. Item 1A. Risk Factors</html>"

    monkeypatch.setattr(sync.sec_edgar, "get_json", _get_json)
    monkeypatch.setattr(edgar_text.sec_edgar, "get_text", _get_text)
    return e


def _filings():
    return rawstore.query("SELECT cik, accession, run_id FROM sec_submissions ORDER BY cik, accession", db="edgar")


def _headers():
    return rawstore.query("SELECT cik, status, sic, run_id FROM sec_submissions_cik ORDER BY cik, run_at",
                          db="edgar")


def test_the_sweep_stores_every_universe_cik_once_with_the_run_id(env):
    out = sync.sync_filings(run_id="job-1")
    assert len(env["urls"]) == 2                                       # one request per CIK, dual class shared
    assert [(r["cik"], r["accession"]) for r in _filings()] == [
        ("1045810", "acc-1"), ("1045810", "acc-1-8k"), ("77", "tiny-1"), ("77", "tiny-1-8k")]
    assert {r["run_id"] for r in _filings()} == {"job-1"}
    assert [(r["cik"], r["status"]) for r in _headers()] == [("1045810", "present"), ("77", "present")]
    s = out["submissions"]
    assert s["requested"] == 2 and s["ciks"] == 2 and s["filings_appended"] == 4 and s["headers_appended"] == 2
    assert s["failed"] == 0 and s["failed_ciks"] == [] and out["run_id"] == "job-1"


def test_only_the_classification_universe_takes_the_document_step(env):
    out = sync.sync_filings()
    assert out["updated"] == 2 and env["downloads"] == 1               # NVDB re-slices NVDA's stored doc
    assert rawstore.get("chain_filings", "TINY") is None
    assert rawstore.get("chain_filings", "NVDB")["accession"] == "acc-1"


def test_a_second_run_appends_nothing_and_downloads_nothing(env):
    sync.sync_filings(run_id="r1")
    out = sync.sync_filings(run_id="r2")
    assert out["current"] == 2 and env["downloads"] == 1
    assert out["submissions"]["filings_appended"] == 0 and out["submissions"]["headers_appended"] == 0
    assert out["submissions"]["headers_unchanged"] == 2
    assert {r["run_id"] for r in _filings()} == {"r1"} and len(_headers()) == 2


def test_a_new_filing_and_a_header_change_append_once(env):
    sync.sync_filings(run_id="r1")
    env["subs"][1045810] = _subs("acc-2", filed="2027-02-01", sic="3672")
    out = sync.sync_filings(run_id="r2")
    assert out["updated"] == 2 and out["submissions"]["filings_appended"] == 2
    assert [(r["cik"], r["sic"], r["run_id"]) for r in _headers()] == [
        ("1045810", "3674", "r1"), ("1045810", "3672", "r2"), ("77", "1000", "r1")]


def test_a_failed_cik_writes_nothing_and_is_named(env):
    env["raise"][77] = _http(503)
    out = sync.sync_filings()
    assert out["submissions"]["failed"] == 1 and out["submissions"]["failed_ciks"] == ["77"]
    assert {r["cik"] for r in _filings()} == {"1045810"}
    assert out["failed"] == 0                                          # TINY has no document step to fail


@pytest.mark.parametrize("code", [503, 403])
def test_a_stored_cik_whose_request_fails_keeps_its_rows(env, code):
    """Only a 404 closes a CIK: a failed request on a stored CIK writes no 'absent' row and no watermark."""
    sync.sync_filings(run_id="r1")
    before = (_headers(), _filings())
    env["raise"][77] = _http(code)
    out = sync.sync_filings(run_id="r2")
    assert out["submissions"]["headers_absent"] == 0 and out["submissions"]["failed_ciks"] == ["77"]
    assert (_headers(), _filings()) == before
    checks = {r["key"]: r["run_id"] for r in rawstore.query(
        "SELECT key, run_id FROM source_checks WHERE dataset = 'sec_submissions'")}
    assert checks == {"1045810": "r2", "77": "r1"}


def test_an_sec_outage_leaves_the_rest_pending(env):
    env["raise"][77] = sync.sec_edgar.SECUnavailable("breaker open")
    out = sync.sync_filings()
    s = out["submissions"]
    assert s["pending"] == 1 and s["failed"] == 0 and s["ciks"] == 1
    assert out["sec_unavailable"] is True and {r["cik"] for r in _filings()} == {"1045810"}


def test_a_cik_sec_no_longer_serves_is_closed_not_failed(env):
    sync.sync_filings(run_id="r1")
    env["raise"][77] = _http(404)
    out = sync.sync_filings(run_id="r2")
    assert out["submissions"]["failed"] == 0 and out["submissions"]["headers_absent"] == 1
    assert [(r["cik"], r["status"]) for r in _headers() if r["cik"] == "77"] == [("77", "present"), ("77", "absent")]


def test_the_document_step_keeps_the_ticker_maps_cik_spelling(env):
    rawstore.write_rows("sec_company_tickers", [{"ticker": "NVDA", "cik": "0001045810", "name": "N"}])
    out = sync.sync_filings()
    assert len(env["urls"]) == 2                                       # still one request for NVDA and NVDB
    assert rawstore.get("chain_filings", "NVDA")["cik"] == "0001045810"
    assert rawstore.get("chain_filings", "NVDB")["cik"] == "1045810" and out["updated"] == 2


def test_refresh_makes_no_request_and_writes_nothing(env):
    out = sync.sync_filings(no_fetch=True)
    assert out["no_fetch"] is True and env["urls"] == [] and _filings() == []


def test_a_limited_run_sweeps_only_the_synced_ciks(env):
    out = sync.sync_filings(limit=1)
    assert len(env["urls"]) == 1 and {r["cik"] for r in _filings()} == {"1045810"}
    assert out["submissions"]["requested"] == 1


def test_a_known_miss_is_still_swept_but_takes_no_document_step(env):
    env["subs"][1045810] = _subs(form="S-1")                            # no 10-K: recorded as a miss
    sync.sync_filings(run_id="r1")
    env["subs"][1045810] = _subs("acc-9", form="S-1")
    out = sync.sync_filings(run_id="r2")
    assert out["known_miss"] == 2 and env["downloads"] == 0
    assert ("1045810", "acc-9") in {(r["cik"], r["accession"]) for r in _filings()}


def test_a_store_failure_is_a_result_and_the_corpus_still_updates(env, monkeypatch):
    def boom(*a, **k):
        raise rawstore.RawStoreError("edgar locked")

    monkeypatch.setattr(sec_submissions, "record", boom)
    out = sync.sync_filings()
    assert "edgar locked" in out["submissions"]["error"]
    assert out["updated"] == 2 and rawstore.get("chain_filings", "NVDA") is not None


def test_a_document_step_failure_keeps_the_submissions_answer(env, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("archive 500")

    monkeypatch.setattr(edgar_text, "fetch_document", broken)
    out = sync.sync_filings()
    assert out["failed"] == 2 and out["submissions"]["failed"] == 0
    assert {r["cik"] for r in _filings()} == {"1045810", "77"}


def test_flushes_in_batches(env, monkeypatch):
    """With a batch of one, the first CIK is stored before the second is even asked."""
    import threading
    monkeypatch.setattr(sync, "FLUSH_EVERY", 1)
    seen, first_stored = [], threading.Event()
    real = sec_submissions.record

    def spy(answers, missing, **kw):
        seen.append(sorted(answers))
        first_stored.set()
        return real(answers, missing, **kw)

    get_json = sync.sec_edgar.get_json

    def gated(url, client=None):
        if "CIK0000000077" in url:
            assert first_stored.wait(5), "CIK 77 was asked before the first batch was stored"
        return get_json(url, client)

    monkeypatch.setattr(sec_submissions, "record", spy)
    monkeypatch.setattr(sync.sec_edgar, "get_json", gated)
    out = sync.sync_filings(workers=1)
    assert seen == [["1045810"], ["77"]] and out["submissions"]["failed"] == 0


# ── documents (D16) ─────────────────────────────────────────────────────────
def test_the_sync_stores_the_document_with_source_and_run_at(env):
    out = sync.sync_filings()
    doc = rawstore.get("edgar_documents", "acc-1")
    assert doc["source"] == "sec_archives" and doc["tickers"] == "NVDA"
    assert doc["run_at"] is not None and doc["fetched_at"] >= doc["run_at"]
    assert out["updated"] == 2


def test_a_forced_rerun_does_not_rewrite_a_stored_document(env):
    sync.sync_filings()
    first = rawstore.get("edgar_documents", "acc-1")
    sync.sync_filings(force=True)
    again = rawstore.get("edgar_documents", "acc-1")
    assert again == first and env["downloads"] == 1


def test_the_value_chain_fetch_download_joins_the_document_path(env, monkeypatch):
    monkeypatch.setattr(engine.sec_edgar, "_cik_map", lambda: {"NVDA": {"cik": "1045810"}})
    out = engine.fetch_corpus(tickers=["NVDA"])
    assert out["fetched"] == 1 and env["downloads"] == 1
    doc = rawstore.get("edgar_documents", "acc-1")
    assert doc["source"] == "sec_archives" and doc["cik"] == "1045810"
    engine.fetch_corpus(tickers=["NVDA"], force=True)                  # a stored doc is re-sliced, not fetched
    assert env["downloads"] == 1


def test_save_document_keeps_the_first_write(rawstore_tmp_db):
    a = edgar_store.document_row("acc-x", "1", "AAA", "10-K", "2026-01-01", "<html>a</html>", run_at=100.0)
    b = {**a, "tickers": "BBB", "run_at": 200.0}
    edgar_store.save_document(a)
    edgar_store.save_document(b)
    got = rawstore.get("edgar_documents", "acc-x")
    assert got["tickers"] == "AAA" and got["run_at"] == 100.0 and edgar_store.count() == 1
