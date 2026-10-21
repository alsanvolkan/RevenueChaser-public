"""Value Chain edge precision — the ``>= 0.70`` quality gate.

Labels live in ``module_value_chain/fixtures/labeled_80.json``: a list of
``{id, src, dst_ticker, relation, label, borderline, snippet}`` where ``label``
is ``true``/``false`` (a human judgement that the edge is correct). They are the
stratified sample (40 supplier + 40 customer, seed 26) hand-labelled in
``docs/reviews/2026-09-26_value_chain_dashboard_review.md`` Appendix A;
borderline calls are labelled false, as in the review. Precision = TP / (TP + FP).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "labeled_80.json"
MIN_PRECISION = 0.70
# Below this many judged labels the gate reports "insufficient sample", never a
# pass: at n = 30 a 0.70 precision is about +-0.17 at 95% (#118 C27). Served
# filtering shrinks n, so a handful of surviving labels could otherwise pass.
MIN_LABELED_N = 30
MAX_EDGES_PER_TICKER = 40


def load_labels(path: Path | None = None) -> list[dict]:
    p = Path(path or FIXTURE)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def precision(labels: list[dict]) -> dict:
    judged = [l for l in labels if l.get("label") in (True, False)]
    tp = sum(1 for l in judged if l["label"])
    fp = len(judged) - tp
    prec = (tp / len(judged)) if judged else None
    if prec is None or len(judged) < MIN_LABELED_N:
        status = "insufficient sample"
    else:
        status = "pass" if prec >= MIN_PRECISION else "fail"
    return {"n": len(judged), "tp": tp, "fp": fp,
            "precision": round(prec, 4) if prec is not None else None,
            "passes": status == "pass", "status": status}


def edges_per_ticker(edges: list[dict]) -> dict[str, int]:
    return dict(Counter(e["src"] for e in edges))


def _key(e: dict) -> tuple:
    return (e.get("src"), e.get("dst_ticker"), e.get("relation"))


def served_links(served: list[dict]) -> set[tuple]:
    """Distinct ``(src, dst_ticker, relation)`` supplier / customer keys the board serves."""
    return {_key(e) for e in served if e.get("relation") in ("supplier", "customer")}


def served_labels(labels: list[dict], served: list[dict]) -> list[dict]:
    """Labels whose ``(src, dst_ticker, relation)`` edge is still served.

    A label for an edge the current extractor no longer emits says nothing
    about what the board shows, so it is left out of the served precision.
    """
    keys = {_key(e) for e in served}
    return [l for l in labels if _key(l) in keys]


def gate(edges: list[dict] | None = None, path: Path | None = None,
         served: list[dict] | None = None) -> dict:
    labels = load_labels(path)
    links: set[tuple] | None = None
    if served is not None:
        labels = served_labels(labels, served)
        links = served_links(served)
    rep = precision(labels)
    if links is not None:
        # How much of what the board draws the labels can speak for.
        rep["links"] = len(links)
        rep["covered"] = len({_key(l) for l in labels
                              if l.get("label") in (True, False) and _key(l) in links})
    if edges is not None:
        rep["over_max_edges"] = {t: n for t, n in edges_per_ticker(edges).items()
                                 if n > MAX_EDGES_PER_TICKER}
    return rep
