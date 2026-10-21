"""#379 D01 switch: Co-movement and the Relatedness peer market read D01, not ``swing_ohlc``.

Both now read ``core.prices.daily_bars(basis="adjusted")`` (the split/dividend-adjusted closes of ``price_bars_daily``
plus ``price_actions``) through its windowed read (K8a), over the last ``config.OHLC_PERIOD`` of sessions. Daily Bars
is D01's one writer, so a Co-movement Fetch calls no provider for bars and writes no ``swing_ohlc`` row; a Refresh
stays zero-network. Offline, temp store.
"""
from __future__ import annotations

import json
import time
from datetime import date

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.core import prices, rawstore
from RevenueChaser.module_comovement import config, engine, ohlc
from RevenueChaser.module_comovement import returns as returns_mod
from RevenueChaser.module_comovement.tests import d01_bars
from RevenueChaser.module_comovement.tests import test_recompute_parity as parity


def _swing_rows(t: str, closes: pd.Series, at: float = 1.0) -> list[dict]:
    return [{"ticker": t, "ts": ts.isoformat(), "open": float(p), "high": float(p), "low": float(p),
             "close": float(p), "volume": 1.0, "cached_at": at} for ts, p in closes.items() if not np.isnan(p)]


def _walk(idx, seed: int) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(idx)))), index=idx)


# ── the reader ──────────────────────────────────────────────────────────────
def test_price_frame_is_the_d01_adjusted_close(rawstore_tmp_db):
    """D01's adjusted closes, not the stored nominal ones, and never a ``swing_ohlc`` row."""
    idx = pd.bdate_range("2025-06-02", periods=30)
    s = _walk(idx, 1)
    d01_bars.write_bars(_swing_rows("AAA", s) + _swing_rows("SPY", _walk(idx, 2)))
    d01_bars.write_action("AAA", idx[20].date(), "dividend", 2.0)
    rawstore.write_rows("swing_ohlc", _swing_rows("AAA", s * 3) + _swing_rows("ZZZ", s))
    frame = ohlc.price_frame(["AAA", "SPY", "ZZZ"])
    adj = prices.daily_bars(["AAA"], basis="adjusted")
    assert list(frame.columns) == ["AAA", "SPY"]                          # ZZZ is only in swing_ohlc
    assert np.allclose(frame["AAA"].to_numpy(), adj["close"].to_numpy())
    assert frame["AAA"].iloc[0] < s.iloc[0]                               # the dividend adjusts the older closes
    assert frame["AAA"].iloc[-1] == pytest.approx(s.iloc[-1])


def test_price_frame_reads_the_last_two_years_of_d01(rawstore_tmp_db):
    """The history the old 2y ``swing_ohlc`` cache held: D01's newest session minus ``OHLC_PERIOD``."""
    idx = pd.bdate_range("2022-01-03", "2025-06-30")
    d01_bars.write_bars(_swing_rows("AAA", _walk(idx, 3)))
    frame = ohlc.price_frame(["AAA"])
    assert frame.index[-1] == pd.Timestamp("2025-06-30")
    assert frame.index[0] >= pd.Timestamp("2023-06-30") and frame.index[0] <= pd.Timestamp("2023-07-05")
    assert ohlc.history_start() == date(2023, 6, 30)
    assert ohlc.window_start(["AAA"], 10_000) is None                     # no more sessions than the lookback
    assert ohlc.history_facts(["AAA"])["first"]["AAA"] == frame.index[0].strftime("%Y-%m-%d")


def test_window_start_counts_d01_sessions(rawstore_tmp_db):
    idx = pd.bdate_range("2025-01-02", periods=40)
    d01_bars.write_bars(_swing_rows("AAA", _walk(idx, 4)))
    assert ohlc.window_start(["AAA"], 5) == idx[-5].strftime("%Y-%m-%d")
    assert ohlc.window_start(["aaa"], 5) == idx[-5].strftime("%Y-%m-%d")


