"""Native OIPD pipeline: SVI smile -> risk-neutral price distribution.

OIPD (https://github.com/Open-Lemma/options-implied-probability) fits an
arbitrage-free SVI implied-volatility surface to a listed option chain and
transforms the fitted surface into a risk-neutral probability distribution.

RevenueChaser reimplements that pipeline here with numpy/scipy rather than
depending on the upstream ``oipd`` package: ``oipd`` pins ``plotly<6``, which
conflicts with this repo's ``plotly>=6`` SEC-13F charts, and pip resolves the
conflict to an old ``oipd`` (1.x) whose API differs from the documented 2.x.
The methodology is unchanged.

Pipeline (per expiry):
  1. log-moneyness ``k = ln(K/F)`` and total variance ``w = sigma^2 * T``.
  2. fit raw SVI ``w(k) = a + b (rho (k-m) + sqrt((k-m)^2 + sigma^2))`` by
     weighted least squares, with a butterfly-arbitrage penalty on ``g(k)``.
  3. risk-neutral density in log-strike via the Gatheral ``g`` function:
     ``p(k) = g(k) / sqrt(2 pi w) * exp(-d2^2 / 2)``, ``d2 = -k/sqrt(w) - sqrt(w)/2``.
  4. CDF by cumulative trapezoid, then quantiles / tail probabilities.

References
- Gatheral & Jacquier, "Arbitrage-free SVI volatility surfaces" (2014).
- Gatheral, "The Volatility Surface" (2006) — density in terms of ``w(k)``.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.integrate import cumulative_trapezoid
from scipy.optimize import brentq, least_squares
from scipy.special import ndtr  # standard-normal CDF (vectorized)

_SQRT_2PI = math.sqrt(2.0 * math.pi)

# numpy renamed ``trapz`` -> ``trapezoid`` in 2.0; support the repo's >=1.24 pin.
_trapz = getattr(np, "trapezoid", None) or np.trapz


def _norm_pdf(x: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * x * x) / _SQRT_2PI


# ── Black-76 (forward) pricing + IV inversion ───────────────────────────────
def black76_price(F: float, K: float, T: float, r: float, sigma: float, cp: str) -> float:
    """European option price on a forward ``F`` with rate ``r`` (Black-76)."""
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        disc = math.exp(-r * max(T, 0.0))
        intrinsic = max(F - K, 0.0) if cp == "C" else max(K - F, 0.0)
        return disc * intrinsic
    sqrt_t = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc = math.exp(-r * T)
    if cp == "C":
        return disc * (F * ndtr(d1) - K * ndtr(d2))
    return disc * (K * ndtr(-d2) - F * ndtr(-d1))


def implied_vol_black76(F: float, K: float, T: float, r: float, price: float,
                        cp: str, lo: float = 1e-4, hi: float = 5.0) -> float | None:
    """Invert Black-76 to an implied vol; ``None`` when no root is bracketed."""
    try:
        if not (F > 0 and K > 0 and T > 0) or price is None or price <= 0:
            return None
        disc = math.exp(-r * T)
        intrinsic = disc * (max(F - K, 0.0) if cp == "C" else max(K - F, 0.0))
        if price < intrinsic - 1e-9:
            return None

        def f(sig: float) -> float:
            return black76_price(F, K, T, r, sig, cp) - price

        if f(lo) * f(hi) > 0:
            return None
        return float(brentq(f, lo, hi, xtol=1e-6, rtol=1e-8, maxiter=100))
    except (ValueError, OverflowError):
        return None


# ── Raw SVI parameterization ────────────────────────────────────────────────
def svi_w(k, a: float, b: float, rho: float, m: float, sigma: float):
    """Total implied variance ``w(k)`` under raw SVI."""
    d = np.asarray(k, dtype=float) - m
    return a + b * (rho * d + np.sqrt(d * d + sigma * sigma))


def svi_dw(k, a: float, b: float, rho: float, m: float, sigma: float):
    d = np.asarray(k, dtype=float) - m
    R = np.sqrt(d * d + sigma * sigma)
    return b * (rho + d / R)


def svi_d2w(k, a: float, b: float, rho: float, m: float, sigma: float):
    d = np.asarray(k, dtype=float) - m
    R = np.sqrt(d * d + sigma * sigma)
    return b * (sigma * sigma) / (R ** 3)


def gatheral_g(k, a: float, b: float, rho: float, m: float, sigma: float):
    """Gatheral's ``g(k)``: the density is ``g/sqrt(2 pi w) * exp(-d2^2/2)``.

    ``g(k) < 0`` anywhere means butterfly arbitrage (a negative density).
    """
    k = np.asarray(k, dtype=float)
    w = svi_w(k, a, b, rho, m, sigma)
    wp = svi_dw(k, a, b, rho, m, sigma)
    wpp = svi_d2w(k, a, b, rho, m, sigma)
    return (1.0 - k * wp / (2.0 * w)) ** 2 - (wp ** 2 / 4.0) * (1.0 / w + 0.25) + wpp / 2.0


def rnd_log_density(k, a: float, b: float, rho: float, m: float, sigma: float):
    """Risk-neutral density in log-strike ``k = ln(K/F)``."""
    k = np.asarray(k, dtype=float)
    w = svi_w(k, a, b, rho, m, sigma)
    w = np.clip(w, 1e-12, None)
    d2 = -k / np.sqrt(w) - np.sqrt(w) / 2.0
    g = gatheral_g(k, a, b, rho, m, sigma)
    return g / np.sqrt(2.0 * math.pi * w) * np.exp(-d2 * d2 / 2.0)


def fit_svi(k, w, g_floor: float = 0.0, penalty_weight: float = 25.0,
            min_points: int = 5) -> dict | None:
    """Least-squares fit of raw SVI to ``(k, w)`` total variance.

    Returns ``{a, b, rho, m, sigma, g_min, rmse}`` or ``None`` when there is not
    enough data or the optimiser fails. A penalty term pushes ``g(k)`` above
    ``g_floor`` on a dense grid so the fitted smile stays butterfly-arbitrage-free.
    """
    k = np.asarray(k, dtype=float)
    w = np.asarray(w, dtype=float)
    ok = np.isfinite(k) & np.isfinite(w) & (w > 0)
    k, w = k[ok], w[ok]
    if k.size < min_points:
        return None

    k_min, k_max = float(k.min()), float(k.max())
    lo = np.array([-1.0, 1e-6, -0.999, k_min - 1.0, 1e-4])
    hi = np.array([2.0, 5.0, 0.999, k_max + 1.0, 2.0])
    x0 = np.array([max(1e-4, float(w.min()) * 0.5), 0.1, -0.3, 0.0, 0.1])
    x0 = np.clip(x0, lo + 1e-9, hi - 1e-9)

    k_grid = np.linspace(min(k_min, -0.5), max(k_max, 0.5), 61)

    def resid(x):
        a, b, rho, m, sigma = x
        r = svi_w(k, a, b, rho, m, sigma) - w
        g = gatheral_g(k_grid, a, b, rho, m, sigma)
        pen = math.sqrt(penalty_weight) * np.clip(g_floor - g, 0.0, None)
        return np.concatenate([r, pen])

    try:
        res = least_squares(resid, x0, bounds=(lo, hi), max_nfev=4000,
                            method="trf")
    except (ValueError, np.linalg.LinAlgError):
        return None
    if not res.success or not np.all(np.isfinite(res.x)):
        return None

    a, b, rho, m, sigma = (float(v) for v in res.x)
    w_fit = svi_w(k, a, b, rho, m, sigma)
    rmse = float(np.sqrt(np.mean((w_fit - w) ** 2)))
    g_min = float(np.min(gatheral_g(k_grid, a, b, rho, m, sigma)))
    return {"a": a, "b": b, "rho": rho, "m": m, "sigma": sigma,
            "g_min": g_min, "rmse": rmse}


def atm_iv(params: dict, T: float) -> float | None:
    """Fitted at-the-money implied vol (``k = 0``)."""
    if not params or T <= 0:
        return None
    w0 = float(svi_w(0.0, params["a"], params["b"], params["rho"], params["m"], params["sigma"]))
    if w0 <= 0:
        return None
    return math.sqrt(w0 / T)


def atm_greeks(params: dict, T: float, r: float, forward: float,
               cp: str = "C") -> dict:
    """Black-76 Greeks at the forward (ATM, K = F), using the fitted ATM vol.

    Per unit of the forward ``F``: delta is dimensionless, gamma is per $ of F,
    vega is per vol point and theta is per calendar day. At K = F the call and
    put value are equal, ``V = disc * F * (N(d1) - N(d2))``, and the rate term
    of theta is ``r * V``.
    """
    iv = atm_iv(params, T)
    if iv is None or not forward or forward <= 0:
        return {}
    disc = math.exp(-r * T)
    sqrt_t = math.sqrt(T)
    d1 = 0.5 * iv * sqrt_t
    d2 = -0.5 * iv * sqrt_t
    value = disc * forward * (float(ndtr(d1)) - float(ndtr(d2)))
    delta = disc * float(ndtr(d1)) if cp == "C" else -disc * float(ndtr(-d1))
    theta = -disc * forward * _norm_pdf(d1) * iv / (2.0 * sqrt_t) + r * value
    gamma = disc * _norm_pdf(d1) / (forward * iv * sqrt_t)
    vega = disc * forward * _norm_pdf(d1) * sqrt_t
    return {"iv": iv, "delta": delta, "gamma": gamma,
            "vega": vega / 100.0, "theta": theta / 365.0}


def density_check(g_min: float, g_min_window: float, clipped_pct: float, min_var: float,
                  clip_flag_pct: float) -> dict:
    """Is the fitted density trustworthy over the whole density window? (#172 D2)

    ``flag`` is true when mass was clipped beyond ``clip_flag_pct`` (percent), g is negative anywhere in the
    window, or the SVI minimum variance is negative; ``note`` is the board label and ``g_min_shown`` the
    smaller of the fit-grid and window g-min.
    """
    g_shown = round(min(g_min, g_min_window), 5)
    if clipped_pct > clip_flag_pct:
        note = f"density clipped {clipped_pct:.2f}%"
    elif g_shown < 0:
        note = "negative density!"
    elif min_var < 0:
        note = "min variance < 0"
    else:
        return {"flag": False, "note": "butterfly ok", "g_min_shown": g_shown}
    return {"flag": True, "note": note, "g_min_shown": g_shown}


class ProbCurve:
    """Risk-neutral price distribution for one expiry, built from an SVI fit."""

    def __init__(self, expiry: str, T: float, forward: float, params: dict,
                 r: float = 0.0, min_half_width: float = 2.5,
                 sd_mult: float = 8.0, half_cap: float = 6.0,
                 grid_n: int = 801):
        self.expiry = expiry
        self.T = float(T)
        self.forward = float(forward)
        self.params = {kk: float(params[kk]) for kk in ("a", "b", "rho", "m", "sigma")}
        self.g_min = params.get("g_min")
        self.rmse = params.get("rmse")
        self.r = float(r)

        # Adaptive log-moneyness window: wide enough to hold the tails so the
        # density integrates to ~1 (a fixed +-1.5 grid truncates high-vol /
        # heavily-skewed smiles and biases the mean away from the forward).
        w0 = float(svi_w(0.0, **self.params))
        sd = math.sqrt(max(w0, 1e-9))
        half = min(max(min_half_width, sd_mult * sd), half_cap)
        k = np.linspace(-half, half, grid_n)
        p = rnd_log_density(k, **self.params)
        # Honest checks over the *density window* (the fit only checks g on
        # +-0.5): the negative mass the clip below throws away, g over the
        # whole window, and the SVI minimum-variance condition.
        self.clipped_mass = float(_trapz(np.clip(-p, 0.0, None), k))
        self.g_min_window = float(np.min(gatheral_g(k, **self.params)))
        self.min_var = (self.params["a"] + self.params["b"] * self.params["sigma"]
                        * math.sqrt(1.0 - self.params["rho"] ** 2))
        p = np.clip(p, 0.0, None)
        area = float(_trapz(p, k))
        if area > 0:
            p = p / area
        self.k = k
        self.log_pdf = p
        self.cdf = cumulative_trapezoid(p, k, initial=0.0)
        self.S = self.forward * np.exp(k)
        self.price_pdf = p / self.S  # density per unit price

    # ── queries ──
    def _k_of(self, price: float) -> float:
        return math.log(max(price, 1e-12) / self.forward)

    def pdf(self, price: float) -> float:
        return float(np.interp(self._k_of(price), self.k, self.price_pdf,
                               left=0.0, right=0.0))

    def cdf_at(self, price: float) -> float:
        return float(np.interp(self._k_of(price), self.k, self.cdf,
                               left=0.0, right=1.0))

    def prob_below(self, price: float) -> float:
        return self.cdf_at(price)

    def prob_above(self, price: float) -> float:
        return 1.0 - self.cdf_at(price)

    def prob_between(self, lo: float, hi: float) -> float:
        return max(0.0, self.cdf_at(hi) - self.cdf_at(lo))

    def quantile(self, q: float) -> float:
        q = min(max(q, 1e-6), 1.0 - 1e-6)
        return float(np.interp(q, self.cdf, self.S))

    def stats(self) -> dict:
        p = np.clip(self.price_pdf, 0.0, None)
        mean = float(_trapz(self.S * p, self.S)) if p.any() else self.forward
        return {
            "mean": mean,
            "median": self.quantile(0.50),
            "p05": self.quantile(0.05), "p10": self.quantile(0.10),
            "p25": self.quantile(0.25), "p75": self.quantile(0.75),
            "p90": self.quantile(0.90), "p95": self.quantile(0.95),
        }


class ProbSurface:
    """A time-ordered set of :class:`ProbCurve` (one per fitted expiry)."""

    def __init__(self, curves: list[ProbCurve]):
        self.curves = sorted(curves, key=lambda c: c.T)

    @property
    def expiries(self) -> list[str]:
        return [c.expiry for c in self.curves]

    def slice(self, expiry: str) -> ProbCurve | None:
        for c in self.curves:
            if c.expiry == expiry:
                return c
        return None

    def fan(self, qs=(0.05, 0.25, 0.5, 0.75, 0.95)) -> dict:
        """Quantile bands across expiries for the fan chart."""
        return {
            "expiries": self.expiries,
            "dte": [round(c.T * 365) for c in self.curves],
            "quantiles": {f"{q:.2f}": [c.quantile(q) for c in self.curves] for q in qs},
            "spot_ref": [c.forward for c in self.curves],
        }
