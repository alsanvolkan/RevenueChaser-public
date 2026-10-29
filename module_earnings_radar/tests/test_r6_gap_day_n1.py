"""Fable review N1 (Earnings Radar, R6): the yield beta's 60-session window kept the last print's reaction day, and
one +/-20% gap moves an OLS slope a lot. docs/rules/redesign.md R6: "Earnings-gap days are dropped from beta". The
return that ends on a completed print's reaction session (core/reaction_anchor) now leaves both the beta and its
t-stat. Offline."""
from datetime import date, timedelta

import pytest

from RevenueChaser.module_earnings_radar.pipeline import _gap_sessions, r6_beta, yield_beta_tstat
from RevenueChaser.module_earnings_radar.rules.rule6_macro import yield_beta

GAP_DAY = "2026-01-20"   # day 15 of the series


def _series(n, duration):
    """n dated (stock, yield-percent) points whose OLS beta is exactly -duration (test_rule6_macro's shape)."""
    stock, yld, price, y = {}, {}, 100.0, 4.0
    for i in range(n):
        d = (date(2026, 1, 5) + timedelta(days=i)).isoformat()
        stock[d], yld[d] = price, y
        step = 0.10 * (0.5 + (i % 3) * 0.5)
        price *= 1.0 - duration * step / 100.0
        y += step
    return stock, yld


def _gapped():
    stock, yld = _series(n=40, duration=10.0)
    return {d: (p * 1.2 if d >= GAP_DAY else p) for d, p in stock.items()}, yld


def test_a_gap_day_bends_the_beta_until_it_is_left_out():
    stock, yld = _gapped()
    assert abs(yield_beta(stock, yld) + 10.0) > 1.0, "the +20% gap moves the slope"
    assert yield_beta(stock, yld, exclude={GAP_DAY}) == pytest.approx(-10.0, abs=0.05)


def test_the_t_stat_reads_the_same_pairs():
    stock, yld = _gapped()
    beta = yield_beta(stock, yld, exclude={GAP_DAY})
    t_out = yield_beta_tstat(stock, yld, beta, exclude={GAP_DAY})
    t_in = yield_beta_tstat(stock, yld, beta)
    assert t_out is None or abs(t_out) > 1e6, "a (near) exact fit once the gap is out"
    assert t_in is not None and abs(t_in) < 1e3, "the gap's residual is in the error"


def test_r6_beta_drops_the_reaction_sessions_it_is_given():
    stock, yld = _gapped()
    bars = [{"date": d, "close": c} for d, c in stock.items()]
    beta, _ = r6_beta(bars, yld, {GAP_DAY})
    assert beta == pytest.approx(-10.0, abs=0.05)


BARS = [{"date": d, "close": 100.0} for d in ("2026-01-15", "2026-01-16", "2026-01-20", "2026-01-21")]


def test_the_reaction_session_of_each_completed_print():
    hist = [{"report_date": "2026-01-16", "report_time": "postmarket", "actual_eps": 1.0},   # AMC: next session
            {"report_date": "2026-01-21", "report_time": "premarket", "actual_eps": None}]   # not reported yet
    assert _gap_sessions(hist, BARS) == {"2026-01-20"}
    assert _gap_sessions([{"report_date": "2026-01-16", "report_time": "premarket", "actual_eps": 1.0}], BARS) == \
        {"2026-01-16"}
    assert _gap_sessions([{"report_date": "2025-10-16", "report_time": "premarket", "actual_eps": 1.0}], BARS) == \
        set(), "a print older than the window has no session in it"
