"""PR 8 (G3): Co-movement and the Relatedness peer market read the prices store (``prices.duckdb``), not D01.

``ohlc.price_frame`` is ``rcdata.read.ticker_bars(basis="total_return")`` pivoted; the span, the coverage, the flat
candidates and the "store changed" stamp all come from the same store. The old D01 tables are not read at all: a bar
that is only in ``price_bars_daily`` of the old store is no bar. Offline, temp store.
"""
from __future__ import annotations

import time
from datetime import date

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_comovement import config, engine, ohlc
from RevenueChaser.module_comovement.tests import test_recompute_parity as parity
from RevenueChaser.rcdata.tests import prices_store


def _rows(t: str, closes: pd.Series, **extra) -> list[dict]:
    return [{"ticker": t, "session_date": ts.date(), "close": float(p), **extra} for ts, p in closes.items()]


def _walk(idx, seed: int) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(idx)))), index=idx)


def _old_d01(ticker: str, idx) -> None:
    """Bars in the OLD store's ``price_bars_daily`` (D01), which the board must no longer read."""
    rawstore.write_rows("price_bars_daily", [{
        "ticker": ticker, "session_date": d.date(), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
        "adj_close": None, "volume": 1.0, "split_ratio": None, "adj_source": None, "source": "uw", "fetched_at": 1.0,
        "run_id": "old", "run_at": 1.0} for d in idx])


def test_price_frame_is_the_prices_store_total_return_close(rawstore_tmp_db):
    idx = pd.bdate_range("2026-06-01", periods=30)
    s = _walk(idx, 1)
    prices_store.put_bars(_rows("AAA", s) + _rows("SPY", _walk(idx, 2)))
    prices_store.put_action("AAA", idx[20].date(), "dividend", 2.0)
    _old_d01("ZZZ", idx)
    frame = ohlc.price_frame(["AAA", "SPY", "ZZZ"])
    assert list(frame.columns) == ["AAA", "SPY"]                          # ZZZ is only in the old D01
    factor = 1 - 2.0 / s.iloc[19]
    assert frame["AAA"].iloc[0] == pytest.approx(s.iloc[0] * factor)      # the dividend adjusts the older closes
    assert frame["AAA"].iloc[-1] == pytest.approx(s.iloc[-1])
    assert frame.index.name == "ts" and isinstance(frame.index, pd.DatetimeIndex)


def test_a_renamed_name_keeps_its_bars_from_before_the_rename(rawstore_tmp_db):
    idx = pd.bdate_range("2026-06-01", periods=20)
    s = _walk(idx, 3)
    prices_store.put_bars(_rows("OLDN", s.iloc[:10], security_id="figi:BBG1")
                          + _rows("NEWN", s.iloc[10:], security_id="figi:BBG1"), replace=False)
    frame = ohlc.price_frame(["NEWN"])
    assert list(frame.columns) == ["NEWN"] and len(frame) == 20
    assert ohlc.history_facts(["NEWN"])["first"] == {"NEWN": idx[0].strftime("%Y-%m-%d")}


def test_the_span_is_two_years_back_from_the_stores_newest_session(rawstore_tmp_db):
    idx = pd.bdate_range("2023-01-03", "2026-06-30")
    prices_store.put_bars(_rows("AAA", _walk(idx, 4)))
    _old_d01("AAA", pd.bdate_range("2026-07-01", periods=5))              # a newer old-store bar moves nothing
    assert ohlc.history_start() == date(2024, 6, 30)
    frame = ohlc.price_frame(["AAA"])
    assert frame.index[-1] == pd.Timestamp("2026-06-30") and frame.index[0] >= pd.Timestamp("2024-06-30")
    assert ohlc.window_start(["AAA"], 5) == idx[-5].strftime("%Y-%m-%d")
    assert ohlc.sessions_held("AAA") == len(frame)


def test_fetch_bulk_counts_the_prices_store(rawstore_tmp_db):
    idx = pd.bdate_range("2026-06-01", periods=10)
    prices_store.put_bars(_rows("AAA", _walk(idx, 5)) + _rows("SPY", _walk(idx, 6)))
    _old_d01("BBB", idx)
    assert ohlc.fetch_bulk(["AAA", "BBB"]) == {"requested": 5, "cached": 2, "pending": 3, "fetched": 0,
                                               "failed": 0, "no_fetch": False}


def test_a_split_name_is_no_flat_candidate_when_its_closes_move(rawstore_tmp_db):
    """D01 had to count every split name as a candidate (a bar's split depended on when it was fetched); the prices
    store's bars are as traded, so only a run of unmoved steps (or ex-dates) makes one."""
    idx = pd.bdate_range("2026-01-02", periods=30)
    prices_store.put_bars(_rows("SPL", _walk(idx, 7)) + _rows("ONE", _walk(idx, 8)))
    prices_store.put_action("SPL", idx[10].date(), "split", 2.0)
    facts = ohlc.history_facts(["SPL", "ONE"])
    assert facts["flat"] == [] and len(facts["sessions"]) == 30


def test_a_new_action_in_the_store_retires_the_stored_run(rawstore_tmp_db):
    tickers = parity._seed(parity._market())
    stored = engine.run(no_fetch=True, persist=True, tickers=tickers, window=252)
    assert engine.run(no_fetch=True, tickers=tickers, window=252)["as_of"] == stored["as_of"]
    time.sleep(0.01)
    prices_store.put_action("B0N00", parity._market().index[-30].date(), "dividend", 1.0)
    assert engine.run(no_fetch=True, tickers=tickers, window=252)["as_of"] != stored["as_of"]


def test_the_about_box_names_the_prices_store(rawstore_tmp_db):
    tickers = parity._seed(parity._market())
    src = engine.run(no_fetch=True, tickers=tickers, window=252)["source"]
    assert src["table"] == "prices.duckdb → price_bar_daily + adj_factor"
    assert "rcdata.read.ticker_bars" in src["detail"] and "Alpaca" in src["provider"]


def test_a_run_of_the_d01_method_is_never_served(rawstore_tmp_db):
    """The closes changed source, so the method moved and a stored D01-era run no longer matches its hash."""
    assert config.METHOD == 3


def test_the_missing_bars_step_says_a_session_can_come_back():
    """Review S2, revision-aware: the prices build re-reads the last sessions and the history run fills older gaps,
    so a missing session is not lost for good (D01 never went back)."""
    steps = engine.funnel_steps({"scanned_total": 10, "below_floor": 0, "no_market_cap": 0, "no_prices": 0,
                                 "short_history": 3, "listed_recently": 1, "missing_bars": 2, "flat": 0,
                                 "clustered": 7, "window": 126, "min_obs": 119})
    missing = next(s for s in steps if s["key"] == "missing_bars")
    assert "can come back" in missing["label"]
