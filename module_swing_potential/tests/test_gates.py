"""Swing screener gates + prefilter — table-driven, no network.

Pin: prefilter thresholds (7-A since #383 R3 C12: price >= $5 and $vol >= $250K on D04's quote; the trend rules are
gone) and the auditable rules[] checklist contract for Minervini / Weinstein / Qullamaggie.
"""
import pytest

from RevenueChaser.module_swing_potential.pipeline import prefilter
from RevenueChaser.module_swing_potential.screeners import (
    evaluate_minervini,
    evaluate_qullamaggie,
    evaluate_weinstein,
)


@pytest.fixture
def d04(monkeypatch):
    """The quote the prefilter reads for "X" (``screen_common.prefilter_inputs``, the prices store since PR 10)."""
    held: dict = {}
    monkeypatch.setattr("RevenueChaser.core.screen_common.prefilter_inputs", lambda tickers: dict(held))

    def put(price=100.0, avg_volume=5000.0):
        held.clear()
        if price is not None or avg_volume is not None:
            held["X"] = {"price": price, "avg_volume": avg_volume, "session_date": None, "source": "yfinance"}
        return [{"ticker": "X", "quote": {}}]
    return put


def test_prefilter_ok(d04):
    pf = prefilter(d04())
    assert len(pf.kept) == 1 and pf.dropped == 0


def test_prefilter_price_below_min(d04):
    pf = prefilter(d04(price=3.0))
    assert pf.kept == [] and pf.drops == {"low_price": ["X"]}


def test_prefilter_missing_price(d04):
    pf = prefilter(d04(price=None))
    assert pf.kept == [] and pf.drops == {"no_quote": ["X"]}


def test_prefilter_dollar_volume_below_min(d04):
    # price 100 x avg volume 1000 = $100K < $250K
    pf = prefilter(d04(avg_volume=1000.0))
    assert pf.kept == [] and pf.drops == {"low_dollar_volume": ["X"]}


def test_prefilter_has_no_trend_rule(d04):
    """The 52-week distance and the 50-day average left the prefilter (7-A): the universe quote's trend fields are
    not read at all."""
    rows = d04()
    rows[0]["quote"] = {"regularMarketPrice": 100.0, "fiftyTwoWeekHigh": 200.0, "fiftyDayAverage": 110.0}
    assert len(prefilter(rows).kept) == 1


def test_prefilter_without_a_volume_skips_the_volume_rule(d04):
    assert len(prefilter(d04(avg_volume=None)).kept) == 1


# ── auditable rules[] checklist contract ──
_PASS = {
    "price": 100.0, "sma50": 95.0, "sma150": 90.0, "sma200": 80.0,
    "sma200_prev_1m": 78.0, "sma150_prev_1m": 88.0,
    "above_52w_low_pct": 1.5, "within_52w_high_pct": 0.99,
    "vol_ratio": 2.0, "rs_vs_spy_3m": 0.05, "rs_vs_spy_12m": 0.1,
    "ema10": 96.0, "ema20": 92.0, "ema10_prev": 90.0, "ema20_prev": 88.0,
    "move_max_1_3m": 0.35, "adr_pct": 0.04, "ret_3m": 0.10,
}


@pytest.mark.parametrize("name,gate,expected_score", [
    ("minervini", lambda: evaluate_minervini(_PASS, rs_rating=85.0), "8/8"),
    ("weinstein", lambda: evaluate_weinstein(_PASS), "5/5"),
    ("qullamaggie", lambda: evaluate_qullamaggie(_PASS), "6/6"),
])
def test_gate_rules_checklist(name, gate, expected_score):
    g = gate()
    assert g["name"]
    assert g["passed"] is True
    assert g["score"] == expected_score
    assert len(g["rules"]) >= 5
    for r in g["rules"]:
        assert r["id"] and r["label"] and r["unit"] and r["threshold"]
        assert isinstance(r["passed"], bool)
        assert "actual" in r
