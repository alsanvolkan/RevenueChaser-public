"""What the Swing board says about its rules and sources matches what the code does (Fable review finding 9, PR 6).

The Minervini gate is decided by its seven price rules; the RS rating is shown and counted for quality but does not
decide (#65, ``screeners.evaluate_minervini``). The names come from the universe (the identity spine by default) and
the bars from the store's daily bars, which the nightly fills from Unusual Whales with a yfinance fallback; the board
itself never calls yfinance for bars (``screen_common.load_bars``).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from RevenueChaser.app import runners
from RevenueChaser.module_swing_potential import report, screeners
from RevenueChaser.module_swing_potential.tests.test_gates import _PASS
from RevenueChaser.module_swing_potential.tests.test_swing_history_278 import _payload


def _page() -> str:
    return report.render_swing_board(_payload())[0]


def test_the_rules_table_says_seven_price_rules_decide_minervini():
    weak_rs = screeners.evaluate_minervini(_PASS, rs_rating=10.0)
    assert weak_rs["passed"] and weak_rs["total"] == 8        # RS fails, the seven price rules still pass the gate
    page = _page()
    assert "8/8 required" not in page
    assert f"{weak_rs['total'] - 1}/{weak_rs['total'] - 1} price rules required" in page
    assert "RS rating ≥ 70 ranks, does not decide" in page


@pytest.mark.parametrize("source, names", [("spine", "identity spine (names)"),
                                           ("store", "old universe store (names)")])
def test_the_footer_names_where_the_names_and_the_bars_come_from(monkeypatch, source, names):
    monkeypatch.setattr(report._universe, "universe_source", lambda: source)
    page = _page()
    assert "Yahoo Finance (universe + OHLCV)" not in page
    assert f"Sources: {names}" in page
    assert "daily bars (Unusual Whales, yfinance fallback)" in page


def test_the_swing_runner_does_not_claim_yfinance_only():
    doc = ast.get_docstring(next(n for n in ast.parse(Path(runners.__file__).read_text(encoding="utf-8")).body
                                 if isinstance(n, ast.FunctionDef) and n.name == "run_swing"))
    assert "yfinance-only" not in doc and "daily bars" in doc
