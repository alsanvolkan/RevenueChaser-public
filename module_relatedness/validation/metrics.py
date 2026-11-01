"""The five Step 0 validation metrics (Relatedness v2, #341). Pure functions: no store, no network.

Definitions are the Fable method review's (``2026-10-30_1410_fable-method-review.md`` §4.5), quoted in the plan:

1. **Peer recall@25** — the share of a set's members found in each member's top-25 peers.
2. **Precision@10** on the hand sets.
3. **Theme purity** against the GICS *industry* (not sector): the share of a fine theme's members in its modal
   industry, weighted by size. Target >= 0.70; a theme below 0.4 is listed by name. Reported, never a gate.
4. **ETF theme recall**: for each narrow ETF, the share of its holdings in one fine or one medium theme.
   Target: median >= 0.6 at medium.
5. **Stability**: the ARI across seeds, reported.

The pair check sheet (the review's metric 4) lives in ``pair_sheet``. A cutover needs metrics 1 to 3 above their
targets and the sheet at >= 80% agreement; the gate itself is a later step's job, this module only measures.

**A risk to keep in mind when reading precision@10:** the hand sets began from a model's judgement (Claude proposed
them, avo corrected them), and the v3 profiles also come from a model, so the two can share blind spots and
hand-set precision@10 can look better than it is. The ETF sets and the pair sheet are the independent checks.

Scores are a dense ``n x n`` matrix over ``tickers`` (row = the name, column = its candidate peer). ``NaN`` is "no
score" and is never a peer; the diagonal is never a peer either. Ties rank by ticker, so a ranking is deterministic.
"""
from __future__ import annotations

from collections import Counter
from itertools import combinations
from statistics import median
from typing import Iterable, Mapping, Sequence

import numpy as np

RECALL_K = 25
PRECISION_K = 10
PURITY_TARGET = 0.70
PURITY_FLOOR = 0.4
ETF_RECALL_TARGET = 0.6
PAIR_AGREEMENT_TARGET = 0.80

# The five nullable theme_runs columns this package fills (core/rawstore.py MANIFEST, avo 1 Nov 2026).
THEME_RUNS_COLUMNS = ("val_peer_recall_25", "val_hand_precision_10", "val_gics_purity",
                      "val_etf_recall_medium", "val_seed_ari")


def _index(tickers: Sequence[str]) -> dict[str, int]:
    return {t: i for i, t in enumerate(tickers)}


def top_k(tickers: Sequence[str], scores: np.ndarray, ticker: str, k: int,
          _idx: Mapping[str, int] | None = None) -> list[str]:
    """The ``k`` best-scored peers of ``ticker``: highest score first, ties by ticker; self and NaN excluded."""
    idx = _idx or _index(tickers)
    i = idx[ticker]
    row = scores[i]
    cand = [(-float(row[j]), tickers[j]) for j in range(len(tickers)) if j != i and not np.isnan(row[j])]
    cand.sort()
    return [t for _, t in cand[:k]]


def _member_hits(tickers, scores, members: Iterable[str], k: int):
    """``(member, hits in its top k, other scored members)`` for each member present in the matrix."""
    idx = _index(tickers)
    present = [m for m in dict.fromkeys(members) if m in idx]
    out = []
    for m in present:
        others = set(present) - {m}
        if not others:
            continue
        top = set(top_k(tickers, scores, m, k, idx))
        out.append((m, len(top & others), len(others)))
    return out


def _summary(per_set: dict[str, float]) -> dict:
    vals = list(per_set.values())
    return {"mean": float(np.mean(vals)) if vals else None,
            "median": float(median(vals)) if vals else None,
            "n_sets": len(vals)}


def peer_recall_at_k(tickers: Sequence[str], scores: np.ndarray, sets, k: int = RECALL_K) -> dict:
    """Per set: the mean over its members of ``|top-k ∩ other members| / |other members|``.

    Members missing from the matrix are left out (the loader has already dropped and reported names outside the
    universe). A set with fewer than two scored members has no value.
    """
    per_set = {}
    for s in sets:
        hits = _member_hits(tickers, scores, s.tickers, k)
        if hits:
            per_set[s.name] = float(np.mean([h / n for _, h, n in hits]))
    return {"k": k, "per_set": per_set, **_summary(per_set)}


