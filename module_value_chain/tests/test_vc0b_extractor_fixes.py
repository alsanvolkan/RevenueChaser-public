"""Value Chain VC0b extractor fixes (#61): CRM phrase, sentence-bounded relation
window, C16 name guards, MICROCHIP case, precision-tile coverage. No network, no store."""
from __future__ import annotations

import pytest

from RevenueChaser.module_value_chain import aliases, concentration, mentions, precision, report


def _pairs(text):
    return {(m["dst_ticker"], m["relation"]) for m in mentions.find_mentions(text)}


def _tickers(text):
    return {h["ticker"] for h in aliases.find_aliases(text) if not h["context_only"]}


# ── CRM phrase ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "We integrate with Salesforce customer relationship management software.",
    "We integrate with Salesforce customer-relationship-management software.",
    "Salesforce offers customer relationship management (CRM) tools.",
])
def test_customer_relationship_management_is_not_a_customer_cue(text):
    assert _pairs(text) == {("CRM", "other")}


@pytest.mark.parametrize("text", [
    "We have long-standing customer relationships with Salesforce.",
    "Our customers include Salesforce.",
    "Salesforce is one of our largest customers.",
])
def test_customer_cue_still_labels_the_name(text):
    assert _pairs(text) == {("CRM", "customer")}


# ── sentence-bounded relation window ──────────────────────────────────────
@pytest.mark.parametrize("text", [
    "Apple is a long-time partner. Our suppliers ship daily.",
    "Our suppliers ship daily. Apple is a long-time partner.",
    "Apple is a long-time partner; Suppliers ship daily.",
    "We compete on price. Apple is a long-time partner. Our customers vary.",
])
def test_a_verb_in_another_sentence_does_not_label_the_name(text):
    assert _pairs(text) == {("AAPL", "other")}


def test_the_verb_in_the_same_sentence_still_labels_the_name():
    text = "Our customers vary. We purchase wafers from TSMC. Apple is a partner."
    assert ("TSM", "supplier") in _pairs(text)
    assert ("AAPL", "other") in _pairs(text)


def test_a_legal_form_abbreviation_does_not_end_the_sentence():
    # "Inc." is not a sentence end, so "We sell to" still reaches Qualcomm.
    assert _pairs("We sell to Example Inc. Qualcomm buys modules too.") == {("QCOM", "customer")}


def test_a_newline_does_not_end_the_sentence():
    assert _pairs("Our largest customers are\nQualcomm and others.") == {("QCOM", "customer")}


def test_the_snippet_still_spans_the_mention_window():
    text = "Our suppliers ship daily. Apple is a long-time partner."
    (m,) = mentions.find_mentions(text)
    assert m["snippet"] == text


def test_sentence_spans_match_the_concentration_split():
    from RevenueChaser.module_value_chain import sentences
    text = "Apple Inc. is big. Dell Inc. buys; Others sell. U.S. sales grew. tail"
    spans = sentences.spans(text)
    assert [text[a:b] for a, b in spans] == list(concentration._sentences(text))
    assert [text[a:b] for a, b in spans] == [
        "Apple Inc. is big.", "Dell Inc. buys;", "Others sell.", "U.S. sales grew. tail"]


def test_sentence_bounds_cover_every_position():
    from RevenueChaser.module_value_chain import sentences
    text = "One. Two three. Four"
    b = sentences.Bounds(text)
    assert b.at(0) == (0, 4)
    assert b.at(6) == (5, 15)
    assert b.at(len(text) - 1) == (16, 20)
    assert sentences.Bounds("").at(0) == (0, 0)


# ── C16 name guards ────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "Our customers include Cadence Bank and other lenders.",
    "Our customers include Apple Hospitality REIT.",
    "Our customers include Eaton Vance funds.",
])
def test_c16_names_are_not_the_company(text):
    assert _pairs(text) == set()


@pytest.mark.parametrize("text", [
    "Cadence Bank accounted for 12% of our revenues.",
    "Apple Hospitality REIT accounted for 12% of our revenues.",
    "Eaton Vance accounted for 12% of our revenues.",
])
def test_c16_names_are_not_concentration_customers(text):
    assert concentration.find(text) == []


@pytest.mark.parametrize("text,ticker", [
    ("Our customers include Cadence Design Systems.", "CDNS"),
    ("Our customers include Eaton Corporation.", "ETN"),
    ("Our customers include Apple.", "AAPL"),
])
def test_c16_positive_controls_stay_customers(text, ticker):
    assert _pairs(text) == {(ticker, "customer")}


def test_c16_positive_control_concentration():
    (c,) = concentration.find("Eaton Corporation accounted for 11% of our net sales.")
    assert (c["dst_ticker"], c["relation"], c["share_pct"]) == ("ETN", "customer", 11.0)


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
