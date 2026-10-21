"""Tests for the summary cards."""
from __future__ import annotations

from RevenueChaser.module_spx_gamma import summary as sm
from RevenueChaser.module_spx_gamma.narrative import build_walkthrough


def test_summary_has_four_cards(snapshot):
    cards = sm.build_summary(snapshot)
    assert [c["title"] for c in cards] == ["Delta", "Gamma", "Vanna", "Charm"]
    assert cards[0]["tag"] == "TREND"
    assert cards[1]["tag"] == "MAGNET"


def test_delta_card_bullish_lean(snapshot):
    delta = sm.build_summary(snapshot)[0]
    assert "bullish lean" in delta["narrative"]  # conftest net delta is positive
    assert any(r[0] == "Net DEX" for r in delta["rows"])


def test_gamma_card_regime(snapshot):
    gamma = sm.build_summary(snapshot)[1]
    assert "Amplifying" in gamma["narrative"] or "Dampening" in gamma["narrative"]
    assert any(r[0] == "Γ vs prev close" for r in gamma["rows"])
    assert any(r[0] == "0–1d rolls off" for r in gamma["rows"])


def test_vanna_charm_cards(snapshot):
    cards = sm.build_summary(snapshot)
    vanna = cards[2]
    charm = cards[3]
    assert any(r[0] == "Net Vanna" for r in vanna["rows"])
    assert any(r[0] == "Where it matters" for r in charm["rows"])


def test_signal_pills(snapshot):
    cards = sm.build_summary(snapshot)
    for c in cards:
        assert "signal" in c
        assert c["signal"]["tone"] in ("green", "red", "amber")
    assert cards[0]["signal"]["label"] == "Bullish"  # conftest net delta positive


def test_at_a_glance(snapshot):
    cards = sm.build_summary(snapshot)
    strip = sm.at_a_glance(cards)
    assert "Delta Bullish" in strip
    assert "Gamma" in strip and "Vanna" in strip and "Charm" in strip


def test_walkthrough_mentions_levels(snapshot):
    paras = build_walkthrough(snapshot)
    assert any("Spot trades at" in p for p in paras)
    assert any("Call Wall" in p for p in paras)
    assert any("liquidity pockets" in p for p in paras)


def test_signal_matrix_consensus(snapshot):
    cards = sm.build_summary(snapshot)
    m = sm.signal_matrix(cards)
    assert m["rows"]
    assert m["total_directional"] >= 1
    assert m["stance"] in ("Bullish consensus", "Bearish consensus", "Divergence", "Neutral")


def test_signal_matrix_all_bullish():
    cards = [
        {"title": "Delta", "signal": {"label": "Bullish", "tone": "green"}},
        {"title": "Gamma", "signal": {"label": "Dampening", "tone": "green"}},
        {"title": "Vanna", "signal": {"label": "Short", "tone": "green"}},
    ]
    m = sm.signal_matrix(cards)
    assert m["stance"] == "Bullish consensus"
    assert m["votes"]["bullish"] == 2  # Delta + Vanna (Short = cushion)
    assert m["votes"]["bearish"] == 0


def test_signal_matrix_divergence():
    cards = [
        {"title": "Delta", "signal": {"label": "Bullish", "tone": "green"}},
        {"title": "Gamma", "signal": {"label": "Dampening", "tone": "green"}},
        {"title": "Vanna", "signal": {"label": "Long", "tone": "red"}},
    ]
    m = sm.signal_matrix(cards)
    assert m["stance"] == "Divergence"
    assert m["votes"]["bullish"] == 1
    assert m["votes"]["bearish"] == 1