def test_history_facts_find_a_flat_run_that_only_the_adjusted_closes_show(rawstore_tmp_db, monkeypatch):
    """The close drops by exactly the dividend on its ex-date: unequal stored closes, equal adjusted ones.

    SQL sees only the stored closes, so the ex-date makes that step a candidate; the adjusted frame then decides,
    and a store-only run still matches the full read."""
    idx = pd.bdate_range("2025-01-02", periods=60)
    s = _walk(idx, 5)
    s.iloc[30:33] = 10.0
    s.iloc[33:36] = 9.0                    # ex-date idx[33]: 10.00 -> 9.00 with a 1.00 dividend (m = 0.9)
    d01_bars.write_bars(_swing_rows("DIV", s) + _swing_rows("SPY", _walk(idx, 6)))
    d01_bars.write_action("DIV", idx[33].date(), "dividend", 1.0)
    whole = ohlc.price_frame(["DIV"])
    assert whole["DIV"].iloc[30:36].nunique() == 1                         # 5 unchanged steps, 2 in the stored closes
    assert returns_mod.flat_print_names(whole, run=config.FLAT_RUN, max_share=config.FLAT_SHARE) == ["DIV"]
    assert ohlc.history_facts(["DIV", "SPY"])["flat"] == ["DIV"]


def test_history_facts_count_a_split_name_as_a_candidate(rawstore_tmp_db):
    """A split's factor depends on each bar's ``fetched_at``, so stored closes cannot rule a split name out."""
    idx = pd.bdate_range("2025-01-02", periods=30)
    d01_bars.write_bars(_swing_rows("SPL", _walk(idx, 7)) + _swing_rows("ONE", _walk(idx, 8)))
    d01_bars.write_action("SPL", idx[10].date(), "split", 2.0)
    facts = ohlc.history_facts(["SPL", "ONE"])
    assert facts["flat"] == ["SPL"]
    assert len(facts["sessions"]) == 30


# ── the Fetch ───────────────────────────────────────────────────────────────
@pytest.fixture
def no_provider(monkeypatch):
    """Every yfinance download is recorded and refused."""
    import yfinance

    calls = []

    def refuse(*a, **k):
        calls.append((a, k))
        raise AssertionError("a Co-movement Fetch must not download bars")

    monkeypatch.setattr(yfinance, "download", refuse)
    return calls


def test_a_fetch_downloads_nothing_and_clusters_d01(rawstore_tmp_db, no_provider):
    tickers = parity._seed(parity._market())
    p = engine.run(no_fetch=False, tickers=tickers, window=252)
    assert no_provider == []
    assert rawstore.count("swing_ohlc") == 0                               # no swing_ohlc write either
    assert p["config_hash"] and p["eligible"] > 100
    f = p["fetch"]
    assert (f["fetched"], f["failed"]) == (0, 0)
    assert (f["requested"], f["pending"]) == (len(tickers) + 3, 2)          # + SPY, DIA, QQQ; D01 lacks DIA, QQQ


def test_fetch_bulk_reports_what_d01_lacks(rawstore_tmp_db, no_provider):
    idx = pd.bdate_range("2025-01-02", periods=10)
    d01_bars.write_bars(_swing_rows("AAA", _walk(idx, 9)) + _swing_rows("SPY", _walk(idx, 10)))
    out = ohlc.fetch_bulk(["AAA", "BBB"])
    assert out == {"requested": 5, "cached": 2, "pending": 3, "fetched": 0, "failed": 0, "no_fetch": False}
    assert ohlc.fetch_bulk(["AAA"], no_fetch=True)["no_fetch"] is True
    assert no_provider == []


