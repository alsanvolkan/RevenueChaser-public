"""Hierarchical clustering of the return-correlation matrix.

Ward linkage (``config.LINKAGE``) on correlation distance. Three cut views, scaled to the
universe size (see ``config.cuts_for``) — never a single hidden ``k``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

from . import config
from . import returns as returns_mod


def clusters(ret: pd.DataFrame, cuts, method: str | None = None,
             corr: pd.DataFrame | None = None, min_periods: int | None = None):
    """Cluster ``ret`` at each cut.

    Returns ``(Z, labels, corr, groups)`` where ``groups`` maps
    ``cut -> {cluster_id: [ticker, ...]}``. Returns ``None`` when there are
    fewer than two tickers to cluster.
    """
    method = config.LINKAGE if method is None else method
    if ret is None or ret.shape[1] < 2:
        return None
    corr = returns_mod.correlation(ret, min_periods) if corr is None else corr
    labels = list(corr.columns)
    d = returns_mod.distance_matrix(corr)
    Z = linkage(squareform(d, checks=False), method=method)

    groups: dict[int, dict[int, list[str]]] = {}
    for k in cuts:
        ids = fcluster(Z, t=k, criterion="maxclust")
        per: dict[int, list[str]] = {}
        for ticker, cid in zip(labels, ids):
            per.setdefault(int(cid), []).append(ticker)
        groups[int(k)] = per
    return Z, labels, corr, groups


def mean_intra_corr(corr: pd.DataFrame, members: list[str]) -> float | None:
    """Mean pairwise correlation within a cluster (excluding the diagonal)."""
    if len(members) < 2:
        return None
    sub = corr.loc[members, members].to_numpy(dtype=float)
    iu = np.triu_indices_from(sub, k=1)
    vals = sub[iu]
    vals = vals[~np.isnan(vals)]
    return float(vals.mean()) if len(vals) else None
