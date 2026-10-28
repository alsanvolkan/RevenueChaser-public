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


# ── finding 2: one settings read per Fetch, kept in the payload ─────────────────────────────────────────────────────
def _results():
    from RevenueChaser.module_swing_potential.tests.test_swing_history_278 import _ind   # vol_ratio 1.6
    return [{"ticker": "AAA", "name": "A", "ind": _ind()}]


def _fake_screen(monkeypatch, between=None):
    """``run_screen`` reduced to its order: prefilter, (a save, when given), score and payload, persist."""
    def fake(**kw):
        kw["prefilter"]([])
        if between:
            between()
        payload = kw["build_payload"](_results(), run_stats={})
        kw["persist"](_results())
        return payload
    monkeypatch.setattr(screen_common, "run_screen", fake)


def test_a_save_during_a_fetch_does_not_change_its_thresholds(rawstore_tmp_db, monkeypatch):
    _fake_screen(monkeypatch, between=lambda: bs.save(B, {"VOL_CONFIRM": 2.5}))
    sink: dict = {}
    payload = pipeline.run_scan(None, rows_sink=sink)
    assert payload["settings"]["vol_confirm"] == 1.5 and payload["settings"]["overrides"] == {}
    assert payload["passing"]["weinstein"] == 1
    vol = next(g for g in sink["gates"] if g["rule_id"] == "volume_confirmation")
    assert vol["passed"] == 1                                     # the stored gate row agrees with the card


def test_the_card_and_the_ledger_follow_the_fetch_not_a_later_save(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_swing_potential import freeze, report
    bs.save(B, {"VOL_CONFIRM": 1.4})
    _fake_screen(monkeypatch)
    payload = pipeline.run_scan(None)
    version = freeze.model_version(payload)
    bs.save(B, {"VOL_CONFIRM": 2.5, "STOP_ATR": 3.0})              # the next morning, before any Fetch
    html = report.render_swing_dashboard(payload)
    assert "≥ 1.4× 20-day avg" in html and "2.5×" not in html      # the modal's rule text (GATE_SPEC)
    assert "volume ≥1.4× average" in html and "entry − 2×ATR(14)" in html   # the Methodology table
    assert freeze.model_version(payload) == version != freeze.model_version()
    assert freeze.effective_config(payload)["VOL_CONFIRM"] == 1.4
    bs.reset(B)
    assert freeze.model_version(payload) == version                # a Reset after the Fetch changes nothing either
