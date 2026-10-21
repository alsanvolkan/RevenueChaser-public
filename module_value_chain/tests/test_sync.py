"""New-filing sync — newer→fetch, same→no write, idempotent (no network)."""
from __future__ import annotations

from RevenueChaser.core import rawstore
from RevenueChaser.module_value_chain import sync


def _subs(accession="acc-1", form="10-K", filed="2026-02-01"):
    return {"filings": {"recent": {
        "form": [form], "filingDate": [filed],
        "accessionNumber": [accession], "primaryDocument": ["doc.htm"]}}}


def _seed(monkeypatch, subs):
    rawstore.replace_all("equity_universe",
                         [{"ticker": "NVDA", "market_cap": 1e9, "name": "NVIDIA"}])
    rawstore.write_rows("sec_company_tickers",
                        [{"ticker": "NVDA", "cik": "1045810", "name": "NVIDIA CORP"}])
    monkeypatch.setattr(sync.sec_edgar, "get_json", lambda url, client=None: subs)
    calls = {"n": 0}

    def _fake_fetch(ticker, cik, filing, client=None):
        calls["n"] += 1
        return ("<html>Item 1. Business</html>",
                {"ticker": ticker.upper(), "cik": str(cik), "form": filing["form"],
                 "filed": filing["filed"], "accession": filing["accession"],
                 "primary_doc": "u", "item_text": "x", "quality": "ok"})

    monkeypatch.setattr(sync.edgar_text, "fetch_document", _fake_fetch)
    return calls


def test_sync_updates_when_newer(rawstore_tmp_db, monkeypatch):
    calls = _seed(monkeypatch, _subs("acc-1"))
    out = sync.sync_filings()
    assert out["updated"] == 1 and calls["n"] == 1
    assert rawstore.get("chain_filings", "NVDA")["accession"] == "acc-1"
    assert rawstore.get("edgar_documents", "acc-1")["tickers"] == "NVDA"


def test_sync_is_idempotent(rawstore_tmp_db, monkeypatch):
    calls = _seed(monkeypatch, _subs("acc-1"))
    sync.sync_filings()
    out = sync.sync_filings()  # same accession -> current, no download
    assert out["current"] == 1 and out["updated"] == 0
    assert calls["n"] == 1  # fetch_document not called the second time


def test_sync_no_fetch_makes_no_request(rawstore_tmp_db, monkeypatch):
    rawstore.replace_all("equity_universe",
                         [{"ticker": "NVDA", "market_cap": 1e9, "name": "NVIDIA"}])
    rawstore.write_rows("sec_company_tickers",
                        [{"ticker": "NVDA", "cik": "1045810", "name": "NVIDIA CORP"}])
    out = sync.sync_filings(no_fetch=True)
    assert out["no_fetch"] is True and out["updated"] == 0


def test_sync_stops_when_sec_breaker_opens(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core.providers import sec_edgar
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "market_cap": 1e9, "name": t} for t in ("AAA", "BBB", "CCC", "DDD")])
    rawstore.write_rows("sec_company_tickers", [
        {"ticker": t, "cik": str(i + 1), "name": t} for i, t in enumerate(("AAA", "BBB", "CCC", "DDD"))])
    seen = []

    def _blocked(url, client=None):
        seen.append(url)
        raise sec_edgar.SECUnavailable("SEC unavailable")

    monkeypatch.setattr(sync.sec_edgar, "get_json", _blocked)
    out = sync.sync_filings(workers=1)
    assert out["sec_unavailable"] is True
    assert out["pending"] == 4 and out["failed"] == 0  # the blocked ticker is pending too
    assert len(seen) == 1  # stopped on the first open


def test_sync_starts_each_run_with_a_closed_breaker(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core.providers import sec_edgar
    calls = _seed(monkeypatch, _subs("acc-1"))
    state = {"open": True, "resets": 0}

    def _get_json(url, client=None):
        if state["open"]:
            raise sec_edgar.SECUnavailable("still open")
        return _subs("acc-1")

    def _reset():
        state["open"] = False
        state["resets"] += 1

    monkeypatch.setattr(sync.sec_edgar, "get_json", _get_json)
    monkeypatch.setattr(sync.sec_edgar, "reset_breaker", _reset)
    out = sync.sync_filings()
    assert state["resets"] == 1
    assert out["updated"] == 1 and calls["n"] == 1 and "sec_unavailable" not in out


def test_breaker_stop_does_not_count_form_40f_as_pending(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core.providers import sec_edgar
    names = ("AAA", "BBB", "CCC")
    rawstore.replace_all("equity_universe", [{"ticker": t, "market_cap": 1e9, "name": t} for t in names])
    rawstore.write_rows("sec_company_tickers", [
        {"ticker": t, "cik": str(i + 1), "name": t} for i, t in enumerate(names)])

    def _get_json(url, client=None):
        if url.endswith("CIK0000000001.json"):
            return _subs("acc-1", form="40-F")  # answered: status form_40f
        raise sec_edgar.SECUnavailable("down")

    monkeypatch.setattr(sync.sec_edgar, "get_json", _get_json)
    out = sync.sync_filings(workers=1)
    assert out["form_40f"] == 1
    assert out["sec_unavailable"] is True
    assert out["pending"] == 2  # BBB (blocked) and CCC (cancelled); AAA was answered
