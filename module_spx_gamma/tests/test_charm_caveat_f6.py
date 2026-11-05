"""F6 (Fable report review, 5 Nov 2026): the charm cards carry the 02-06 Nov report's sign-rule warning.

The report warns that UW's summed charm sign can differ from a Black-Scholes sum over the same contracts, so the
board's "buy/sell pressure" reading is UW's sign, not a confirmed dealer hedge.
"""
from __future__ import annotations

from RevenueChaser.module_spx_gamma import charm_pin, relations, summary
from .conftest import make_snapshot


def test_summary_charm_card_carries_the_caveat():
    card = summary.build_summary(make_snapshot())[3]
    assert charm_pin.CHARM_SIGN_CAVEAT in card["narrative"]


def test_relations_charm_card_carries_the_caveat():
    card = next(c for c in relations.greek_relations(make_snapshot()) if c["greek"] == "Charm")
    assert charm_pin.CHARM_SIGN_CAVEAT in card["english"]


def test_no_data_card_has_no_caveat():
    card = summary.charm_card([], None, 7800.0)
    assert charm_pin.CHARM_SIGN_CAVEAT not in card["narrative"]
