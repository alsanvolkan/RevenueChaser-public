"""Value Chain precision gate (>= 0.70) — synthetic labels, no network."""
from __future__ import annotations

import json

from RevenueChaser.module_value_chain import precision as prec


def _labels(n_true, n_false):
    return ([{"src": "A", "dst_ticker": "B", "relation": "supplier", "label": True}] * n_true
            + [{"src": "A", "dst_ticker": "C", "relation": "customer", "label": False}] * n_false)


def test_precision_passes_at_threshold():
    rep = prec.precision(_labels(21, 9))  # n = 30, the minimum sample (#118 C27)
    assert rep["precision"] == 0.7 and rep["passes"] is True


def test_precision_fails_below_threshold():
    rep = prec.precision(_labels(6, 4))
    assert rep["precision"] == 0.6 and rep["passes"] is False


def test_unlabeled_ignored_and_empty_is_none():
    assert prec.precision([{"src": "A", "label": None}])["precision"] is None
    assert prec.precision([])["passes"] is False


def test_gate_flags_over_max_edges(tmp_path):
    edges = [{"src": "HOT"} for _ in range(41)] + [{"src": "OK"} for _ in range(5)]
    p = tmp_path / "labels.json"
    p.write_text(json.dumps(_labels(24, 6)), encoding="utf-8")
    rep = prec.gate(edges=edges, path=p)
    assert rep["passes"] is True
    assert rep["over_max_edges"] == {"HOT": 41}


def test_committed_fixture_measures_the_review_sample():
    """The 80 hand labels from docs/reviews/2026-09-26_value_chain_dashboard_review.md."""
    labels = prec.load_labels()
    assert len(labels) == 80
    rep = prec.precision(labels)
    assert (rep["n"], rep["tp"], rep["passes"]) == (80, 31, False)
    assert {l["relation"] for l in labels} == {"supplier", "customer"}
    assert sum(1 for l in labels if l.get("borderline")) == 5


def test_load_labels_missing_is_empty():
    from pathlib import Path
    assert prec.load_labels(Path("definitely_missing.json")) == []
