"""Value Chain VC0b fixes (#61) that outlived the text scans (#748): the C16 name guards and the MICROCHIP case of
the alias table, which the name resolver still reads, and the precision-tile coverage. No network, no store."""
from __future__ import annotations

import pytest

from RevenueChaser.module_value_chain import aliases, precision, report


def _tickers(text):
    return {h["ticker"] for h in aliases.find_aliases(text) if not h["context_only"]}


# ── C16 name guards ────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "Our customers include Cadence Bank and other lenders.",
    "Our customers include Apple Hospitality REIT.",
    "Our customers include Eaton Vance funds.",
])
def test_c16_names_are_not_the_company(text):
    assert _tickers(text) == set()


@pytest.mark.parametrize("text,ticker", [
    ("Our customers include Cadence Design Systems.", "CDNS"),
    ("Our customers include Eaton Corporation.", "ETN"),
    ("Our customers include Apple.", "AAPL"),
])
def test_c16_positive_controls_stay_customers(text, ticker):
    assert _tickers(text) == {ticker}


# ── MICROCHIP case ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["We buy from MICROCHIP TECHNOLOGY INC.",
                                  "We buy from Microchip Technology Inc."])
def test_microchip_proper_noun_resolves(text):
    assert _tickers(text) == {"MCHP"}


@pytest.mark.parametrize("text", ["A global microchip shortage hurt suppliers.",
                                  "Each card carries an embedded microchip."])
def test_lowercase_microchip_is_a_word_not_the_company(text):
    assert _tickers(text) == set()


# ── precision tile coverage ────────────────────────────────────────────────
def _edge(src, dst, rel):
    return {"src": src, "dst_ticker": dst, "relation": rel}


def test_gate_reports_label_coverage_of_served_links(tmp_path):
    import json
    p = tmp_path / "labels.json"
    p.write_text(json.dumps([
        {"src": "A", "dst_ticker": "B", "relation": "supplier", "label": True},
        {"src": "A", "dst_ticker": "C", "relation": "customer", "label": False},
        {"src": "A", "dst_ticker": "D", "relation": "customer", "label": None},
        {"src": "Z", "dst_ticker": "B", "relation": "supplier", "label": True},
    ]), encoding="utf-8")
    served = [_edge("A", "B", "supplier"), _edge("A", "B", "supplier"),   # duplicate key
              _edge("A", "C", "customer"), _edge("A", "D", "customer"),
              _edge("A", "E", "competitor"), _edge("A", "F", "other"),    # not links
              _edge("Q", "R", "supplier")]
    rep = precision.gate(path=p, served=served)
    assert (rep["links"], rep["covered"], rep["n"]) == (4, 2, 2)


def test_gate_without_served_has_no_coverage(tmp_path):
    rep = precision.gate(path=tmp_path / "missing.json")
    assert "links" not in rep and "covered" not in rep


def test_committed_fixture_coverage_equals_n():
    labels = precision.load_labels()
    served = [_edge(l["src"], l["dst_ticker"], l["relation"]) for l in labels[:10]]
    rep = precision.gate(served=served)
    assert rep["covered"] == rep["n"] == 10
    assert rep["links"] == len({(e["src"], e["dst_ticker"], e["relation"]) for e in served})


def test_tile_shows_label_coverage():
    html = report._precision_tile({"n": 2, "tp": 2, "precision": 1.0, "passes": True,
                                   "links": 147, "covered": 2})
    assert "labels cover 2 of 147 links" in html
    assert "hand-checked n=2" in html


def test_unmeasured_tile_shows_coverage_when_links_known():
    html = report._precision_tile({"n": 0, "precision": None, "links": 147, "covered": 0})
    assert "not measured" in html and "labels cover 0 of 147 links" in html


def test_tile_without_links_has_no_coverage_text():
    assert "labels cover" not in report._precision_tile(None)
    assert "labels cover" not in report._precision_tile({"n": 3, "precision": 0.9, "passes": True})
