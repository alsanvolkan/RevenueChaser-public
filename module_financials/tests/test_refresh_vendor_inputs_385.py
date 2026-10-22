"""#385 FIN-5: the Refresh and the model inputs read the Fetch's own stored vendor rows.

A Fetch stores what it fetched in ``vendor_financials`` / ``vendor_valuation`` (#297). The store-only Refresh reads
those rows back through ``core.vendor_data.read_financials`` — read-only, zero network — so the model inputs are the
rows the Fetch stored, not a raw cache payload and never a new request. Quick Remarks is handed the payload the run
already has, so a Fetch makes ONE ``get_financials`` call and a Refresh makes none.

Offline: a temp DuckDB, an injected ``get_financials``; the Refresh runs with a provider that raises on any call.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timezone

import pytest

import RevenueChaser.module_financials.run as fm
from RevenueChaser.core import rawstore, vendor_data
from RevenueChaser.core.providers import cache as cache_manager
from RevenueChaser.module_asymmetric_growth import engine as agh
from RevenueChaser.module_financials.engines import quick_remarks_engine as qr

# 2026-10-08 is a Thursday: 20:35 UTC = 16:35 ET (a complete session, so the valuation row is written).
COMPLETE = datetime(2026, 10, 8, 20, 35, tzinfo=timezone.utc).timestamp()
_PEER_INFO = {"enterpriseValue": 9.0e9, "ebitda": 1.0e9, "totalRevenue": 4.0e9, "trailingPE": 18.0}


def _payload(financials: dict) -> dict:
    """What ``yfinance.get_financials`` returns for the fixture: the years, each with its fiscal period end."""
    out = copy.deepcopy(financials)
    for rec in out["annual"]:
        rec["period_end"] = f"{rec['fiscal_year']}-12-31"
        # yfinance serves the cash-flow line under its canonical name, not the fixture's short one.
        rec.setdefault("capital_expenditure", rec.get("capex"))
    out["info"] = {"longName": "Test Corp.", "currency": "USD", "sector": "Technology",
                   "sharesOutstanding": 1000.0, "marketCap": 100_000.0, "currentPrice": 100.0,
                   "beta": 1.2, "enterpriseValue": 1.0e9}
    out["source"] = "yfinance"
    return out


def _fetch(monkeypatch, tmp_path, financials: dict, *, counting: bool = True) -> list:
    """A Financials Fetch with an injected provider: no network, the temp cache dir, the calls it made."""
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(vendor_data, "_clock", lambda: COMPLETE)
    calls: list = []

    def get_financials(ticker: str) -> dict:
        calls.append(ticker)
        return _payload(financials)

    monkeypatch.setattr(fm.yfinance_client, "get_financials", get_financials)
    monkeypatch.setattr(qr, "fetch_technical", lambda t: {})
    monkeypatch.setattr(agh, "score_ticker", lambda t, f, tech, meta=None: None)
    monkeypatch.setattr(fm.comps_engine, "_default_fetch", lambda t: dict(_PEER_INFO))
    return calls


def _failing(*_a, **_kw):
    raise AssertionError("a store-only Refresh called a provider")


def _rows(table: str) -> int:
    return len(rawstore.query(f"SELECT * FROM {table}"))


# ── the reader ───────────────────────────────────────────────────────────────────────────────────────────────
def test_the_vendor_reader_returns_the_fetchs_own_rows(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    """Statements merged per fiscal year, the ``.info`` fields, and None for a ticker the store never fetched."""
    _fetch(monkeypatch, tmp_path, financials)
    fm.run("TEST", modules=["dcf"], output="none", run_id="job-q")
    stored = (_rows("vendor_financials"), _rows("vendor_valuation"))

    got = vendor_data.read_financials("TEST")
    assert got["ticker"] == "TEST" and got["currency"] == "USD" and got["source"] == "yfinance"
    assert [r["fiscal_year"] for r in got["annual"]] == ["2021", "2022", "2023"]
    latest = got["annual"][-1]
    assert [r["period_end"] for r in got["annual"]] == ["2021-12-31", "2022-12-31", "2023-12-31"]
    assert latest["revenue"] == 1000.0 and latest["ebitda"] == 300.0 and latest["cash"] == 120.0
    assert latest["total_debt"] == 260.0 and latest["pretax_income"] == 240.0
    assert latest["capital_expenditure"] == 50.0 and latest["depreciation"] == 50.0
    assert got["shares_outstanding"] == 1000.0 and got["market_cap"] == 100_000.0
    assert got["beta"] == 1.2 and got["current_price"] == 100.0
    # read-only: the same rows, twice, and nothing appended
    assert vendor_data.read_financials("TEST") == got
    assert (_rows("vendor_financials"), _rows("vendor_valuation")) == stored
    assert vendor_data.read_financials("NOPE") is None


def test_a_period_closed_as_absent_is_not_read_as_a_value(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    """Nit (review): the reader takes 'present' rows only. A newer row that closes a period as absent carries
    no value, so it must not win the newest-row rule over the present row the Fetch stored."""
    _fetch(monkeypatch, tmp_path, financials)
    fm.run("TEST", modules=["dcf"], output="none", run_id="job-q")
    rawstore.write_rows("vendor_financials", [{
        "ticker": "TEST", "source": "yfinance", "statement": "income", "report_type": "annual",
        "period_end": date(2023, 12, 31), "valid_from": date(2026, 10, 9), "status": "absent",
        "fetched_at": COMPLETE, "run_id": "job-later", "run_at": COMPLETE + 86_400, "payload": {}}])

    latest = vendor_data.read_financials("TEST")["annual"][-1]
    assert latest.get("revenue") == 1000.0 and latest.get("ebitda") == 300.0


# ── the Refresh's model inputs are those rows ───────────────────────────────────────────────────────────────
def test_a_refresh_renders_from_the_stored_vendor_rows(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    """A different payload left in the raw cache: the Refresh's numbers are still the vendor rows' (FY2023 1000)."""
    _fetch(monkeypatch, tmp_path, financials)
    fm.run("TEST", modules=["dcf"], output="none", run_id="job-q")

    decoy = _payload(financials)
    decoy["annual"] = [{"fiscal_year": "2023", "period_end": "2023-12-31", "revenue": 1.0, "ebitda": 1.0}]
    cache_manager.save("TEST", decoy)
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)

    res = fm.run("TEST", modules=["dcf"], output="none", no_fetch=True)
    base = res["results"]["modules"]["dcf"]["base_year"]
    assert base["fiscal_year"] == "2023" and base["revenue"] == 1000.0 and base["ebitda"] == 300.0
    assert res["results"]["financials"]["shares_outstanding"] == 1000.0


def test_the_whole_refresh_path_makes_zero_provider_calls(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    """Not even when no stored run exists (the comps empty state), and not for the peer quotes."""
    _fetch(monkeypatch, tmp_path, financials)
    fm.run("TEST", modules=["dcf"], output="none", run_id="job-q")

    monkeypatch.setattr(fm.yfinance_client, "get_financials", _failing)
    monkeypatch.setattr(fm.comps_engine, "_default_fetch", _failing)
    monkeypatch.setattr(qr, "fetch_technical", _failing)

    res = fm.run("TEST", modules=["dcf", "comps"], output="none", no_fetch=True)
    assert res["results"]["modules"]["dcf"]["base_year"]["revenue"] == 1000.0
    assert res["results"]["modules"]["comps"]["not_stored"] is True     # no stored run: said, not fetched


# ── Quick Remarks: no second get_financials ─────────────────────────────────────────────────────────────────
def test_quick_remarks_calls_get_financials_zero_times_on_a_refresh(rawstore_tmp_db, tmp_path, monkeypatch,
                                                                    financials):
    _fetch(monkeypatch, tmp_path, financials)
    fm.run("TEST", modules=["dcf"], output="none", run_id="job-q")

    calls: list = []
    monkeypatch.setattr(fm.yfinance_client, "get_financials", lambda t: calls.append(t) or {})
    qr_calls = []
    monkeypatch.setattr(qr.yfinance_client, "get_financials", lambda t: qr_calls.append(t) or {})
    fm.run("TEST", modules=["dcf"], output="none", no_fetch=True)
    assert calls == [] and qr_calls == []


def test_a_fetch_makes_one_get_financials_call_not_two(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    """Quick Remarks is handed the payload the run already fetched, so the second call is gone."""
    calls = _fetch(monkeypatch, tmp_path, financials)
    seen: dict = {}
    monkeypatch.setattr(agh, "score_ticker",
                        lambda t, f, tech, meta=None: seen.update(fin=f) or None)

    fm.run("TEST", modules=["dcf"], output="none", run_id="job-q")
    assert calls == ["TEST"]
    assert seen["fin"]["annual"][-1]["revenue"] == 1000.0               # the run's own payload reached it


def test_a_refresh_with_nothing_stored_still_raises_the_no_data_error(rawstore_tmp_db, tmp_path, monkeypatch):
    """Neither vendor rows nor a raw payload: the caller falls back to a Fetch, which is not this path's job."""
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    with pytest.raises(fm._NoCachedData):
        fm.run("ZZZZ", modules=["dcf"], output="none", no_fetch=True)
