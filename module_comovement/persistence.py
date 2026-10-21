"""Out-of-sample persistence for the Co-movement board (review 2026-10-01 §1).

The shuffled-returns null (``noise.selected_group_null``) asks whether a group
beats pure noise; on real data nearly every group does, because the residual
market has ~20 real factors. The harder question is whether a group is a
*lasting* one. This module answers it without look-ahead:

1. **Form** groups on a full window that ends ``OOS_HOLDOUT`` sessions ago
   (same returns, benchmark, winsorisation, linkage and cuts as the board).
2. **Test** each of those lagged groups on the held-out last quarter only: its
   mean pairwise r there, against random names of the same size in the same
   held-out matrix. Random names are a valid null *here* because the held-out
   data played no part in choosing the group.
3. **Match** each current group to its best lagged group by Jaccard overlap:
   ``>= PERSIST_MIN_JACCARD`` → the same group a quarter ago (held or not);
   below → new this quarter.

Steps 1–2 cost one extra clustering and are Fetch-only (persisted in
``comov_lag``); step 3 is cheap and runs on every render.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config
from . import noise
from . import returns as returns_mod
from . import settings as settings_mod

RANDOM_DRAWS = 200


def _residual(ret: pd.DataFrame, bench_ticker: str | None,
              apply: bool) -> pd.DataFrame:
    """The board's market removal: the benchmark column never joins a group;
    when the board applied it, one-factor OLS residuals, else raw returns."""
    if bench_ticker and bench_ticker in ret.columns:
        market = ret[bench_ticker]
        uni = ret.drop(columns=[bench_ticker])
        if apply and int(market.notna().sum()) >= 3 and uni.shape[1] >= 3:
            return returns_mod.residualize(uni, market)
        return uni
    return ret


def random_names_baseline(corr: pd.DataFrame, sizes, draws: int = RANDOM_DRAWS,
                          q: float = noise.NULL_Q, seed: int = 0) -> dict[int, float]:
    """``size -> q-quantile of mean pairwise r`` over random names of that size.

    Only valid on data that played no part in choosing the groups (the held-out
    quarter). Sampled once per ``noise.GRID`` point; a size reads the largest
    point <= it (stricter, as the quantile falls with size).
    """
    a = corr.to_numpy(dtype=float)
    a = np.where(np.isnan(a), 0.0, a)
    n_all = a.shape[0]
    if n_all < 2:
        return {}
    rng = np.random.default_rng(seed)
    quant: dict[int, float] = {}
    for k in sorted({g for g in noise.GRID if g <= min(noise.NULL_MAX_N, n_all)}):
        iu = np.triu_indices(k, 1)
        means = np.empty(draws, dtype=float)
        for i in range(draws):
            idx = rng.choice(n_all, size=k, replace=False)
            means[i] = a[np.ix_(idx, idx)][iu].mean()
        quant[k] = float(np.quantile(means, q))
    out: dict[int, float] = {}
    for size in sizes:
        g = noise.grid_point(min(int(size), n_all))
        if g is not None and g in quant:
            out[int(size)] = quant[g]
    return out


def lagged_test(prices: pd.DataFrame, cuts, horizon: int,
                bench_ticker: str | None, cluster_fn, apply_bench: bool = True,
                holdout: int | None = None, seed: int = 0,
                eff: "settings_mod.Effective | None" = None) -> dict[int, list[dict]]:
    """``cut -> [{lag_id, members, oos_r, oos_base, held}]`` for groups formed on
    the window ending ``holdout`` sessions ago and tested on the last ``holdout``.

    ``prices`` is the board's price frame (benchmark column included, flat
    printers already dropped); ``apply_bench`` mirrors whether the board
    residualized on it. ``held`` is ``None`` when fewer than two members
    have held-out returns. Empty when the history is too short for a lagged
    window of at least ``MIN_OBS`` returns.
    """
    eff = eff or settings_mod.effective()
    holdout = eff.oos_holdout if holdout is None else int(holdout)
    oos_min_obs = (eff.oos_min_obs if holdout == eff.oos_holdout
                   else settings_mod.oos_min_obs_for(holdout))
    horizon = max(1, int(horizon))
    if prices is None or prices.empty or len(prices) < holdout + eff.min_obs + horizon + 1:
        return {}
    lag_ret, _ = returns_mod.log_returns(prices.iloc[:-holdout], window=eff.window,
                                         min_obs=eff.min_obs, horizon=horizon)
    lag = returns_mod.winsorize_mad(_residual(lag_ret, bench_ticker, apply_bench),
                                    eff.winsor_k)
    if lag.shape[1] < 3:
        return {}
    res = cluster_fn(lag, tuple(int(k) for k in cuts))
    if res is None:
        return {}
    groups = res[3]

    test_ret, _ = returns_mod.log_returns(prices, window=holdout,
                                          min_obs=oos_min_obs, horizon=horizon)
    test = _residual(test_ret, bench_ticker, apply_bench)
    corr = test.corr(min_periods=oos_min_obs)
    a = np.where(np.isnan(corr.to_numpy(dtype=float)), 0.0, corr.to_numpy(dtype=float))
    pos = {t: i for i, t in enumerate(corr.columns)}
    present = {int(k): {cid: [t for t in mem if t in pos] for cid, mem in per.items()}
               for k, per in groups.items()}
    sizes = {len(m) for per in present.values() for m in per.values() if len(m) >= 2}
    base = random_names_baseline(corr, sizes, seed=seed)

    out: dict[int, list[dict]] = {}
    for k, per in groups.items():
        rows = []
        for cid, mem in per.items():
            idx = [pos[t] for t in present[int(k)][cid]]
            r = b = held = None
            if len(idx) >= 2:
                iu = np.triu_indices(len(idx), 1)
                r = float(a[np.ix_(idx, idx)][iu].mean())
                b = base.get(len(idx))
                held = None if b is None else bool(r > b)
            rows.append({"lag_id": str(cid), "members": list(mem),
                         "oos_r": r, "oos_base": b, "held": held})
        out[int(k)] = rows
    return out


def _best(members, lag_groups) -> tuple[dict | None, float]:
    s = set(members)
    best, best_j = None, 0.0
    for g in lag_groups or []:
        m = set(g["members"])
        inter = len(s & m)
        if not inter:
            continue
        j = inter / len(s | m)
        if j > best_j:
            best, best_j = g, j
    return best, best_j


def core_members(members: list[str], lag_groups: list[dict] | None) -> dict:
    """``ticker -> core`` for one current group: ``True`` when the name was in
    the matching group a quarter ago, ``False`` when it joined since.

    Every value is ``None`` when there is no lagged test or the group is new
    this quarter: with no earlier group to compare, no name is "fringe".
    """
    best, j = _best(members, lag_groups)
    if best is None or j < config.PERSIST_MIN_JACCARD:
        return {t: None for t in members}
    before = set(best["members"])
    return {t: t in before for t in members}


def match(members: list[str], lag_groups: list[dict] | None) -> dict | None:
    """The persistence verdict for one current group.

    ``None`` when there is no lagged test for its cut (short history, or a
    Refresh with no persisted Fetch). Otherwise ``{"jaccard", "new", "held",
    "oos_r", "oos_base"}`` for the best-overlapping lagged group; ``new`` when
    that overlap is below ``PERSIST_MIN_JACCARD`` (then ``held`` is ``None``: a
    different group's verdict says nothing about this one).
    """
    if not lag_groups:
        return None
    best, best_j = _best(members, lag_groups)
    if best is None or best_j < config.PERSIST_MIN_JACCARD:
        return {"jaccard": best_j, "new": True, "held": None,
                "oos_r": None, "oos_base": None}
    return {"jaccard": best_j, "new": False, "held": best.get("held"),
            "oos_r": best.get("oos_r"), "oos_base": best.get("oos_base")}


def summary(lag: dict[int, list[dict]]) -> dict[str, dict[str, int]]:
    """``cut -> {"held", "total"}`` over lagged groups that could be judged."""
    out: dict[str, dict[str, int]] = {}
    for k, rows in (lag or {}).items():
        judged = [r for r in rows if r.get("held") is not None]
        if judged:
            out[str(k)] = {"held": sum(1 for r in judged if r["held"]),
                           "total": len(judged)}
    return out
