"""Local similarity over LLM business profiles.

A profile is turned into a field-weighted bag of tags; similarity is the cosine
of two bags, with the shared tags kept as the human-readable evidence. Each tag
is scaled by its inverse document frequency over the run's profiles (#395, review
RL-1), so a tag shared by most companies (``subscription``) weighs less than one
shared by a few (``euv lithography``), the same idea as the themes' TF-IDF.
"""
from __future__ import annotations

import math

from . import config


def idf(profiles: dict[str, dict]) -> dict[str, float]:
    """``{tag: idf}`` over ``profiles``: ``log((1 + n) / (1 + df)) + 1``, sklearn's smoothed form, so a tag every
    company carries still weighs 1.0 and a tag one company carries weighs ``log((1 + n) / 2) + 1``."""
    df: dict[str, int] = {}
    for profile in profiles.values():
        for term in set(term_weights(profile)):
            df[term] = df.get(term, 0) + 1
    n = len(profiles)
    return {t: math.log((1 + n) / (1 + d)) + 1.0 for t, d in df.items()}


def term_weights(profile: dict, idf: dict[str, float] | None = None) -> dict[str, float]:
    """Field weight per tag (a tag in two fields sums), times its ``idf`` when one is given."""
    weights: dict[str, float] = {}
    for field, values in (profile or {}).items():
        w = config.FIELD_WEIGHTS.get(field, 0.0)
        if not w:
            continue
        for v in (values or []):
            term = str(v).strip().lower()
            if term:
                weights[term] = weights.get(term, 0.0) + w
    if idf:
        weights = {t: w * idf.get(t, 1.0) for t, w in weights.items()}
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


def score_pair(profile_a: dict, profile_b: dict, idf: dict[str, float] | None = None) -> tuple[float, list[str]]:
    wa, wb = term_weights(profile_a, idf), term_weights(profile_b, idf)
    return cosine(wa, wb), shared_terms(wa, wb)


def all_scores(profiles: dict[str, dict]) -> dict[tuple[str, str], float]:
    """Pairwise cosine for every unordered pair (upper triangle), with the idf of ``profiles``."""
    tickers = sorted(profiles)
    weights = {t: term_weights(profiles[t], idf(profiles)) for t in tickers}
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


def peer_lists(profiles: dict[str, dict], top: int = 25, min_score: float = config.PEER_MIN_SCORE,
               min_shared: int = config.PEER_MIN_SHARED):
    """Vectorised top-``top`` peers per ticker → ``(peers, nonzero_scores)``.

    A sparse cosine matrix keeps a few thousand tickers to seconds instead of the
    O(n^2) Python loop. ``nonzero_scores`` is the **upper triangle** (i < j) of
    the pairwise scores with the zero pairs left out; the percentile is ranked
    over it. The weights carry the idf of ``profiles``. A peer under ``min_score``
    or with fewer than ``min_shared`` shared tags is left out of the list (#395,
    review RL-3: the tail of a top-25 was names sharing one weak tag); each kept
    peer carries ``n_shared``, the number of tags the two profiles share.
    """
    import numpy as np
    from scipy.sparse import csr_matrix, triu

    tickers = sorted(profiles)
    scale = idf(profiles)
    weights = {t: term_weights(profiles[t], scale) for t in tickers}
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
        out[t] = []
        for k in order:
            peer = tickers[int(idx[k])]
            n_shared = len(set(weights[t]) & set(weights[peer]))
            if vals[k] < min_score or n_shared < min_shared:
                continue
            out[t].append({"peer": peer, "score": round(float(vals[k]), 4), "score_raw": float(vals[k]),
                           "shared": shared_terms(weights[t], weights[peer]), "n_shared": n_shared})
    return out, scores


# --- Similarity v2 (#669, plan 2026-10-30 Step 2): embeddings + customer/supplier + structure -----------------

_SUFFIXES = {"inc", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "llc", "lp", "sa", "ag", "nv",
             "group", "holdings", "the"}
STRUCT_FIELDS = ("business_model", "channel", "stage", "sector")


def party_name(name) -> str:
    """A customer/supplier name compared across profiles: lower case, punctuation and company suffixes dropped."""
    words = "".join(c if c.isalnum() else " " for c in str(name or "").lower()).split()
    return " ".join(w for w in words if w not in _SUFFIXES)