def precision_at_k(tickers: Sequence[str], scores: np.ndarray, sets, k: int = PRECISION_K) -> dict:
    """Per set: the mean over its members of ``|top-k ∩ other members| / k``.

    The denominator is ``k`` (standard precision@k), so a set smaller than ``k + 1`` can never reach 1.0; its
    ``ceiling`` (``min(k, other members) / k``) is reported beside it so a 5-name set is read against 0.4.
    """
    per_set, ceiling = {}, {}
    for s in sets:
        hits = _member_hits(tickers, scores, s.tickers, k)
        if hits:
            per_set[s.name] = float(np.mean([h / k for _, h, _ in hits]))
            ceiling[s.name] = float(np.mean([min(k, n) / k for _, _, n in hits]))
    return {"k": k, "per_set": per_set, "ceiling": ceiling, **_summary(per_set)}


def _members(theme) -> list[str]:
    return list(theme["members"]) if isinstance(theme, Mapping) else list(theme)


def _label(tid: str, theme) -> str:
    return (theme.get("label") or tid) if isinstance(theme, Mapping) else tid


def theme_purity(themes: Mapping[str, object], industry_by: Mapping[str, str],
                 floor: float = PURITY_FLOOR, target: float = PURITY_TARGET) -> dict:
    """Size-weighted share of fine-theme members in their theme's modal GICS industry.

    ``themes`` maps theme id to ``{"label", "members"}`` (or a bare member list). A member with no industry is left
    out of both the count and the size. Themes below ``floor`` are listed by label. Report only, never a gate.
    """
    per_theme, below, num, den = {}, [], 0, 0
    for tid, theme in themes.items():
        inds = [industry_by[m] for m in _members(theme) if industry_by.get(m)]
        if not inds:
            continue
        modal = Counter(inds).most_common(1)[0][1]
        p = modal / len(inds)
        per_theme[tid] = p
        num += modal
        den += len(inds)
        if p < floor:
            below.append(_label(tid, theme))
    purity = num / den if den else None
    return {"purity": purity, "per_theme": per_theme, "below_floor": sorted(below), "target": target,
            "floor": floor, "passes": None if purity is None else purity >= target}


def etf_theme_recall(etf_sets, themes: Mapping[str, object], target: float = ETF_RECALL_TARGET) -> dict:
    """For each ETF set, the largest share of its (loaded) holdings that falls in one theme of ``themes``.

    Pass the medium themes for the gated number (target: median >= 0.6), or the fine themes for the report.
    """
    sets_by_theme = {tid: set(_members(t)) for tid, t in themes.items()}
    per_etf = {}
    for s in etf_sets:
        h = set(s.tickers)
        if not h:
            continue
        best_tid, best = None, 0
        for tid in sorted(sets_by_theme):
            n = len(h & sets_by_theme[tid])
            if n > best:
                best_tid, best = tid, n
        per_etf[s.name] = {"recall": best / len(h), "theme": best_tid, "holdings": len(h)}
    med = float(median(v["recall"] for v in per_etf.values())) if per_etf else None
    return {"per_etf": per_etf, "median": med, "target": target,
            "passes": None if med is None else med >= target}


def seed_ari(assignments: Sequence[Mapping[str, object]]) -> float:
    """Mean pairwise adjusted Rand index of hard theme assignments (ticker -> label) across seed runs.

    Only tickers present in both runs of a pair count. Fewer than two runs gives NaN.
    """
    from sklearn.metrics import adjusted_rand_score

    if len(assignments) < 2:
        return float("nan")
    vals = []
    for a, b in combinations(assignments, 2):
        common = sorted(set(a) & set(b))
        if len(common) < 2:
            continue
        vals.append(adjusted_rand_score([str(a[t]) for t in common], [str(b[t]) for t in common]))
    return float(np.mean(vals)) if vals else float("nan")


def theme_runs_values(report: Mapping) -> dict:
    """The five ``theme_runs`` column values from a report dict (``None`` where the report has no number)."""
    def pick(section, key):
        sec = report.get(section)
        return sec.get(key) if isinstance(sec, Mapping) else None

    ari = report.get("seed_ari")
    if isinstance(ari, float) and np.isnan(ari):
        ari = None
    return dict(zip(THEME_RUNS_COLUMNS, (
        pick("peer_recall", "mean"), pick("hand_precision", "mean"), pick("purity", "purity"),
        pick("etf_recall", "median"), ari)))
