"""Native OIPD math: SVI fit, Gatheral density, ProbCurve queries."""
from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.stats import norm

from RevenueChaser.module_oipd import opd_math


def _flat_smile(vol=0.25, T=0.25, n=41):
    k = np.linspace(-0.5, 0.5, n)
    return k, np.full_like(k, vol * vol * T)


class TestSviFit:
    def test_recovers_known_params(self):
        true = dict(a=0.02, b=0.15, rho=-0.6, m=0.02, sigma=0.12)
        k = np.linspace(-0.6, 0.6, 31)
        w = opd_math.svi_w(k, **true)
        fit = opd_math.fit_svi(k, w)
        assert fit is not None
        for key in ("a", "b", "rho", "m", "sigma"):
            assert abs(fit[key] - true[key]) < 1e-3, key

    def test_flat_variance_is_fitted(self):
        k, w = _flat_smile()
        fit = opd_math.fit_svi(k, w)
        assert fit is not None
        # a flat smile is reproduced by a near-constant total variance
        assert abs(fit["a"] - w[0]) < 1e-3
        assert fit["g_min"] >= 0.0

    def test_too_few_points_returns_none(self):
        assert opd_math.fit_svi([0.0, 0.1], [0.04, 0.05]) is None


class TestDensity:
    def test_flat_smile_matches_black_scholes_quantiles(self):
        F, T, r, vol = 100.0, 0.25, 0.04, 0.25
        k, w = _flat_smile(vol=vol, T=T)
        fit = opd_math.fit_svi(k, w)
        curve = opd_math.ProbCurve("2026-12-19", T, F, fit, r)
        sd = vol * math.sqrt(T)
        for q in (0.05, 0.5, 0.95):
            bs = F * math.exp(-0.5 * sd * sd + sd * norm.ppf(q))
            assert abs(curve.quantile(q) - bs) / bs < 0.01

    def test_density_mean_is_the_forward(self):
        F, T, r = 100.0, 0.2, 0.04
        params = dict(a=0.02, b=0.15, rho=-0.6, m=0.02, sigma=0.12)
        curve = opd_math.ProbCurve("x", T, F, params, r)
        assert abs(curve.stats()["mean"] - F) / F < 0.02

    def test_probabilities_are_consistent(self):
        params = dict(a=0.02, b=0.15, rho=-0.6, m=0.0, sigma=0.12)
        curve = opd_math.ProbCurve("x", 0.2, 100.0, params, 0.04)
        p_below = curve.prob_below(100.0)
        assert 0.0 <= p_below <= 1.0
        assert abs((p_below + curve.prob_above(100.0)) - 1.0) < 1e-9
        assert curve.prob_between(90.0, 110.0) > 0.0

    def test_quantiles_are_monotonic(self):
        params = dict(a=0.02, b=0.15, rho=-0.6, m=0.0, sigma=0.12)
        curve = opd_math.ProbCurve("x", 0.2, 100.0, params, 0.04)
        qs = [curve.quantile(q) for q in (0.05, 0.25, 0.5, 0.75, 0.95)]
        assert qs == sorted(qs)

    def test_g_min_flags_butterfly_arbitrage(self):
        # a wildly concave smile drives g(k) negative somewhere
        params = dict(a=0.01, b=0.9, rho=0.0, m=0.0, sigma=0.01)
        k = np.linspace(-1.0, 1.0, 201)
        assert float(np.min(opd_math.gatheral_g(k, **params))) < 0.0


class TestBlack76:
    def test_iv_round_trip(self):
        F, K, T, r, vol, cp = 100.0, 105.0, 0.5, 0.04, 0.3, "C"
        price = opd_math.black76_price(F, K, T, r, vol, cp)
        assert abs(opd_math.implied_vol_black76(F, K, T, r, price, cp) - vol) < 1e-6

    def test_no_root_below_intrinsic(self):
        assert opd_math.implied_vol_black76(100.0, 90.0, 0.5, 0.04, 0.0, "C") is None


_PARAMS = dict(a=0.01, b=0.1, rho=-0.3, m=0.0, sigma=0.2)


def _fd_greeks(F, T, r, iv, cp):
    h = F * 1e-3
    px = lambda f, s, t: opd_math.black76_price(f, F, t, r, s, cp)
    gamma = (px(F + h, iv, T) - 2 * px(F, iv, T) + px(F - h, iv, T)) / (h * h)
    dv = 1e-4
    vega = (px(F, iv + dv, T) - px(F, iv - dv, T)) / (2 * dv) / 100.0
    dt = 1e-5
    theta = (px(F, iv, T - dt) - px(F, iv, T + dt)) / (2 * dt) / 365.0
    return gamma, vega, theta


