"""The seven small findings (N1-N7) of the SPX Gamma Fable review (spx_gamma-fable-review.md).

Each section names its finding. They were nits: wording that contradicted the numbers, or an edge the
rule did not cover.
"""
from __future__ import annotations

from RevenueChaser.module_spx_gamma import relations
from RevenueChaser.module_spx_gamma.summary import charm_card


# ── N1: the Relations card and the Charm card read the same charm sum the same way ──────────────
def _charm_rows(vol: float | None, oi: float | None = None) -> list[dict]:
    return [{"strike": 7800.0, "call_charm_vol": vol, "put_charm_vol": None if vol is None else 0.0,
             "call_charm_oi": oi, "put_charm_oi": None if oi is None else 0.0}]


def _relations_charm(rows: list[dict]) -> dict:
    return {c["greek"]: c for c in relations.greek_relations({"spot_exposures": rows})}["Charm"]


def _live_direction(rows: list[dict]) -> str:
    return dict((r[0], r[2]) for r in charm_card(rows, {}, 7800.0)["rows"])["Live charm"]


def test_n1_both_cards_name_the_same_direction_for_each_sign():
    for v in (5e8, -5e8, 0.0):
        rows = _charm_rows(v, v)
        assert _relations_charm(rows)["row"][0][2] == _live_direction(rows), v


def test_n1_a_zero_charm_sum_is_neither_buying_nor_selling():
    rows = _charm_rows(0.0, 0.0)
    card = _relations_charm(rows)
    assert "selling" not in card["headline"].lower() and "buying" not in card["headline"].lower()
    assert card["row"][0][2] not in ("Buy pressure", "Sell pressure")
    positioning = dict((r[0], r[2]) for r in charm_card(rows, {}, 7800.0)["rows"])["Positioning"]
    assert not positioning.startswith(("Buy pressure", "Sell pressure"))


def test_n1_rows_without_charm_values_are_unknown_not_negative():
    card = _relations_charm(_charm_rows(None, None))
    assert card["headline"] == "Charm — unknown"
