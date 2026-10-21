"""#297 (D12): a Financials Fetch stores the ticker's yfinance statements and valuation, and its comps peers'
valuation, in vendor_financials / vendor_valuation. A cache hit and a Refresh store nothing. No network."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

import RevenueChaser.module_financials.run as fm
from RevenueChaser.core import rawstore, vendor_data
from RevenueChaser.core.providers import cache as cache_manager

COMPLETE = datetime(2026, 10, 8, 20, 35, tzinfo=timezone.utc).timestamp()
_PEER_INFO = {"enterpriseValue": 9.0e9, "ebitda": 1.0e9, "totalRevenue": 4.0e9, "trailingPE": 18.0}


def _payload(financials):
    out = copy.deepcopy(financials)
    for rec in out["annual"]:
        rec["period_end"] = f"{rec['fiscal_year']}-12-31"
    out["info"] = {"enterpriseValue": 1.0e9, "currency": "USD", "longBusinessSummary": "Test Co."}
    out["source"] = "yfinance"
    return out


def _setup(monkeypatch, tmp_path, financials):
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(vendor_data, "_clock", lambda: COMPLETE)
    calls = []

    def fetch(ticker):
        calls.append(ticker)
        return _payload(financials)

    monkeypatch.setattr(fm.yfinance_client, "get_financials", fetch)
    qr = fm.quick_remarks_engine
    monkeypatch.setattr(qr, "run_quick_remarks", lambda ticker, no_fetch=False: qr._from_card(ticker, {}, "yfinance"))
    monkeypatch.setattr(fm.comps_engine, "_default_fetch", lambda t: dict(_PEER_INFO))
    return calls


def _stored(table):
    return rawstore.query(f"SELECT * FROM {table} ORDER BY ticker")


def test_a_fetch_stores_statements_valuation_and_peer_valuation(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    calls = _setup(monkeypatch, tmp_path, financials)
    fm.run("TEST", modules=["dcf", "comps"], output="html", peers=["P1", "P2"], run_id="job-q")
    assert calls == ["TEST"]
    fin = _stored("vendor_financials")
    assert {r["ticker"] for r in fin} == {"TEST"} and len(fin) == 3 * len(financials["annual"])
    assert {r["run_id"] for r in fin} == {"job-q"} and {r["source"] for r in fin} == {"yfinance"}
    val = {r["ticker"]: r for r in _stored("vendor_valuation")}
    assert sorted(val) == ["P1", "P2", "TEST"]
    assert val["P1"]["trailing_pe"] == 18.0 and val["TEST"]["enterprise_value"] == 1.0e9
    assert {r["run_id"] for r in val.values()} == {"job-q"}


def test_a_cache_hit_stores_no_statements(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    calls = _setup(monkeypatch, tmp_path, financials)
    cache_manager.save("TEST", _payload(financials))
    fm.run("TEST", modules=["dcf"], output="html", run_id="job-q")
    assert calls == []
    assert _stored("vendor_financials") == [] and _stored("vendor_valuation") == []


def test_a_refresh_stores_nothing(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    calls = _setup(monkeypatch, tmp_path, financials)
    cache_manager.save("TEST", _payload(financials))
    fm.run("TEST", modules=["dcf", "comps"], output="html", no_fetch=True, peers=["P1"])
    assert calls == []
    assert _stored("vendor_financials") == [] and _stored("vendor_valuation") == []


def test_without_a_run_id_the_rows_get_a_fresh_one(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    _setup(monkeypatch, tmp_path, financials)
    fm.run("TEST", modules=["dcf"], output="html")
    ids = {r["run_id"] for r in _stored("vendor_financials")}
    assert len(ids) == 1 and None not in ids


def test_a_report_write_that_fails_leaves_no_vendor_rows(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    """FIN-7: the vendor rows are flushed after the report is written, so a crash before the board file leaves no
    ``vendor_*`` rows with an orphan run_id."""
    _setup(monkeypatch, tmp_path, financials)

    def boom(path, html):
        raise OSError("disk full")

    monkeypatch.setattr(fm, "write_artifact", boom)
    with pytest.raises(OSError):
        fm.run("TEST", modules=["dcf", "comps"], output="html", peers=["P1"], run_id="job-q")
    assert _stored("vendor_financials") == [] and _stored("vendor_valuation") == []


def test_a_report_render_that_fails_leaves_no_vendor_rows(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    _setup(monkeypatch, tmp_path, financials)

    def boom(results):
        raise RuntimeError("template error")

    monkeypatch.setattr(fm, "build_report_html", boom)
    with pytest.raises(RuntimeError):
        fm.run("TEST", modules=["dcf"], output="html", run_id="job-q")
    assert _stored("vendor_financials") == [] and _stored("vendor_valuation") == []


def test_a_fetch_without_a_report_still_stores_the_vendor_rows(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    """The flush moved after the report block, not inside it: a Fetch that writes no report still stores."""
    _setup(monkeypatch, tmp_path, financials)
    out = fm.run("TEST", modules=["dcf", "comps"], output="none", peers=["P1"], run_id="job-q")
    assert out["paths"] == {}
    assert len(_stored("vendor_financials")) == 3 * len(financials["annual"])
    assert sorted(r["ticker"] for r in _stored("vendor_valuation")) == ["P1", "TEST"]
