"""A4 (Fable report review, 5 Nov 2026): the dealer book under the reports' two assumptions.

Black-Scholes greeks (r = 0) from each UW contract's own IV, summed over open interest: gamma (and gamma with spot
50 points lower), vanna, charm, vomma, zomma and speed. Assumption (a): dealers long calls and short puts (UW's own
sign convention); (b): dealers short every option. The greeks are checked against finite differences of the
price and of each other, not against a copy of the formulas.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma import dealer_book as db
from RevenueChaser.module_spx_gamma.chains import black_scholes_mid

S, K, T, V = 7700.0, 7650.0, 10 / 365, 0.16
H = 1e-3


def _g(s=S, t=T, v=V, cp="C"):
    return db.bs_greeks(s, K, t, v, cp)


def test_delta_and_gamma_match_the_price():
    h = 0.5
    up, dn = black_scholes_mid(S + h, K, T, V, "C"), black_scholes_mid(S - h, K, T, V, "C")
    mid = black_scholes_mid(S, K, T, V, "C")
    assert _g()["delta"] == pytest.approx((up - dn) / (2 * h), rel=1e-4)
    assert _g()["gamma"] == pytest.approx((up - 2 * mid + dn) / h ** 2, rel=1e-3)


@pytest.mark.parametrize("cp", ["C", "P"])
def test_higher_order_greeks_match_finite_differences(cp):
    g = _g(cp=cp)
    assert g["vanna"] == pytest.approx((_g(v=V + H, cp=cp)["delta"] - _g(v=V - H, cp=cp)["delta"]) / (2 * H), rel=1e-3)
    # charm: the change in delta as one year passes (time to expiry falls)
    assert g["charm"] == pytest.approx(-(_g(t=T + 1e-5, cp=cp)["delta"] - _g(t=T - 1e-5, cp=cp)["delta"]) / 2e-5,
                                       rel=1e-3)
    assert g["vomma"] == pytest.approx((_g(v=V + H, cp=cp)["vega"] - _g(v=V - H, cp=cp)["vega"]) / (2 * H), rel=1e-3)
    assert g["zomma"] == pytest.approx((_g(v=V + H, cp=cp)["gamma"] - _g(v=V - H, cp=cp)["gamma"]) / (2 * H), rel=1e-3)
    assert g["speed"] == pytest.approx((_g(s=S + 0.5, cp=cp)["gamma"] - _g(s=S - 0.5, cp=cp)["gamma"]) / 1.0, rel=1e-3)


def _row(strike, cp, oi, iv=0.16, expiry="2026-11-13"):
    return {"strike": strike, "type": cp, "expiry": expiry, "iv": iv, "open_interest": oi}


def test_book_signs_under_both_assumptions():
    rows = [_row(7700.0, "C", 1000), _row(7700.0, "P", 1000)]
    book = db.dealer_book(rows, S, "2026-11-04")
    a, b = book["a"], book["b"]
    # (a) long calls, short puts: the call and put gamma cancel; (b) short both: twice the put's, negative
    assert a["gamma"] == pytest.approx(0.0, abs=1e-6 * abs(b["gamma"]))
    assert b["gamma"] < 0
    assert book["contracts"] == 2 and book["expiries"] == ["2026-11-13"]


def test_gamma_50_points_lower_and_decision_words():
    rows = [_row(7600.0, "P", 5000), _row(7800.0, "C", 1000)]
    book = db.dealer_book(rows, S, "2026-11-04")
    assert book["a"]["gamma_down50"] != book["a"]["gamma"]
    assert book["a"]["words"]                   # a reading for each assumption
    assert book["b"]["words"]


def test_no_chain_no_book():
    assert db.dealer_book([], S, "2026-11-04") is None


def test_snapshot_and_page_carry_the_book():
    from RevenueChaser.module_spx_gamma import render
    from .conftest import make_snapshot
    snap = make_snapshot()
    assert snap["dealer_book"]["contracts"] > 0
    html = render.render_html(snap)
    assert "Dealer book" in html and "(a) long calls, short puts" in html
