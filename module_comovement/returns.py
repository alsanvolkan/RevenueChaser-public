"""Return matrix + correlation for the Co-movement board.

Membership comes from **daily log returns**, never price levels: two names that
trend together at different price scales still move together.
"""
from __future__ import annotations

from itertools import groupby

import numpy as np
import pandas as pd

from . import settings as settings_mod


def log_returns(prices: pd.DataFrame, window: int | None = None,
                min_obs: int | None = None,
                horizon: int = 1) -> tuple[pd.DataFrame, list[str]]:
    """Overlapping ``log(P_t / P_{t-horizon})`` returns over the last ``window``.

    ``horizon=1`` is the daily log return; ``horizon=5`` compares each close to
    the close five sessions earlier (not 5-day Yahoo bars). Drops tickers with
    fewer than ``min_obs`` non-null returns (short IPO / listing history).
    Returns ``(returns, dropped_tickers)``.
    """
    if window is None or min_obs is None:
        eff = settings_mod.effective(window)
        window = eff.window if window is None else window
        min_obs = eff.min_obs if min_obs is None else min_obs
    horizon = max(1, int(horizon))
    if prices is None or prices.empty:
        return pd.DataFrame(), []

    p = prices.tail(window + horizon)
    ret = np.log(p / p.shift(horizon)).replace([np.inf, -np.inf], np.nan)
    if len(ret) > horizon:
        ret = ret.iloc[horizon:]
    counts = ret.notna().sum()
    keep = [c for c in ret.columns if counts.get(c, 0) >= min_obs]
    dropped = [c for c in ret.columns if c not in keep]
    return ret[keep], dropped


def residualize(ret: pd.DataFrame, market: pd.Series) -> pd.DataFrame:
    """Remove the market factor from each column by one-factor OLS.

    Per-column ``r_i = a + b * r_m + e_i`` with an intercept, fitted on the
    overlapping non-null rows. Returns the residuals ``e_i`` (same shape as
    ``ret``); a column with too few overlapping rows becomes all-NaN.
    """
    if ret is None or ret.empty:
        return pd.DataFrame() if ret is None else ret.copy()
    m = market.reindex(ret.index) if isinstance(market, pd.Series) else \
        pd.Series(market, index=ret.index)
    mvals = m.to_numpy(dtype=float)
    # Collect the columns and build the frame once: inserting them one by one
    # fragments the frame (pandas PerformanceWarning, #93) and slows the run.
    data: dict = {}
    for col in ret.columns:
        y = ret[col].to_numpy(dtype=float)
        mask = np.isfinite(y) & np.isfinite(mvals)
        if int(mask.sum()) < 3:
            data[col] = np.full(len(ret.index), np.nan)
            continue
        x = np.column_stack([np.ones(int(mask.sum())), mvals[mask]])
        beta, *_ = np.linalg.lstsq(x, y[mask], rcond=None)
        resid = y - (beta[0] + beta[1] * mvals)
        resid[~mask] = np.nan
        data[col] = resid
    return pd.DataFrame(data, index=ret.index)


def correlation(ret: pd.DataFrame, min_periods: int | None = None) -> pd.DataFrame:
    """Pairwise Pearson correlation of returns."""
    min_periods = (settings_mod.effective().min_periods if min_periods is None
                   else min_periods)
    if ret is None or ret.empty:
        return pd.DataFrame()
    return ret.corr(min_periods=min_periods)


# Pairs per vectorised block. Each (rows x pairs) float temporary is ~8 MB at ~500
# rows; about eight are alive at once, so the peak stays near 65 MB.
_PAIR_CHUNK = 2048


