"""#385 FIN-5: a Refresh chooses the statements and the valuation per part, not per payload.

``core.vendor_data.read_financials`` returns a payload when *either* half of what a Fetch stored exists, so taking
it whole replaces a complete raw cache with half a payload:

- an intraday Fetch (ET 09:30-16:30) stores statements but no ``vendor_valuation`` row (that row needs a closed
  session), so ``.info`` reads empty and the model loses the market cap, beta, price and share count;
- an EDGAR-backed AG name has a valuation row but no statement rows (#150), so ``annual`` reads empty and the board
  says the financial data is missing although the raw cache is complete.

The Refresh therefore takes each part from the stored rows when that part exists and from the cache payload when it
does not. The annual window is the Fetch's as well: ``get_financials`` serves the newest six fiscal years, and the
store keeps every period, so a Refresh must read the same six back (at year seven the NWC/revenue averages, which
take every year, would otherwise differ from the Fetch's).

Offline: a temp DuckDB, an injected ``get_financials``; a Refresh runs with every provider patched to raise.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

import RevenueChaser.module_financials.run as fm
from RevenueChaser.core import rawstore, vendor_data
from RevenueChaser.core.providers import cache as cache_manager
from RevenueChaser.module_asymmetric_growth import engine as agh
from RevenueChaser.module_financials.engines import quick_remarks_engine as qr

# 2026-10-08 is a Thursday. 15:00 UTC = 11:00 ET: an intraday run, inside the session, before the close.
INTRADAY = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc).timestamp()
# 20:35 UTC = 16:35 ET: the session is complete, so the valuation row is written.
COMPLETE = datetime(2026, 10, 8, 20, 35, tzinfo=timezone.utc).timestamp()

_INFO = {"longName": "Test Corp.", "currency": "USD", "sector": "Technology", "sharesOutstanding": 1000.0,
         "marketCap": 100_000.0, "currentPrice": 100.0, "beta": 1.2, "enterpriseValue": 1.0e9}
# What an older raw cache would hold if the stored rows did not win: every number is different.
_DECOY_INFO = {"longName": "Decoy Corp.", "currency": "USD", "sharesOutstanding": 1.0, "marketCap": 5.0,
               "currentPrice": 5.0, "beta": 0.1, "enterpriseValue": 5.0e9}
_PEER_INFO = {"enterpriseValue": 9.0e9, "ebitda": 1.0e9, "totalRevenue": 4.0e9, "trailingPE": 18.0}


def _annual(first_year: int, years: int) -> list[dict]:
    """``years`` fiscal years from ``first_year``; revenue is a function of the year (rising 100 a year) so two
    ranges that share a year carry the same numbers."""
    out = []
    for year in range(first_year, first_year + years):
        rev = 100.0 * (year - 2000)
        out.append({
            "fiscal_year": str(year), "period_end": f"{year}-12-31", "revenue": rev,
            "ebitda": rev * 0.3, "ebit": rev * 0.25, "depreciation": rev * 0.05,
            "capex": rev * 0.05, "capital_expenditure": rev * 0.05, "net_income": rev * 0.19,
            "total_debt": 260.0, "cash": 120.0, "total_assets": rev * 1.8, "total_equity": rev,
            "receivables": rev * 0.1, "inventory": rev * 0.075, "payables": rev * 0.05,
            "current_assets": rev * 0.31, "current_liabilities": rev * 0.17,
            "interest_expense": 16.0, "pretax_income": rev * 0.24, "tax_provision": rev * 0.05,
        })
    return out


def _payload(annual: list[dict], info: dict | None = None) -> dict:
    """What ``yfinance.get_financials`` returns (``_payload`` in ``core/providers/yfinance.py``): its exact keys."""
    return {
        "ticker": "TEST", "info": copy.deepcopy(_INFO if info is None else info), "sector": "Technology",
        "annual": copy.deepcopy(annual),
        "shares_outstanding": 1000.0, "market_cap": 100_000.0, "enterprise_value": 1.0e9,
        "beta": 1.2, "current_price": 100.0, "currency": "USD", "source": "yfinance",
    }


def _failing(*_a, **_kw):
    raise AssertionError("a store-only Refresh called a provider")


def _fetch(monkeypatch, tmp_path, payload: dict, *, at: float = COMPLETE) -> dict:
    """A Financials Fetch with an injected provider: no network, the temp cache dir, the run's results."""
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(vendor_data, "_clock", lambda: at)
    monkeypatch.setattr(fm.yfinance_client, "get_financials", lambda t: copy.deepcopy(payload))
    monkeypatch.setattr(qr, "fetch_technical", lambda t: {})
    monkeypatch.setattr(agh, "score_ticker", lambda t, f, tech, meta=None: None)
    monkeypatch.setattr(fm.comps_engine, "_default_fetch", lambda t: dict(_PEER_INFO))
    return fm.run("TEST", modules=["dcf"], output="none", run_id="job-fetch")


def _refresh(monkeypatch, tmp_path) -> dict:
    """The store-only Refresh, with every provider patched to raise."""
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fm.yfinance_client, "get_financials", _failing)
    monkeypatch.setattr(fm.comps_engine, "_default_fetch", _failing)
    monkeypatch.setattr(qr, "fetch_technical", _failing)
    monkeypatch.setattr(agh, "score_ticker", _failing)
    return fm.run("TEST", modules=["dcf"], output="none", no_fetch=True)


