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
    return [str(t).upper() for t in tickers], m


def _clean(value):
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def build_report(tickers: Sequence[str], scores: np.ndarray, *,
                 fine: Mapping | None = None, medium: Mapping | None = None,
                 industry_by: Mapping[str, str] | None = None,
                 seeds: Sequence[Mapping[str, object]] | None = None,
                 etf_holdings: Mapping[str, object] | None = None) -> dict:
    universe = set(tickers)
    hand = peer_sets.hand_sets_for(universe)
    etf = peer_sets.etf_sets_for(etf_holdings or {}, universe)
    all_sets = hand.sets + etf.sets

    recall = metrics.peer_recall_at_k(tickers, scores, all_sets)
    recall["by_kind"] = {
        kind: metrics.peer_recall_at_k(tickers, scores, [s for s in all_sets if s.kind == kind])["mean"]
        for kind in ("hand", "etf")}
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
    }
    out["theme_runs"] = metrics.theme_runs_values(out)
    return out