def chain_sets(pj: dict | None) -> tuple[set[str], set[str]]:
    """``(types, names)`` of a v3 profile's customers (``c:``) and suppliers (``s:``)."""
    types, names = set(), set()
    for side, key in (("c", "customers"), ("s", "suppliers")):
        for p in (pj or {}).get(key) or []:
            if not isinstance(p, dict):
                continue
            t = str(p.get("type") or "").strip().lower()
            if t:
                types.add(f"{side}:{t}")
            n = party_name(p.get("name"))
            if n:
                names.add(n)
    return types, names


def _binary(sets: list[set[str]]):
    from scipy.sparse import csr_matrix
    vocab: dict[str, int] = {}
    rows, cols = [], []
    for i, s in enumerate(sets):
        for v in s:
            rows.append(i)
            cols.append(vocab.setdefault(v, len(vocab)))
    return csr_matrix(([1.0] * len(rows), (rows, cols)), shape=(len(sets), max(1, len(vocab))), dtype="float32")


def blended_peer_lists(profiles: dict[str, dict], pjs: dict[str, dict], vectors: dict, sectors: dict | None = None,
                       top: int = 25, chunk: int = 256):
    """Top-``top`` peers per ticker on ``W_DO s_do + W_CHAIN s_chain + W_STRUCT s_struct`` -> ``(peers, scores)``.

    ``s_do`` is the cosine of the company vectors (``embeddings.company_vectors``; a ticker without one scores 0 on
    it). ``s_chain`` = ``W_CHAIN_TYPES`` x Jaccard of the customer/supplier types + ``W_CHAIN_NAMES`` when the two
    name a customer or supplier in common. ``s_struct`` is the share of ``STRUCT_FIELDS`` both fill with the same
    value. ``scores`` is the upper triangle (i < j) as one float32 array, for the percentile. The pairs are scored in
    row chunks, so memory stays ``chunk x n``, never ``n x n``. Each peer keeps the shared tags as evidence.
    shortcut: names are matched as normalised text, not resolved to tickers through Value Chain's aliases, and the
    direct edge (A names B) is not scored; upgrade when Value Chain's resolver (#410) lands.
    """
    import numpy as np

    tickers = sorted(profiles)
    n = len(tickers)
    if n < 2:
        return {t: [] for t in tickers}, np.zeros(0, dtype="float32")
    dim = len(next(iter(vectors.values()))) if vectors else 1
    E = np.zeros((n, dim), dtype="float32")
    for i, t in enumerate(tickers):
        if t in vectors:
            E[i] = vectors[t]
    chains = [chain_sets(pjs.get(t)) for t in tickers]
    T, N = _binary([c[0] for c in chains]), _binary([c[1] for c in chains])
    t_size = np.asarray(T.sum(axis=1)).ravel()
    codes = []
    for f in STRUCT_FIELDS:
        vals = [str((sectors or {}).get(t) if f == "sector" else (pjs.get(t) or {}).get(f) or "").strip().lower()
                for t in tickers]
        index = {v: k for k, v in enumerate(sorted({v for v in vals if v}))}
        codes.append(np.asarray([index.get(v, -1) for v in vals]))
    scale = idf(profiles)
    weights = {t: term_weights(profiles[t], scale) for t in tickers}

    peers: dict[str, list] = {}
    upper = []
    for a in range(0, n, chunk):
        b = min(n, a + chunk)
        s_do = E[a:b] @ E.T
        inter = (T[a:b] @ T.T).toarray()
        union = t_size[a:b, None] + t_size[None, :] - inter
        jac = np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)
        named = ((N[a:b] @ N.T).toarray() > 0).astype("float32")
        s_struct = sum(((c[a:b, None] == c[None, :]) & (c[a:b, None] >= 0)).astype("float32") for c in codes)
        s = (config.W_DO * s_do + config.W_CHAIN * (config.W_CHAIN_TYPES * jac + config.W_CHAIN_NAMES * named)
             + config.W_STRUCT * s_struct / len(STRUCT_FIELDS)).astype("float32")
        for r in range(b - a):
            i = a + r
            upper.append(s[r, i + 1:])
            row = s[r].copy()
            row[i] = -np.inf
            k = min(top, n - 1)
            sel = np.argpartition(-row, k - 1)[:k]
            sel = sel[np.argsort(-row[sel])]
            focal = tickers[i]
            peers[focal] = []
            for j in sel:
                if row[j] <= 0:
                    continue
                peer = tickers[int(j)]
                peers[focal].append({"peer": peer, "score": round(float(row[j]), 4), "score_raw": float(row[j]),
                                     "shared": shared_terms(weights[focal], weights[peer]),
                                     "n_shared": len(set(weights[focal]) & set(weights[peer]))})
    return peers, np.concatenate(upper)
