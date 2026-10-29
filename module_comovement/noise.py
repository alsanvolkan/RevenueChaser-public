"""Noise controls for the Co-movement board.

A group's mean intra correlation only means something against what the same
pipeline finds when there is nothing to find. Two tools (whether a group
*lasts* is ``persistence.py``):

* ``selected_group_null`` — the whole-pipeline permutation null: shuffle each
  column in time (pure noise, same marginals and gaps), run the same
  correlation → linkage → cuts, and record how tight the groups it *selects*
  look. Comparing a real group with random *unselected* names (the old test)
  let 100% of pure-noise groups pass (review 2026-10-01 §1, Gao–Bien–Witten);
* ``factors_above_noise`` — how many eigenvalues of the correlation matrix sit
  above the Marchenko–Pastur noise edge, i.e. how many real common moves the
  window holds (replaces the participation-ratio "independent bets", whose
  reading was inverted: pure noise scored highest).

Both take the board's ``horizon``. Overlapping h-session returns are serially
correlated, so a pair's sample r spreads wider than at 1 day: shuffling them row
by row made the null 1-day tight (on pure noise every 10-day group passed), and
the edge read 250 overlapping rows as 250 independent ones (Fable review of
28 Oct 2026, S1). At h > 1 the null shuffles the daily returns and sums them
over h sessions like the board's own, and the factor count reads every h-th row.

The block-bootstrap "stable %" (S9) was removed on 2026-09-30: resampling
dates inside one window measures sampling noise, not persistence, and cost ~20
clusterings per Fetch. Out-of-sample persistence replaced it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import returns as returns_mod

NULL_PERMS = 5     # shuffled re-clusterings per Fetch (~9 s each at N≈3,500)
NULL_Q = 0.99
NULL_MAX_N = 200   # beyond this the null is ~flat; read the 200 point

# Group sizes the null is summarised at. Every null group is filed under the
# largest grid point <= its size, and a real group reads the same point. A
# group smaller than any populated point reads the strictest populated one:
# the q-quantile of a selected group's mean r falls as the group grows, so
# borrowing a larger point would be lax.
GRID: tuple[int, ...] = (2, 3, 4, 5, 6, 8, 10, 13, 16, 20, 25, 32, 40, 50,
                         64, 80, 100, 128, 160, 200)


def grid_point(size: int) -> int | None:
    """Largest ``GRID`` point <= ``min(size, NULL_MAX_N)``.

    ``None`` for a size that cannot form a pair; the caller stores no baseline,
    and the board then treats the group as not-judged rather than as significant.
    """
    cap = min(int(size), NULL_MAX_N)
    best = None
    for k in GRID:
        if k <= cap:
            best = k
    return best


def _shuffle_columns(ret: pd.DataFrame, rng) -> pd.DataFrame:
    """Each column permuted in time on its own: marginals and gap counts kept,
    every cross-sectional link destroyed."""
    a = ret.to_numpy(dtype=float, copy=True)
    a = rng.permuted(a, axis=0)
    return pd.DataFrame(a, columns=ret.columns)


def _summed_daily_shuffle(daily: np.ndarray, horizon: int, gaps: np.ndarray,
                          rng) -> np.ndarray:
    """Pure noise with the overlap of ``horizon``-session returns: each column's
    daily returns permuted in time, then summed over ``horizon`` overlapping
    sessions as ``returns.log_returns`` does. A missing daily return counts as no
    move; the real matrix's own gaps (``gaps``) are kept, as the 1-day shuffle
    keeps them."""
    a = rng.permuted(daily, axis=0)
    a = np.where(np.isfinite(a), a, 0.0)
    c = np.vstack([np.zeros((1, a.shape[1])), np.cumsum(a, axis=0)])
    out = c[horizon:] - c[:-horizon]
    out[gaps] = np.nan
    return out


def selected_group_null(ret: pd.DataFrame, cuts, cluster_fn,
                        n_perm: int = NULL_PERMS, q: float = NULL_Q,
                        seed: int = 0, *, horizon: int = 1,
                        daily: pd.DataFrame | None = None,
                        winsor_k: float | None = None) -> dict[int, float]:
    """``grid point -> q-quantile of mean r`` of the groups the pipeline selects
    in column-shuffled (pure-noise) returns.

    ``cluster_fn(ret, cuts)`` must return ``cluster.clusters``' tuple — the same
    correlation, linkage and cuts as the real board, so the null carries the
    selection the real groups went through. A fixed ``seed`` makes two runs of
    the same matrix report the same null. Empty when nothing can be clustered.

    At ``horizon`` > 1 the noise is ``daily`` (the daily log returns under
    ``ret``'s columns, ``horizon - 1`` rows more than ``ret``) shuffled and summed
    over ``horizon`` sessions, then winsorised at ``winsor_k`` like the board's
    matrix: it then spreads as wide as overlapping returns do. Without such a
    ``daily`` the null is empty, so the groups are not judged.
    """
    if ret is None or getattr(ret, "empty", True) or ret.shape[1] < 3:
        return {}
    horizon = max(1, int(horizon))
    if horizon > 1:
        if daily is None or daily.shape != (len(ret) + horizon - 1, ret.shape[1]):
            return {}
        days = daily.to_numpy(dtype=float)
        gaps = ret.isna().to_numpy()
    rng = np.random.default_rng(seed)
    cuts = tuple(int(k) for k in cuts)
    samples: dict[int, list[float]] = {}
    for _ in range(int(n_perm)):
        if horizon > 1:
            shuffled = pd.DataFrame(_summed_daily_shuffle(days, horizon, gaps, rng),
                                    index=ret.index, columns=ret.columns)
            if winsor_k is not None:
                shuffled = returns_mod.winsorize_mad(shuffled, winsor_k)
        else:
            shuffled = _shuffle_columns(ret, rng)
        res = cluster_fn(shuffled, cuts)
        if res is None:
            continue
        corr = res[2]
        # An unmeasured pair (NaN) is skipped, as ``cluster.mean_intra_corr`` skips it for a real group (N1).
        a = corr.to_numpy(dtype=float)
        pos = {t: i for i, t in enumerate(corr.columns)}
        for per in res[3].values():
            for mem in per.values():
                g = grid_point(len(mem))
                if g is None:
                    continue
                idx = [pos[t] for t in mem]
                iu = np.triu_indices(len(idx), 1)
                vals = a[np.ix_(idx, idx)][iu]
                vals = vals[~np.isnan(vals)]
                if vals.size:
                    samples.setdefault(g, []).append(float(vals.mean()))
    return {g: float(np.quantile(v, q)) for g, v in sorted(samples.items())}


def baseline_for(null: dict, size: int) -> float | None:
    """The null bar a group of ``size`` must beat (see ``GRID``).

    ``None`` for a one-name group or an empty null: the group is then not
    judged, which the board shows rather than hides.
    """
    if not null or size is None or int(size) < 2:
        return None
    pts = {int(k): float(v) for k, v in null.items()}
    g = grid_point(int(size))
    below = [k for k in pts if g is not None and k <= g]
    if below:
        return pts[max(below)]
    return max(pts.values())


def factors_above_noise(ret: pd.DataFrame, horizon: int = 1) -> int | None:
    """Eigenvalues of the correlation matrix above the Marchenko–Pastur edge
    ``(1 + sqrt(N/T))²`` — the count of real common moves in the window.

    Complete-case rows over columns with >= 95% coverage (one recent listing
    must not cut the matrix to a handful of rows). Uses the T×T Gram matrix
    (same non-zero eigenvalues as N×N), cheap at N≈3,500, T≈252. ``None`` when
    fewer than 3 rows or columns survive.

    At ``horizon`` > 1 it reads every ``horizon``-th row back from the newest:
    those returns do not overlap, so the edge holds for them (fewer rows, so a
    weak move no longer counts; at 1 day nothing changes).
    """
    if ret is None or getattr(ret, "empty", True):
        return None
    x = ret.loc[:, ret.notna().mean() >= 0.95]
    if int(horizon) > 1:
        x = x.iloc[::-1].iloc[::int(horizon)].iloc[::-1]
    x = x.dropna(axis=0, how="any")
    if x.shape[0] < 3 or x.shape[1] < 3:
        return None
    z = (x - x.mean()) / x.std(ddof=0).replace(0, np.nan)
    z = z.dropna(axis=1)
    if z.shape[0] < 3 or z.shape[1] < 3:
        return None
    zz = z.to_numpy(dtype=float)
    t, n = zz.shape
    lam = np.linalg.eigvalsh(zz @ zz.T / t)
    edge = (1.0 + np.sqrt(n / t)) ** 2
    return int((lam > edge).sum())
