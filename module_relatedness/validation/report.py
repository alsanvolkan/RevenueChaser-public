"""Build the Step 0 validation report from in-memory inputs (Relatedness v2, #341). No store, no network.

``build_report`` takes a score matrix and whatever theme inputs a run has, loads the peer sets against the matrix's
tickers (the universe), and returns one JSON-ready dict: the five metrics, the per-set load report, and the five
``theme_runs`` column values. A metric with no input is ``None``, never a made-up number.

Peer recall@25 is measured on every loaded set (hand and ETF) and is also broken out by kind; precision@10 is
measured on the hand sets only. ETF theme recall is gated at the medium level and reported at the fine level.
"""
from __future__ import annotations

import math
from typing import Mapping, Sequence

import numpy as np

from . import metrics, peer_sets


def matrix_from_pairs(rows) -> tuple[list[str], np.ndarray]:
    """``(tickers, matrix)`` from ``{ticker, peer, score}`` rows (the ``relatedness_scores`` shape); NaN = no score."""
    tickers = sorted({str(r["ticker"]).upper() for r in rows} | {str(r["peer"]).upper() for r in rows})
    idx = {t: i for i, t in enumerate(tickers)}
    m = np.full((len(tickers), len(tickers)), np.nan)
    for r in rows:
        if r.get("score") is not None:
            m[idx[str(r["ticker"]).upper()], idx[str(r["peer"]).upper()]] = float(r["score"])
    return tickers, m


def matrix_from_dense(tickers: Sequence[str], matrix) -> tuple[list[str], np.ndarray]:
    m = np.array([[np.nan if v is None else float(v) for v in row] for row in matrix], dtype=float)
    if m.shape != (len(tickers), len(tickers)):
        raise ValueError(f"{len(tickers)} tickers but a {m.shape} score matrix; it must be square, one row and "
                         f"one column per ticker")
    return [str(t).upper() for t in tickers], m


def _upper_themes(themes: Mapping | None, universe: set[str]) -> tuple[dict | None, set[str]]:
    """Themes with upper-cased members, and the members (upper-cased) that are not in the score matrix."""
    if not themes:
        return themes, set()
    out, missing = {}, set()
    for tid, t in themes.items():
        raw = t["members"] if isinstance(t, Mapping) else t
        members = [str(m).upper() for m in raw]
        missing |= {m for m in members if m not in universe}
        out[tid] = {**t, "members": members} if isinstance(t, Mapping) else members
    return out, missing


def _upper_keys(by: Mapping | None, universe: set[str]) -> tuple[dict | None, int]:
    if not by:
        return by, 0
    out = {str(k).upper(): v for k, v in by.items()}
    return out, sum(1 for k in out if k not in universe)


def _upper_seeds(seeds, universe: set[str]) -> tuple[list | None, int]:
    if not seeds:
        return seeds, 0
    out = [{str(k).upper(): v for k, v in s.items()} for s in seeds]
    return out, len({k for s in out for k in s} - universe)


def _clean(value):
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def build_report(tickers: Sequence[str], scores: np.ndarray, *,
                 fine: Mapping | None = None, medium: Mapping | None = None,
                 industry_by: Mapping[str, str] | None = None,
                 seeds: Sequence[Mapping[str, object]] | None = None,
                 etf_holdings: Mapping[str, object] | None = None) -> dict:
    tickers = [str(t).upper() for t in tickers]
    universe = set(tickers)
    fine, miss_fine = _upper_themes(fine, universe)
    medium, miss_medium = _upper_themes(medium, universe)
    industry_by, miss_ind = _upper_keys(industry_by, universe)
    seeds, miss_seeds = _upper_seeds(seeds, universe)
    hand = peer_sets.hand_sets_for(universe)
    etf = peer_sets.etf_sets_for(etf_holdings or {}, universe)
    all_sets = hand.sets + etf.sets

    recall = metrics.peer_recall_at_k(tickers, scores, all_sets)
    # The per-kind means come from the per-set values already computed (one top-k pass, not three).
    recall["by_kind"] = {}
    for kind in ("hand", "etf"):
        vals = [recall["per_set"][s.name] for s in all_sets if s.kind == kind and s.name in recall["per_set"]]
        recall["by_kind"][kind] = float(np.mean(vals)) if vals else None
    out = {
        "hand_sets_version": hand.version,
        "etf_sets_version": etf.version,
        "n_tickers": len(tickers),
        "peer_recall": recall,
        "hand_precision": metrics.precision_at_k(tickers, scores, hand.sets),
        "purity": metrics.theme_purity(fine, industry_by) if fine and industry_by else None,
        "etf_recall": metrics.etf_theme_recall(etf.sets, medium) if medium else None,
        "etf_recall_fine": metrics.etf_theme_recall(etf.sets, fine) if fine else None,
        "seed_ari": _clean(metrics.seed_ari(seeds)) if seeds else None,
        "peer_set_report": {"hand": hand.report, "etf": etf.report},
        # Input keys that name no ticker of the score matrix (after upper-casing), so a typo is visible, not silent.
        "unmatched": {"themes": len(miss_fine | miss_medium), "industries": miss_ind, "seeds": miss_seeds},
    }
    out["theme_runs"] = metrics.theme_runs_values(out)
    return out