class TestAtmGreeks:
    """Black-76 ATM Greeks against a finite-difference oracle (#172 M2)."""

    def _check(self, F, cp):
        T, r = 30 / 365.0, 0.04
        g = opd_math.atm_greeks(_PARAMS, T, r, F, cp)
        gamma, vega, theta = _fd_greeks(F, T, r, g["iv"], cp)
        assert g["gamma"] == pytest.approx(gamma, rel=1e-4)
        assert g["vega"] == pytest.approx(vega, rel=1e-4)
        assert g["theta"] == pytest.approx(theta, rel=1e-4)

    def test_matches_finite_difference_at_spy_forward(self):
        self._check(600.0, "C")
        self._check(600.0, "P")

    def test_matches_finite_difference_at_unit_forward(self):
        self._check(1.0, "C")
        self._check(1.0, "P")

    def test_scaling_gamma_vega_theta_homogeneous_in_forward(self):
        T, r = 0.25, 0.04
        a = opd_math.atm_greeks(_PARAMS, T, r, 100.0)
        b = opd_math.atm_greeks(_PARAMS, T, r, 600.0)
        assert a["gamma"] * 100.0 == pytest.approx(b["gamma"] * 600.0)
        assert a["vega"] / 100.0 == pytest.approx(b["vega"] / 600.0)
        assert a["theta"] / 100.0 == pytest.approx(b["theta"] / 600.0)

    def test_put_call_parity_at_the_money(self):
        T, r = 0.5, 0.05
        c = opd_math.atm_greeks(_PARAMS, T, r, 600.0, "C")
        p = opd_math.atm_greeks(_PARAMS, T, r, 600.0, "P")
        assert c["delta"] - p["delta"] == pytest.approx(math.exp(-r * T))
        assert c["gamma"] == p["gamma"] and c["vega"] == p["vega"]
        assert c["theta"] == pytest.approx(p["theta"])


class TestHonestDensityChecks:
    """The clip and the window checks are reported, not hidden (#172 M3)."""

    def test_clean_smile_has_no_clipped_mass(self):
        k, w = _flat_smile()
        fit = opd_math.fit_svi(k, w)
        curve = opd_math.ProbCurve("x", 0.25, 100.0, fit)
        assert curve.clipped_mass < 1e-6
        assert curve.g_min_window >= -1e-9
        assert curve.min_var > 0

    def test_passes_the_fit_grid_but_clips_in_the_density_window(self):
        # g >= 0 on the +-0.5 fit grid (0.190) but negative on the density window (-0.137): the #172 M3 case
        steep = dict(a=-0.0378, b=0.5768, rho=-0.9827, m=-0.2705, sigma=0.4598)
        assert float(np.min(opd_math.gatheral_g(np.linspace(-0.5, 0.5, 61), **steep))) > 0.18
        curve = opd_math.ProbCurve("x", 0.25, 100.0, steep)
        assert curve.g_min_window == pytest.approx(-0.1365, abs=1e-3)
        assert 100.0 * curve.clipped_mass == pytest.approx(4.32, abs=0.02)

    def test_negative_min_variance_is_reported(self):
        bad = dict(a=-0.5, b=0.1, rho=0.0, m=0.0, sigma=0.1)
        curve = opd_math.ProbCurve("x", 0.25, 100.0, bad)
        assert curve.min_var < 0

    def test_min_var_is_the_raw_svi_minimum(self):
        p = dict(a=0.01, b=0.2, rho=0.5, m=0.0, sigma=0.3)
        curve = opd_math.ProbCurve("x", 0.25, 100.0, p)
        assert curve.min_var == pytest.approx(0.01 + 0.2 * 0.3 * math.sqrt(1 - 0.25))


class TestDensityCheck:
    """The board label and the flag are decided here, not in the JS (#172 D2)."""

    def check(self, g=0.2, gw=0.2, clip=0.0, mv=0.01, thr=0.01):
        return opd_math.density_check(g, gw, clip, mv, thr)

    def test_clean_density_is_ok(self):
        r = self.check()
        assert r == {"flag": False, "note": "butterfly ok", "g_min_shown": 0.2}

    def test_clip_threshold_is_strict(self):
        assert not self.check(clip=0.01)["flag"]
        r = self.check(clip=0.0101)
        assert r["flag"] and r["note"] == "density clipped 0.01%"
        assert self.check(clip=5.0, thr=5.0)["flag"] is False

    def test_window_g_min_flags_even_when_the_fit_grid_is_clean(self):
        r = self.check(g=0.19, gw=-0.137)
        assert r["flag"] and r["note"] == "negative density!" and r["g_min_shown"] == -0.137

    def test_fit_grid_g_min_flags_when_the_window_is_clean(self):
        assert self.check(g=-0.1, gw=0.3)["note"] == "negative density!"

    def test_clipped_note_wins_over_negative_g(self):
        assert self.check(g=-0.1, gw=-0.2, clip=4.32)["note"] == "density clipped 4.32%"

    def test_negative_min_variance_flags(self):
        r = self.check(mv=-0.01)
        assert r["flag"] and r["note"] == "min variance < 0"