def pair_correlation(ret: pd.DataFrame, pairs: list[tuple[str, str]],
                     min_periods: int | None = None) -> np.ndarray:
    """Pearson r for the given ``(a, b)`` column pairs, never the full matrix.

    Same rule as ``ret.corr(min_periods)`` looked up per pair: computed on the
    rows where both columns are finite. The result is aligned with ``pairs`` and
    is NaN when the value is unavailable: a column is missing, fewer than
    ``min_periods`` jointly-finite rows, either side has zero variance on those
    rows, or the result is not finite. Cost is O(rows x pairs), against
    O(rows x columns^2) for the matrix.
    """
    min_periods = (settings_mod.effective().min_periods if min_periods is None
                   else min_periods)
    out = np.full(len(pairs), np.nan)
    if ret is None or ret.empty or not pairs:
        return out
    cols = {c: i for i, c in enumerate(ret.columns)}
    R = ret.to_numpy(dtype=float)
    pos, fi, pj = [], [], []
    for k, (a, b) in enumerate(pairs):
        i, j = cols.get(a), cols.get(b)
        if i is None or j is None:
            continue
        pos.append(k)
        fi.append(i)
        pj.append(j)
    pos = np.asarray(pos, dtype=int)
    fi = np.asarray(fi, dtype=int)
    pj = np.asarray(pj, dtype=int)
    mp = max(int(min_periods), 1)
    with np.errstate(all="ignore"):
        for s in range(0, len(fi), _PAIR_CHUNK):
            X = R[:, fi[s:s + _PAIR_CHUNK]]
            Y = R[:, pj[s:s + _PAIR_CHUNK]]
            M = np.isfinite(X) & np.isfinite(Y)
            n = M.sum(axis=0)
            nn = np.maximum(n, 1)
            mx = np.where(M, X, 0.0).sum(axis=0) / nn
            my = np.where(M, Y, 0.0).sum(axis=0) / nn
            dx = np.where(M, X - mx, 0.0)
            dy = np.where(M, Y - my, 0.0)
            sxy = (dx * dy).sum(axis=0)
            sxx = (dx * dx).sum(axis=0)
            syy = (dy * dy).sum(axis=0)
            den = np.sqrt(sxx * syy)
            r = sxy / den
            bad = (n < mp) | (den == 0) | ~np.isfinite(r)
            r[bad] = np.nan
            out[pos[s:s + _PAIR_CHUNK]] = r
    return out


def distance_matrix(corr: pd.DataFrame) -> np.ndarray:
    """Correlation distance ``sqrt(2(1-r))`` clipped to ``[0, 2]``.

    ``linkage`` cannot accept NaNs, and an unmeasured pair is **r = 0** — the
    honest value, so the distance is ``sqrt(2)`` (review F9: mapping NaN to
    distance 1.0 claimed "half correlated" about a pair never measured).
    """
    a = np.where(np.isnan(corr.to_numpy(dtype=float)), 0.0, corr.to_numpy(dtype=float))
    d = np.sqrt(2.0 * (1.0 - a))
    d = np.clip(d, 0.0, 2.0)
    np.fill_diagonal(d, 0.0)
    return d


def winsorize_mad(ret: pd.DataFrame, k: float | None = None) -> pd.DataFrame:
    """Clip each column to ``median ± k · 1.4826 · MAD``.

    Pearson on log returns is dominated by a handful of days — earnings gaps,
    halts, SPAC prints — so one +80% print can make a name look like it never
    moved with its own group (review §4b). The MAD is the robust spread, and
    1.4826 scales it to a standard deviation for normal data. A column whose MAD
    is 0 (flat, or one value) has no spread to clip against and is left alone.
    """
    k = settings_mod.effective().winsor_k if k is None else float(k)
    if ret is None or getattr(ret, "empty", True):
        return ret if ret is not None else pd.DataFrame()
    out = ret.copy()
    for col in out.columns:
        x = out[col]
        med = x.median()
        mad = (x - med).abs().median()
        if not np.isfinite(mad) or float(mad) == 0.0:
            continue
        half = k * 1.4826 * float(mad)
        out[col] = x.clip(med - half, med + half)
    return out


def flat_print_names(prices: pd.DataFrame, run: int = 5,
                     max_share: float = 0.02) -> list[str]:
    """Names whose close is unchanged for ``run`` sessions or more, over
    ``max_share`` of the window.

    A halted or never-traded name has a constant close, so it correlates with
    nothing real but still lands in the hierarchy. The window is the one
    actually used for the run, and only an unbroken stretch counts: many
    two-session repeats are a thin name, not a halt.
    """
    out: list[str] = []
    if prices is None or getattr(prices, "empty", True):
        return out
    run = max(2, int(run))
    for col in prices.columns:
        s = prices[col].dropna()
        if len(s) < run + 1:
            continue
        v = s.to_numpy(dtype=float)
        eq = v[1:] == v[:-1]
        flat = 0
        for unchanged, grp in groupby(eq):
            if unchanged:
                n = sum(1 for _ in grp)
                if n >= run:
                    flat += n
        if eq.size and flat / eq.size > max_share:
            out.append(col)
    return out