def _rows(table: str) -> int:
    return len(rawstore.query(f"SELECT * FROM {table}"))


# ── T1: an intraday Fetch stores statements only ──────────────────────────────────────────────────────────────
def test_an_intraday_fetchs_valuation_comes_from_the_cache(rawstore_tmp_db, tmp_path, monkeypatch):
    """Statements were stored, the valuation row was not: the Refresh keeps the cache's market data."""
    _fetch(monkeypatch, tmp_path, _payload(_annual(2021, 3)), at=INTRADAY)
    assert _rows("vendor_financials") > 0 and _rows("vendor_valuation") == 0   # the case under test

    res = _refresh(monkeypatch, tmp_path)
    fin = res["results"]["financials"]
    assert fin["market_cap"] == 100_000.0 and fin["beta"] == 1.2
    assert fin["current_price"] == 100.0 and fin["shares_outstanding"] == 1000.0

    dcf = res["results"]["modules"]["dcf"]
    assert dcf["wacc"]["beta"] == 1.2
    assert dcf["wacc"]["equity_weight"] != 0.85            # not the fixed 0.85/0.15 fallback
    assert dcf["valuation"]["per_share_gordon"] is not None


# ── T2: the mirror — a valuation row but no statement rows (an EDGAR-backed AG name) ──────────────────────────
def test_a_valuation_only_fetchs_statements_come_from_the_cache(rawstore_tmp_db, tmp_path, monkeypatch):
    """The stored ``.info`` wins, the missing statements come from the cache: an annual series, not ``[]``."""
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    vendor_data.record(valuations=[vendor_data.valuation_row_yfinance("TEST", dict(_INFO))],
                       run_id="job-ag", run_at=COMPLETE)
    cache_manager.save("TEST", _payload(_annual(2021, 3)))
    assert _rows("vendor_financials") == 0 and _rows("vendor_valuation") == 1

    res = _refresh(monkeypatch, tmp_path)
    got = vendor_data.read_financials("TEST")
    assert got["annual"] == [] and got["market_cap"] == 100_000.0          # what the stored rows alone say

    fin = res["results"]["financials"]
    assert [r["fiscal_year"] for r in fin["annual"]] == ["2021", "2022", "2023"]
    assert res["results"]["modules"]["dcf"]["base_year"]["revenue"] == 2300.0
    assert "error" not in res["results"]["modules"]["dcf"]


# ── T3: both halves stored — FIN-5's own behaviour is unchanged ───────────────────────────────────────────────
def test_both_halves_stored_take_nothing_from_the_cache(rawstore_tmp_db, tmp_path, monkeypatch):
    """A decoy payload in the raw cache: the stored rows win for the statements and the valuation alike."""
    _fetch(monkeypatch, tmp_path, _payload(_annual(2021, 3)))
    assert _rows("vendor_financials") > 0 and _rows("vendor_valuation") == 1

    cache_manager.save("TEST", _payload(_annual(2010, 1), info=_DECOY_INFO))
    res = _refresh(monkeypatch, tmp_path)
    stored = vendor_data.read_financials("TEST")

    fin = res["results"]["financials"]
    assert fin["annual"] == stored["annual"]
    assert [r["fiscal_year"] for r in fin["annual"]] == ["2021", "2022", "2023"]
    assert fin["info"] == stored["info"] == _INFO
    assert fin["market_cap"] == 100_000.0 and fin["beta"] == 1.2 and fin["current_price"] == 100.0
    assert fin["shares_outstanding"] == 1000.0


# ── T4: the Refresh reads the same six years the Fetch serves ─────────────────────────────────────────────────
def test_a_refresh_reads_the_fetchs_six_most_recent_years(rawstore_tmp_db, tmp_path, monkeypatch):
    """Seven periods are stored (the store keeps every period), ``get_financials`` serves six: same DCF inputs."""
    seven = _annual(2017, 7)
    vendor_data.record(statements=vendor_data.statement_rows_yfinance("TEST", _payload(seven)),
                       run_id="job-old", run_at=COMPLETE)
    assert _rows("vendor_financials") == 3 * 7                      # three statements x seven fiscal years

    got = vendor_data.read_financials("TEST")
    assert [r["fiscal_year"] for r in got["annual"]] == ["2018", "2019", "2020", "2021", "2022", "2023"]
    assert [r["period_end"] for r in got["annual"]] == [f"{y}-12-31" for y in range(2018, 2024)]

    fetched = _fetch(monkeypatch, tmp_path, _payload(_annual(2018, 6)))      # what the provider serves today
    res = _refresh(monkeypatch, tmp_path)

    fetch_dcf = fetched["results"]["modules"]["dcf"]
    refresh_dcf = res["results"]["modules"]["dcf"]
    assert refresh_dcf["base_year"] == fetch_dcf["base_year"]
    assert refresh_dcf["historical_fcff"] == fetch_dcf["historical_fcff"]


# ── T5: neither half stored and no cache ──────────────────────────────────────────────────────────────────────
def test_with_neither_half_stored_and_no_cache_the_no_data_error_still_raises(rawstore_tmp_db, tmp_path,
                                                                              monkeypatch):
    """Nothing stored anywhere is still ``_NoCachedData``: the caller falls back to a Fetch."""
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    with pytest.raises(fm._NoCachedData):
        fm.run("TEST", modules=["dcf"], output="none", no_fetch=True)