# ── a stored run is served only while D01 has not changed under it ──────────
def test_a_new_dividend_retires_the_stored_run(rawstore_tmp_db):
    """A dividend written after the run changes the adjusted history without moving the last date."""
    tickers = parity._seed(parity._market())
    stored = engine.run(no_fetch=True, persist=True, tickers=tickers, window=252)
    served = engine.run(no_fetch=True, tickers=tickers, window=252)
    assert served["as_of"] == stored["as_of"]
    time.sleep(0.01)
    d01_bars.write_action("B0N00", parity._market().index[-30].date(), "dividend", 1.0)
    again = engine.run(no_fetch=True, tickers=tickers, window=252)
    assert again["as_of"] != stored["as_of"]


def test_the_about_box_names_d01(rawstore_tmp_db):
    tickers = parity._seed(parity._market())
    p = engine.run(no_fetch=True, tickers=tickers, window=252)
    assert "price_bars_daily" in p["source"]["table"]
    assert "swing_ohlc" not in json.dumps(p["source"])


# ── the Relatedness peer market ─────────────────────────────────────────────
def test_the_peer_market_reads_d01(rawstore_tmp_db):
    from RevenueChaser.module_relatedness import engine as rel_engine
    from RevenueChaser.module_relatedness.tests.test_pure_refresh import _market_peers, _market_prices

    px = _market_prices(n=300)
    d01_bars.write_bars([r for t in px.columns for r in _swing_rows(t, px[t])])
    peers = _market_peers()
    rel_engine._attach_peer_market(peers)
    nvda = {p["peer"]: p for p in peers["NVDA"]}
    assert nvda["AMD"].get("move") and "beyond_spy" in nvda["AMD"]


# ── the Settings window rule counts what Co-movement reads ──────────────────
def test_stored_sessions_counts_spy_in_d01(rawstore_tmp_db):
    from RevenueChaser.core import board_settings as bs

    idx = pd.bdate_range("2025-01-02", periods=12)
    d01_bars.write_bars(_swing_rows("SPY", _walk(idx, 11)))
    rawstore.write_rows("swing_ohlc", _swing_rows("SPY", _walk(pd.bdate_range("2024-01-02", periods=30), 12)))
    assert bs.stored_sessions() == 12
    assert ohlc.sessions_held("SPY") == 12


# ── the data-as-of chip dates the names Co-movement clusters ────────────────
def test_the_data_date_is_the_d01_date_of_its_names(rawstore_tmp_db):
    from RevenueChaser.core import provenance

    d01_bars.write_bars(_swing_rows("AAA", _walk(pd.bdate_range(end="2026-10-13", periods=5), 13))
                        + _swing_rows("OTHER", _walk(pd.bdate_range(end="2026-10-14", periods=5), 14)))
    rawstore.write_rows("swing_ohlc", _swing_rows("AAA", _walk(pd.bdate_range(end="2026-10-15", periods=5), 15)))
    rawstore.replace_all("equity_universe", [{"ticker": "AAA", "market_cap": 1.0e10, "sector": "Technology",
                                              "price": 10.0, "name": "AAA", "industry": "Software",
                                              "quote": {"symbol": "AAA"}}])
    assert provenance.data_as_of("module_comovement") == "2026-10-13"


def test_the_board_never_says_a_fetch_downloads_prices():
    # The Daily Bars job fills D01; the board's own texts must not send the user to Fetch for prices (RB-8)
    from RevenueChaser.module_comovement import report
    from RevenueChaser.module_comovement.tests.test_comovement import _report_payload
    empty = report.render_comovement_dashboard({"as_of": "2026-09-17T00:00:00+00:00", "clusters": {}, "cuts": []})
    missing = report.render_comovement_dashboard(_report_payload(benchmark_applied=False))
    stale = report.render_comovement_dashboard(_report_payload(benchmark_status="stale", window_end="2026-09-10"))
    for html in (empty, missing, stale):
        assert "to download it" not in html and "fill daily OHLC" not in html
        assert "Run <b>Fetch</b> to update" not in html
    assert "has no usable prices in the store" in missing and "ends 2026-09-10" in stale
    assert all("<b>Daily Bars</b>" in html for html in (empty, missing, stale))
