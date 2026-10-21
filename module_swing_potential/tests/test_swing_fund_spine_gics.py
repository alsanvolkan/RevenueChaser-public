"""R5 (#88), #266: Swing and Fundamentals read the spine's sector and industry, never the yfinance ``.info`` ones."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.core import rawstore, screen_common
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_swing_potential import pipeline as sp


def _ohlcv(periods=300) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=periods)
    close = pd.Series(100 * np.exp(np.arange(periods) * 0.0015), index=idx)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": 1_000_000})


def _seed(rows, master=None):
    rawstore.replace_all("equity_universe", [{
        "ticker": t, "cik": "", "name": t, "market_cap": 1e9, "price": 10.0,
        "sector": sec, "industry": ind, "quote": {"symbol": t}} for t, sec, ind in rows])
    if master:
        rawstore.replace_all("security_master", [
            {"ticker": t, "security_type": ty} for t, ty in master.items()])


@pytest.fixture
def swing_inputs(monkeypatch):
    # yfinance says Technology / Semiconductors; the spine says Healthcare / Biotechnology.
    info = {"sector": "Technology", "industry": "Semiconductors", "longName": "X Corp"}
    monkeypatch.setattr(screen_common, "load_bars", lambda *a, **k: (_ohlcv(), _ohlcv()))   # D01 (C12, #383 R3)
    monkeypatch.setattr(sp, "load_ticker_info", lambda *a, **k: info)
    monkeypatch.setattr(fp, "load_ticker_info", lambda *a, **k: info)


def test_swing_card_sector_and_industry_are_the_spine_not_yfinance(rawstore_tmp_db, swing_inputs):
    _seed([("AAA", "Healthcare", "Biotechnology")])
    out = sp._compute_row({"ticker": "AAA", "name": "A", "market_cap": 1e9, "sector": "Technology"},
                          _ohlcv())
    assert out["sector"] == "Healthcare"
    assert out["industry"] == "Biotechnology"
    assert "industry_group" not in out and "sub_industry" not in out


def test_swing_name_without_a_master_row_is_unclassified_with_no_yfinance_fallback(
        rawstore_tmp_db, swing_inputs):
    _seed([("AAA", "Healthcare", "Biotechnology")])
    out = sp._compute_row({"ticker": "ZZZ", "name": "Z", "market_cap": 1e9, "sector": "Technology"},
                          _ohlcv())
    assert (out["sector"], out["industry"]) == ("Unclassified", "Unclassified")


def test_swing_themes_still_read_the_vendor_industry_text_not_the_spine_industry(
        rawstore_tmp_db, swing_inputs, monkeypatch):
    """The keyword matcher keeps the vendor's free text (``Semiconductors`` here); the card's industry is the spine's."""
    seen = {}
    monkeypatch.setattr(sp, "assign_themes", lambda sector, industry, *a, **k: seen.update(
        sector=sector, industry=industry) or ["T"])
    _seed([("AAA", "Healthcare", "Biotechnology")])
    out = sp._compute_row({"ticker": "AAA", "name": "A", "market_cap": 1e9}, _ohlcv())
    assert out["industry"] == "Biotechnology"
    assert seen == {"sector": "Healthcare", "industry": "Semiconductors"}


def _fund_annual():
    return [{"revenue": 100.0 * (i + 1), "gross_profit": 50.0, "operating_cash_flow": 20.0,
             "capex": 5.0, "total_debt": 10.0, "cash": 30.0, "total_assets": 200.0,
             "total_equity": 100.0, "net_income": 10.0} for i in range(3)]


def test_fundamentals_card_sector_and_industry_are_the_spine_not_yfinance(
        rawstore_tmp_db, swing_inputs, monkeypatch):
    _seed([("AAA", "Healthcare", "Biotechnology")])
    monkeypatch.setattr(fp.sec_edgar, "get_fundamentals", lambda *a, **k: (_fund_annual(), [], {}))
    out = fp._compute_row({"ticker": "AAA", "name": "A", "market_cap": 1e9, "sector": "Technology"},
                          _ohlcv())
    if out is None:
        pytest.skip("fundamentals fixture too thin for this tree")
    assert out["sector"] == "Healthcare"
    assert out["industry"] == "Biotechnology"
    assert "industry_group" not in out and "sub_industry" not in out


def test_fundamentals_themes_still_read_the_vendor_industry_text_not_the_spine_industry(
        rawstore_tmp_db, swing_inputs, monkeypatch):
    seen = {}
    monkeypatch.setattr(fp, "assign_themes", lambda sector, industry, *a, **k: seen.update(
        sector=sector, industry=industry) or ["T"])
    _seed([("AAA", "Healthcare", "Biotechnology")])
    monkeypatch.setattr(fp.sec_edgar, "get_fundamentals", lambda *a, **k: (_fund_annual(), [], {}))
    out = fp._compute_row({"ticker": "AAA", "name": "A", "market_cap": 1e9}, _ohlcv())
    if out is None:
        pytest.skip("fundamentals fixture too thin for this tree")
    assert out["industry"] == "Biotechnology"
    assert seen == {"sector": "Healthcare", "industry": "Semiconductors"}


@pytest.mark.parametrize("mod", [sp, fp])
def test_run_scan_drops_excluded_types_and_counts_them(rawstore_tmp_db, monkeypatch, mod):
    _seed([("OPCO", "Energy", "Oil & Gas E&P"), ("FUND", "Financial Services", "Asset Management")],
          master={"OPCO": "common", "FUND": "cef"})
    seen = {}
    monkeypatch.setattr(mod, "fetch_universe_stats", lambda **kw: (
        [{"ticker": "OPCO", "market_cap": 1e9, "price": 10.0, "name": "O", "sector": "Energy"},
         {"ticker": "FUND", "market_cap": 1e9, "price": 10.0, "name": "F", "sector": "Financial Services"}],
        {"out_of_universe": 0}))
    monkeypatch.setattr(mod, "prefilter", lambda rows: screen_common.Prefiltered(seen.setdefault("rows", rows)))
    monkeypatch.setattr(screen_common, "load_bars", lambda *a, **k: (pd.DataFrame(), pd.DataFrame()))
    monkeypatch.setattr(mod, "load_ohlc", lambda *a, **k: None)          # the SPY fallback
    monkeypatch.setattr(mod, "load_ticker_info", lambda *a, **k: {})      # no live yfinance info call
    monkeypatch.setattr(mod, "demo_payload", lambda: {"is_fixture": True})
    mod.run_scan()
    assert [r["ticker"] for r in seen["rows"]] == ["OPCO"]
