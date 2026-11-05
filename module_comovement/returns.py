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


#: A pair's variance counts as no spread below this share of its mean square. ``sum(x^2)/n - mean^2``
#: loses its leading digits when the variance is tiny beside the mean -- a constant column leaves a
#: residue of about 1e-16 of the mean square, positive as often as not, and ``var > 0`` then lets a
#: meaningless r through (measured: a flat column against its neighbour gave -2.9e-09 where pandas
#: gives NaN). Log returns sit near zero, so their variance IS their mean square and this never
#: fires on real input; it is the degenerate columns it catches. The cost is that a column whose
#: mean dwarfs its standard deviation by more than ~1e6 would read as flat -- not returns.
_VAR_FLOOR = 1e-12

#: Columns per block of the correlation matrix. The temporaries of one block are (block x names),
#: not (names x names): at 5 117 names one float64 matrix is 0.20 GB and the all-at-once form keeps
#: about ten alive, which measured +0.88 GB of peak RSS on a box whose dashboard holds the DuckDB
#: writer. Blocked it is +0.00 GB **and** faster, because (512 x names) products stay in cache:
#: 3.14 s against 3.73 s all at once (homesrv, 5 Nov 2026). Same reasoning as ``_PAIR_CHUNK``.
_CORR_BLOCK = 512


def correlation(ret: pd.DataFrame, min_periods: int | None = None) -> pd.DataFrame:
    """Pairwise Pearson correlation of returns, pairwise-complete as ``DataFrame.corr`` is.

    Five matrix products per block of columns instead of ``ret.corr(min_periods)``, which walks the
    pairs one at a time: **18.56 s -> 3.14 s** over 5 117 names x 252 sessions on homesrv
    (5 Nov 2026, #459), and the whole Co-movement board was 94% CPU-bound on this and
    ``winsorize_mad``.

    Per pair the mean and the variance have to be taken on the rows where BOTH columns are finite,
    so a single per-column mean will not do. The counts and the four sums come out of products
    against the finite mask: with ``A`` the returns and NaN as 0, and ``M`` 1 where finite,
    ``n = M'M`` counts the jointly finite rows and ``A'M``, ``M'A``, ``A'A``, ``(A*A)'M``,
    ``M'(A*A)`` give the sums of x, y, xy, x^2 and y^2 over exactly those rows.

    It is not bit-identical to pandas -- a different summation order moves the last bits -- but it
    is the same answer: measured against ``ret.corr`` on the live frame, the NaN pattern is
    identical in all 26 183 451 cells, the largest difference is 2.4e-15, and no cell differs by
    more than 1e-12. Downstream that changes nothing anyone can see: the linkage's merge pairs and
    cluster sizes are identical, only the merge heights move (at most 7.8e-14), and the cluster
    membership is the same at every cut from 2 to 300 -- the board draws 20, 80 and 250.
    """
    min_periods = (settings_mod.effective().min_periods if min_periods is None
                   else min_periods)
    if ret is None or ret.empty:
        return pd.DataFrame()
    x = ret.to_numpy(dtype=np.float64, copy=False)
    finite = np.isfinite(x)
    a = np.where(finite, x, 0.0)
    mask = finite.astype(np.float64)
    squares = a * a
    cols = x.shape[1]
    out = np.empty((cols, cols), dtype=np.float64)
    for lo in range(0, cols, _CORR_BLOCK):
        hi = min(lo + _CORR_BLOCK, cols)
        a_t, mask_t, sq_t = a[:, lo:hi].T, mask[:, lo:hi].T, squares[:, lo:hi].T
        n = mask_t @ mask
        with np.errstate(invalid="ignore", divide="ignore"):
            mean_x = (a_t @ mask) / n
            mean_y = (mask_t @ a) / n
            scale_x = (sq_t @ mask) / n                  # mean of x^2 on the jointly finite rows
            scale_y = (mask_t @ squares) / n
            cov = (a_t @ a) / n - mean_x * mean_y
            var_x = scale_x - mean_x * mean_x
            var_y = scale_y - mean_y * mean_y
            block = cov / np.sqrt(var_x * var_y)
        # Fewer jointly finite rows than asked for, or no spread on either side, is not a number.
        block[(n < min_periods)
              | ~(var_x > _VAR_FLOOR * scale_x)
              | ~(var_y > _VAR_FLOOR * scale_y)] = np.nan
        out[lo:hi, :] = block
    # A correlation is in [-1, 1] and a name correlates with itself exactly, both of which the
    # arithmetic only gets to within rounding: measured on the live frame, 823 cells came out above
    # 1 (at most 1 + 4.4e-16) and 1 616 diagonals were not exactly 1.0. All 823 were diagonal, which
    # ``distance_matrix`` overwrites with 0, so no distance was wrong -- but ``sqrt(2(1-r))`` on
    # r > 1 is a NaN and 212 RuntimeWarnings, and an off-diagonal cell would land in ``linkage``,
    # which the docstring there says cannot take NaNs. Cheaper to be exact than to rely on that.
    np.clip(out, -1.0, 1.0, out=out)
    diagonal = np.diag_indices_from(out)
    # Except where the pair is not a number at all (all-NaN, or fewer rows than ``min_periods``),
    # which pandas leaves NaN on the diagonal too.
    out[diagonal] = np.where(np.isnan(out[diagonal]), np.nan, 1.0)
    return pd.DataFrame(out, index=ret.columns, columns=ret.columns)


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
    # One pass over the frame, not one per name: the medians, the MAD and the clip are all
    # column-wise, and the loop this replaced cost 36.7 s of a 97 s Co-movement run on homesrv
    # (5 Nov 2026, ~5 100 names) -- most of it pandas overhead paid 5 100 times over.
    med = ret.median()
    mad = (ret - med).abs().median()
    half = k * 1.4826 * mad
    # The loop's ``continue`` condition, kept term for term so the numbers are the same: a column
    # with no spread is left alone. Infinite bounds are how that is said here, because ``clip`` is a
    # no-op against them -- a bound of median +- 0 would clip away the one real move of a name that
    # printed flat and then gapped (MAD 0 without being constant), which is the case the mask is
    # for. The ``isfinite`` half is belt and braces: a NaN MAD gives a NaN bound, which pandas
    # already treats as no bound, so no test can fail on it -- it is here to mirror the loop.
    spread = np.isfinite(mad) & (mad != 0.0)
    return ret.clip(lower=(med - half).where(spread, -np.inf),
                    upper=(med + half).where(spread, np.inf), axis=1)


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
