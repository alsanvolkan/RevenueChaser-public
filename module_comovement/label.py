"""Label a return cluster from enrichment against the cut, not a bare majority.

``label_level`` names a cluster only when an industry (else an NMF theme) is
enriched versus the names clustered at the same cut. The text carries the share
and the lift (``Regional Banks 83%, ×17``). Nothing that passes stays ``None``
(the board shows ``Cluster N``).

``label_cluster`` is the legacy majority rule, kept for research scripts.
"""
from __future__ import annotations

from collections import Counter

import numpy as np
from scipy.stats import hypergeom

from RevenueChaser.core.gics_map import is_unclassified

from . import config


def _kept(value) -> bool:
    # Same skip as ``_majority``: blank, and unclassified ("Unclassified" / legacy "Other").
    return bool(value) and not is_unclassified(value)


def _tags(values) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        if _kept(value) and value not in out:
            out.append(value)
    return tuple(out)


def _family_tails(xs: list[int], N: int, Ks: list[int], ns: list[int]) -> list[float]:
    """P(X >= x) for every test in one family, one vectorized hypergeometric call."""
    if not xs:
        return []
    tails = hypergeom.sf(
        np.asarray(xs, dtype=float) - 1,
        N,
        np.asarray(Ks, dtype=float),
        np.asarray(ns, dtype=float),
    )
    return [float(p) for p in np.atleast_1d(tails)]


def _bh(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values, in the same order as ``pvals``."""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: (pvals[i], i))
    adj = [1.0] * m
    running = 1.0
    for rank_from_end, i in enumerate(reversed(order)):
        rank = m - rank_from_end
        running = min(running, pvals[i] * m / rank)
        adj[i] = running
    return adj


def _format_label(name: str, share: float, lift: float) -> str:
    pct = round(share * 100)
    lift_txt = f"{round(lift):.0f}" if lift >= 10 else f"{lift:.1f}"
    return f"{name} {pct}%, ×{lift_txt}"


def _label_by(clusters: dict, tags_of, background: list[str]) -> dict:
    N = len(background)
    bg: Counter = Counter()
    for ticker in background:
        bg.update(tags_of(ticker))
    tests = []
    xs: list[int] = []
    Ks: list[int] = []
    ns: list[int] = []
    for cid, members in clusters.items():
        n = len(members)
        if n == 0 or N == 0:
            continue
        counts: Counter = Counter()
        for ticker in members:
            counts.update(tags_of(ticker))
        for name, x in counts.items():
            K = bg[name]
            if K <= 0 or n > N or K > N or x > min(n, K):
                continue
            share = x / n
            lift = share / (K / N)
            tests.append((cid, name, x, n, share, lift))
            xs.append(x)
            Ks.append(K)
            ns.append(n)
    tests = [(*row, p) for row, p in zip(tests, _family_tails(xs, N, Ks, ns))]
    qs = _bh([row[6] for row in tests])
    passing: dict = {cid: [] for cid in clusters}
    for (cid, name, x, n, share, lift, _p), q in zip(tests, qs):
        if (q < config.LABEL_Q_MAX and n >= config.LABEL_MIN_N
                and x >= config.LABEL_MIN_HITS
                and share >= config.LABEL_MIN_ENRICH_SHARE
                and lift >= config.LABEL_MIN_LIFT):
            passing[cid].append((lift, share, name))
    out: dict = {}
    for cid in clusters:
        cands = passing[cid]
        if not cands:
            out[cid] = None
            continue
        cands.sort(key=lambda row: (-row[0], -row[1], row[2]))
        lift, share, name = cands[0]
        out[cid] = _format_label(name, share, lift)
    return out


def label_level(clusters: dict[int, list[str]], industry_by_ticker: dict[str, str],
                themes_by_ticker: dict[str, list[str]] | None,
                background: list[str]) -> dict[int, str | None]:
    """One cut: industry enrichment, else theme enrichment, else ``None``."""
    industry_by = industry_by_ticker or {}
    themes_by = themes_by_ticker or {}

    def industry_tags(ticker: str) -> tuple[str, ...]:
        return _tags((industry_by.get(ticker),))

    def theme_tags(ticker: str) -> tuple[str, ...]:
        return _tags(themes_by.get(ticker) or ())

    industry = _label_by(clusters, industry_tags, background)
    themes = _label_by(clusters, theme_tags, background)
    return {cid: industry[cid] or themes[cid] for cid in clusters}


def _majority(counter: Counter, n: int, min_share: float) -> str | None:
    # Never name a group after "no value": blank, the unknown bucket ("Unclassified", and the legacy
    # "Other" on rows the identity master has not reached) and the theme catch-all ("Other") alike.
    for key in [k for k in counter if not k or is_unclassified(k)]:
        del counter[key]
    if not counter:
        return None
    value, count = counter.most_common(1)[0]
    # Strictly more than ``min_share`` — a 50/50 tie is not a shared business.
    return value if count / n > min_share else None


def label_cluster(members: list[str], industry_by_ticker: dict[str, str],
                  themes_by_ticker: dict[str, list[str]] | None = None,
                  min_share: float = config.LABEL_MIN_SHARE,
                  min_n: int = config.LABEL_MIN_N) -> str | None:
    """Legacy majority label, kept for research scripts. Prefer :func:`label_level`.

    A shared industry / theme, or ``None`` for a neutral cluster.
    """
    n = len(members)
    if n < min_n:
        return None

    industry = _majority(Counter(industry_by_ticker.get(t) for t in members), n, min_share)
    if industry:
        return industry

    if themes_by_ticker:
        themes: Counter = Counter()
        for t in members:
            for th in (themes_by_ticker.get(t) or []):
                themes[th] += 1
        return _majority(themes, n, min_share)
    return None
