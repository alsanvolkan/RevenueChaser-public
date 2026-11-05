"""Tests for the Greek Relations engine (sign-aware, data-driven)."""
from __future__ import annotations

from RevenueChaser.module_spx_gamma import relations
from .conftest import make_snapshot


def test_relation_cards(snapshot):
    # A9 added the reports' vol/VIX and flow relations before Vomma (the caveat card stays last).
    cards = relations.greek_relations(snapshot)
    assert [c["greek"] for c in cards] == ["Gamma", "Vanna", "Charm", "Delta", "Vol / VIX", "Flow", "Vomma"]


def test_each_card_has_a_headline_and_english(snapshot):
    # The board is English (avo, 5 Nov 2026); the Turkish line is optional and the two A9 cards carry none.
    for c in relations.greek_relations(snapshot):
        assert c["headline"]
        assert c["english"]


def test_gamma_card_matches_regime(snapshot):
    cards = {c["greek"]: c for c in relations.greek_relations(snapshot)}
    gamma = cards["Gamma"]
    # conftest: spot is below the OI flip -> Short Gamma -> Amplifying
    assert "amplifying" in gamma["headline"].lower()


def test_vanna_card_sign(snapshot):
    cards = {c["greek"]: c for c in relations.greek_relations(snapshot)}
    vanna = cards["Vanna"]
    assert "vanna" in vanna["headline"].lower()


def test_causal_chain_has_four_steps(snapshot):
    chain = relations.causal_chain(snapshot)
    assert len(chain) == 4
    assert chain[0]["label"] == "IV rises"
    assert chain[3]["label"] == "Move damped / amplified"
    for s in chain:
        assert s["english"] and s["turkish"]


def test_glossary_tr_entries():
    assert len(relations.GLOSSARY_TR) >= 6
    for g in relations.GLOSSARY_TR:
        assert g["greek"] and g["def"]


def test_vomma_is_static_second_order():
    snap = make_snapshot()
    vomma = relations.greek_relations(snap)[-1]
    assert vomma["greek"] == "Vomma"
    assert "2nd order" in vomma["headline"].lower()
