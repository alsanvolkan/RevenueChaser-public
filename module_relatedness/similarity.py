"""Local similarity over LLM business profiles.

A profile is turned into a field-weighted bag of tags; similarity is the cosine
of two bags, with the shared tags kept as the human-readable evidence.
"""
from __future__ import annotations

import math

from . import config


def term_weights(profile: dict) -> dict[str, float]:
    weights: dict[str, float] = {}
    for field, values in (profile or {}).items():
        w = config.FIELD_WEIGHTS.get(field, 0.0)
        if not w:
            continue
        for v in (values or []):
            term = str(v).strip().lower()
            if term:
                weights[term] = weights.get(term, 0.0) + w
    return weights


def cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    shared = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in shared)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def shared_terms(a: dict, b: dict, top: int = 8) -> list[str]:
    shared = set(a) & set(b)
    return [t for t in sorted(shared, key=lambda t: -(a[t] + b[t]))[:top]]


def score_pair(profile_a: dict, profile_b: dict) -> tuple[float, list[str]]:
    wa, wb = term_weights(profile_a), term_weights(profile_b)
    return cosine(wa, wb), shared_terms(wa, wb)


def all_scores(profiles: dict[str, dict]) -> dict[tuple[str, str], float]:
    """Pairwise cosine for every unordered pair (upper triangle)."""
    tickers = sorted(profiles)
    weights = {t: term_weights(profiles[t]) for t in tickers}
    out: dict[tuple[str, str], float] = {}
    for i, a in enumerate(tickers):
        for b in tickers[i + 1:]:
            out[(a, b)] = cosine(weights[a], weights[b])
    return out


def percentile(score: float, scores: list[float]) -> float:
    """Share of pairs with a lower score (0-100)."""
    if not scores:
        return 0.0
    return round(100.0 * sum(1 for s in scores if s < score) / len(scores), 1)


def peer_lists(profiles: dict[str, dict], top: int = 25):
    """Vectorised top-``top`` peers per ticker → ``(peers, nonzero_scores)``.

    A sparse cosine matrix keeps a few thousand tickers to seconds instead of the
    O(n^2) Python loop. ``nonzero_scores`` is the **upper triangle** (i < j) of
    the pairwise scores with the zero pairs left out; a caller counts those
    analytically (``n*(n-1)/2 - len(nonzero_scores)``) for percentile ranking.
    """
    import numpy as np
    from scipy.sparse import csr_matrix, triu

    tickers = sorted(profiles)
    weights = {t: term_weights(profiles[t]) for t in tickers}
    vocab: dict[str, int] = {}
    rows, cols, data = [], [], []
    for i, t in enumerate(tickers):
        for term, w in weights[t].items():
            j = vocab.get(term)
            if j is None:
                j = vocab[term] = len(vocab)
            rows.append(i)
            cols.append(j)
            data.append(w)
    if not rows:
        return {t: [] for t in tickers}, []

    M = csr_matrix((data, (rows, cols)), shape=(len(tickers), len(vocab)),
                   dtype="float64")
    inv = 1.0 / np.maximum(np.sqrt(M.multiply(M).sum(axis=1)).A1, 1e-12)
    M = M.multiply(inv[:, None]).tocsr()
    S = (M @ M.T).tocsr()
    # Only the upper triangle, no self-pairs, no zero-score pairs: the caller
    # adds the zeros back analytically.
    scores = [float(x) for x in triu(S, k=1).tocsr().data if x > 0]

    out: dict[str, list] = {}
    for i, t in enumerate(tickers):
        row = S.getrow(i)
        idx, vals = row.indices, row.data
        keep = idx != i
        idx, vals = idx[keep], vals[keep]
        if len(vals) > top:
            sel = np.argpartition(-vals, top - 1)[:top]
            idx, vals = idx[sel], vals[sel]
        order = np.argsort(-vals)
        out[t] = [
            {"peer": tickers[int(idx[k])], "score": round(float(vals[k]), 4),
             "score_raw": float(vals[k]),
             "shared": shared_terms(weights[t], weights[tickers[int(idx[k])]])}
            for k in order
        ]
    return out, scores
