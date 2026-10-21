"""#118 PR B: C25 (no prefix name resolution), C27 (minimum labelled sample),
C19 (graph build indexes the edges once, same output). No network."""
from __future__ import annotations

import random

from RevenueChaser.module_value_chain import aliases, config, engine, precision, report


# ── C25 ────────────────────────────────────────────────────────────────────
def test_unsafe_prefix_resolver_is_gone():
    """``resolve_name`` mapped "Apple Hospitality REIT" to AAPL by prefix; it had
    no production caller, so it is deleted rather than kept as a trap."""
    assert not hasattr(aliases, "resolve_name")


# ── C27 ────────────────────────────────────────────────────────────────────
def _labels(n_true, n_false):
    return ([{"src": "A", "dst_ticker": "B", "relation": "supplier", "label": True}] * n_true
            + [{"src": "A", "dst_ticker": "C", "relation": "customer", "label": False}] * n_false)


def test_gate_needs_a_minimum_labelled_sample():
    assert precision.MIN_LABELED_N == 30
    rep = precision.precision(_labels(1, 0))
    assert rep["precision"] == 1.0 and rep["passes"] is False
    assert rep["status"] == "insufficient sample"
    rep = precision.precision(_labels(20, 9))   # n = 29
    assert rep["passes"] is False and rep["status"] == "insufficient sample"


def test_gate_passes_at_the_minimum_sample():
    rep = precision.precision(_labels(21, 9))   # n = 30, precision 0.70
    assert rep["passes"] is True and rep["status"] == "pass"
    assert precision.precision(_labels(20, 10))["status"] == "fail"


def test_tile_shows_the_below_minimum_state():
    html = report._precision_tile({"n": 2, "tp": 2, "precision": 1.0, "passes": False,
                                   "status": "insufficient sample", "links": 147, "covered": 2})
    assert 'class="stat warn"' in html
    assert "insufficient sample (min 30)" in html
    assert "hand-checked n=2" in html and "labels cover 2 of 147 links" in html


# ── C19 ────────────────────────────────────────────────────────────────────
def _reference_focal_graph(edges, ticker, depth=2, max_hop2=config.MAX_HOP2):
    """The pre-#118 ``focal_graph``, verbatim in behaviour: the oracle. A link also carries the edge's
    ``share_pct`` (#387 D7), a copied field that the index does not change."""
    ticker = (ticker or "").upper()
    by_src: dict = {}
    for e in edges:
        by_src.setdefault(e["src"], []).append(e)
    seen = {ticker}
    links = []

    def _add(e, hop, incoming=False):
        links.append({"src": e["src"], "dst": e.get("dst_ticker") or e["dst_name"],
                      "dst_ticker": e.get("dst_ticker") or "",
                      "relation": e["relation"], "snippet": e.get("snippet") or "",
                      "share_pct": e.get("share_pct"),
                      "hop": hop, "incoming": incoming})

    first = [e for e in by_src.get(ticker, []) if e["relation"] in ("supplier", "customer")]
    for e in first:
        _add(e, 1)
        if depth >= 2 and e.get("dst_ticker"):
            seen.add(e["dst_ticker"])
    for e in edges:
        if (e.get("dst_ticker") == ticker and e["src"] != ticker
                and e["relation"] in ("supplier", "customer")):
            _add(e, 1, incoming=True)
    if depth >= 2:
        hop2 = 0
        for e in first:
            dst = e.get("dst_ticker")
            if not dst or hop2 >= max_hop2:
                continue
            for e2 in by_src.get(dst, []):
                if e2["relation"] not in ("supplier", "customer"):
                    continue
                _add(e2, 2)
                hop2 += 1
                if hop2 >= max_hop2:
                    break
    return {"ticker": ticker, "links": links}


def _fixture_edges(n_tickers=120, n_edges=3000, seed=118):
    rng = random.Random(seed)
    names = [f"T{i:03d}" for i in range(n_tickers)]
    rels = ["supplier", "customer", "competitor", "other"]
    out = []
    for i in range(n_edges):
        dst = rng.choice(names + ["", ""])  # some private (unresolved) names
        out.append({"src": rng.choice(names), "dst_name": dst or f"Private {i}",
                    "dst_ticker": dst, "relation": rng.choice(rels),
                    "snippet": f"s{i}", "filed": "2026-02-01"})
    return names, out


def test_indexed_graphs_equal_the_old_per_ticker_graphs():
    names, edges = _fixture_edges()
    tickers = names + ["NONE", "t001"]
    want = {t: _reference_focal_graph(edges, t, depth=2) for t in tickers}
    assert report._graphs({"edges": edges}, tickers) == want
    index = engine.edge_index(edges)
    for t in tickers:
        assert engine.focal_graph(edges, t, depth=2, index=index) == want[t]
        assert engine.focal_graph(edges, t) == want[t]   # old call shape still works
    assert engine.focal_graph(edges, "T001", depth=1) == _reference_focal_graph(edges, "T001", 1)


def test_graphs_index_the_edges_once(monkeypatch):
    names, edges = _fixture_edges(n_tickers=50, n_edges=500)
    calls = {"n": 0}
    real = engine._norm

    def _counting(e):
        calls["n"] += 1
        return real(e)

    monkeypatch.setattr(engine, "_norm", _counting)
    report._graphs({"edges": edges}, names)
    assert calls["n"] == 1
