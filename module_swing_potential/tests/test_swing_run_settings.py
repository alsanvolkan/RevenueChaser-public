"""One Swing Fetch scores with one set of thresholds, and the board says the ones it used (Fable review of Swing,
28 Oct 2026, findings 1 and 2).

Finding 1: the funnel named ``config``'s price and dollar-volume floors while the prefilter dropped names on the
saved Settings values. Finding 2: the card's rule text, the stored gate rows and the forecast ledger's model version
each read the Settings store at their own moment, so a save during or after a Fetch put them on different
thresholds than the candidates were picked with.
"""
from __future__ import annotations

import pandas as pd

from RevenueChaser.core import board_settings as bs
from RevenueChaser.core import screen_common
from RevenueChaser.module_swing_potential import pipeline

B = "module_swing_potential"


def _fetch(monkeypatch, quotes: dict[str, dict]):
    """``run_scan`` over the given D04 quotes with no D01 bars (every kept name drops as ``no_bars``)."""
    monkeypatch.setattr(pipeline, "fetch_universe_stats", lambda **kw: (
        [{"ticker": t, "market_cap": 1e9, "price": q["price"], "name": t, "sector": "Energy"}
         for t, q in quotes.items()], {"out_of_universe": 0}))
    monkeypatch.setattr("RevenueChaser.core.quotes.prefilter_quotes", lambda tickers: dict(quotes))
    monkeypatch.setattr(screen_common, "load_bars", lambda *a, **k: (pd.DataFrame(), pd.DataFrame()))
    monkeypatch.setattr(pipeline, "load_ohlc", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "load_ticker_info", lambda *a, **k: {})
    return pipeline.run_scan()


def test_the_funnel_names_the_price_floor_the_prefilter_used(rawstore_tmp_db, monkeypatch):
    bs.save(B, {"MIN_PRICE": 10, "MIN_DOLLAR_VOLUME": 2_000_000})
    payload = _fetch(monkeypatch, {"MID": {"price": 7.5, "avg_volume": 1_000_000.0},
                                   "THIN": {"price": 50.0, "avg_volume": 10_000.0},
                                   "OK": {"price": 100.0, "avg_volume": 1_000_000.0}})
    steps = {s["key"]: s for s in payload["funnel_steps"]}
    assert steps["low_price"]["label"] == "Price under $10.00" and steps["low_price"]["tickers"] == ["MID"]
    assert steps["low_dollar_volume"]["label"] == "Average daily dollar volume under $2,000,000"
    assert steps["low_dollar_volume"]["tickers"] == ["THIN"]
