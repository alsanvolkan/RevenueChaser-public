"""A9 (Fable report review, 5 Nov 2026): tail probabilities against 5-session frequencies, and the two missing
relations (vol/VIX and flow) of the reports' "six relations".

Implied: a two-sided normal on the 5-session log return with sigma = IV x sqrt(5/252). Realized: the share of
overlapping 5-session windows in the stored closes whose |log return| exceeded the same move.
"""
from __future__ import annotations

import math

import pytest

from RevenueChaser.module_spx_gamma import relations, tails
from .conftest import make_snapshot


def test_implied_tail_probability():
    sigma = 0.16 * math.sqrt(5 / 252)
    want = 2 * (1 - 0.5 * (1 + math.erf(0.02 / sigma / math.sqrt(2))))
    assert tails.implied_tail(0.16, 0.02) == pytest.approx(want)


def test_realized_frequency_over_overlapping_windows():
    closes = [100, 101, 102, 103, 104, 110, 110, 110, 110, 110, 104]
    # 5-session log moves: 100->110 9.5%, 101->110 8.5%, 102->110 7.6%, 103->110 6.6%, 104->110 5.6%, 110->104 5.6%
    out = tails.realized_tail(closes, 0.07)
    assert out == {"windows": 6, "hits": 3, "freq": pytest.approx(0.5)}


def test_tail_table_rows():
    closes = [100.0 * (1.001 ** i) for i in range(40)]
    rows = tails.tail_table(closes, 0.16)
    assert [r["move_pct"] for r in rows] == [2.0, 3.0, 5.0]
    assert all(r["implied"] is not None and r["realized"]["windows"] == 35 for r in rows)


def test_tail_table_needs_iv_and_closes():
    assert tails.tail_table([], 0.16) == []
    assert tails.tail_table([100.0] * 10, None) == []


def test_snapshot_carries_tails():
    rows = make_snapshot()["tails"]
    assert rows and rows[0]["move_pct"] == 2.0


def test_six_relations_include_vol_and_flow():
    snap = make_snapshot()
    snap["vix"] = {"vix": 19.4, "vix3m": 20.8, "term_mode": "contango"}
    snap["vega_flow"] = {"dir_vega_flow": 14e6, "side": "buyers", "verdict": "vega buyers (holds without the largest minute)"}
    greeks = [c["greek"] for c in relations.greek_relations(snap)]
    assert "Vol / VIX" in greeks and "Flow" in greeks
    vol = next(c for c in relations.greek_relations(snap) if c["greek"] == "Vol / VIX")
    assert "contango" in vol["english"]
