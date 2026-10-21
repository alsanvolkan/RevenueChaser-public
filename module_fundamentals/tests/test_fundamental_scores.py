"""Fundamentals score/gate persistence (pure transforms + rawstore write)."""
from __future__ import annotations

from RevenueChaser.core import rawstore
from RevenueChaser.module_fundamentals import scores


def _results():
    m = {"rev_growth_ttm": 0.30, "fcf_margin": 0.20, "ro40": 50.0, "ttm_revenue": 1e9,
         "gross_margin": 0.40, "roic": 0.20, "net_debt_ebitda": 1.0, "pe": 30.0,
         "pb": 5.0, "dist_52w_pct": 0.90, "rs_6m": 0.10, "above_200d": True}
    return [{"ticker": "GOOD", "period": "Q1/2026", "metrics": m}]


def test_score_rows():
    rows = scores.score_rows(_results())
    assert len(rows) == 1
    r = rows[0]
    assert r["ticker"] == "GOOD" and r["multiplier"] == 3
    assert r["quality_pass"] == 3 and r["momentum_pass"] == 3
    assert r["distorted"] == 0 and r["above_200d"] == 1
    assert r["ro40"] == 50.0


def test_gate_rows_shape():
    rows = scores.gate_rows(_results())
    assert len(rows) == 7  # rule_of_40 (1) + quality (3) + momentum (3)
    assert all(set(r) == {"ticker", "gate", "rule_id", "passed", "actual", "unit"}
               for r in rows)
    assert {r["gate"] for r in rows} == {"rule_of_40", "quality", "momentum"}
    assert all(r["passed"] in (0, 1) for r in rows)


def test_persist_writes_both(rawstore_tmp_db):
    out = scores.persist(_results())
    assert out == {"scores": 1, "gates": 7}
    assert rawstore.count("fundamentals_scores") == 1
    assert rawstore.count("fundamentals_gates") == 7


def test_distorted_flag():
    rows = scores.score_rows([{"ticker": "WILD",
                               "metrics": {"rev_growth_ttm": 10.0, "ttm_revenue": 1e9}}])
    assert rows[0]["distorted"] == 1
