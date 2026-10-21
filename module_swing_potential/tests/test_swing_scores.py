"""Swing score/gate persistence (pure transforms + rawstore write)."""
from __future__ import annotations

from RevenueChaser.core import rawstore
from RevenueChaser.module_swing_potential import scores


def _ind(price=100.0, rs=0.30):
    return {
        "price": price, "sma50": 98.0, "sma150": 95.0, "sma200": 90.0,
        "sma200_prev_1m": 88.0, "sma150_prev_1m": 93.0, "ema10": 99.0,
        "ema20": 97.0, "ema10_prev": 98.0, "ema20_prev": 96.0,
        "above_52w_low_pct": 1.5, "within_52w_high_pct": 0.99, "vol_ratio": 1.6,
        "rs_vs_spy_3m": 0.05, "rs_vs_spy_12m": rs, "mom_12_1": 0.3,
        "consistency_6m": 0.8, "move_max_1_3m": 0.4, "adr_pct": 0.04,
        "atr14": 3.0, "change_pct": 0.01, "change_1w": 0.02, "change_1m": 0.05,
    }


def _results():
    return [{"ticker": "AAA", "ind": _ind(price=100.0, rs=0.30)},
            {"ticker": "BBB", "ind": _ind(price=50.0, rs=0.10)}]


def test_score_rows():
    rows = scores.score_rows(_results())
    assert {r["ticker"] for r in rows} == {"AAA", "BBB"}
    a = next(r for r in rows if r["ticker"] == "AAA")
    assert a["multiplier"] == 3
    assert a["ma_alignment"] == "aligned"
    assert a["entry"] == 100.0 and a["stop"] is not None
    assert a["rs_rating"] == 100.0


def test_gate_rows_shape():
    rows = scores.gate_rows(_results())
    assert len(rows) == 2 * 21  # minervini 8 + weinstein 5 + qullamaggie 6 + direction 2
    assert {r["gate"] for r in rows} == {"minervini", "weinstein", "qullamaggie", "direction"}
    assert all(set(r) == {"ticker", "gate", "rule_id", "passed", "actual", "unit"}
               for r in rows)


def test_persist_writes_both(rawstore_tmp_db):
    out = scores.persist(_results())
    assert out == {"scores": 2, "gates": 42}
    assert rawstore.count("swing_scores") == 2
    assert rawstore.count("swing_gates") == 42
