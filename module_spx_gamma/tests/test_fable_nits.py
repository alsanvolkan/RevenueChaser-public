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


# ── N2: a same-day contract is not priced as if a full day were left ────────────────────────────
from RevenueChaser.module_spx_gamma.chains import black_scholes_mid, normalize_option_chain  # noqa: E402

YEAR_HOURS = 365.0 * 24.0


def _unquoted(expiry: str) -> dict:
    yymmdd = expiry[2:4] + expiry[5:7] + expiry[8:10]
    return {"data": [{"option_symbol": f"SPXW{yymmdd}C07800000", "implied_volatility": "0.15"}]}


def test_n2_a_0dte_mid_uses_the_time_left_to_the_close():
    # 19:00 UTC on 14 Aug 2026 is 15:00 New York: one hour to the 16:00 close.
    row = normalize_option_chain(_unquoted("2026-08-14"), 7800.0, "2026-08-14",
                                 snapshot_time="2026-08-14T19:00:00Z")[0]
    assert row["price_source"] == "modeled"
    assert row["mid"] == round(black_scholes_mid(7800.0, 7800.0, 1.0 / YEAR_HOURS, 0.15, "C"), 2)


def test_n2_a_0dte_mid_is_cheaper_than_the_next_day_contract():
    zero = normalize_option_chain(_unquoted("2026-08-13"), 7800.0, "2026-08-13")[0]["mid"]
    one = normalize_option_chain(_unquoted("2026-08-14"), 7800.0, "2026-08-13")[0]["mid"]
    assert zero < one


def test_n2_without_a_fetch_time_a_0dte_mid_gets_one_session_at_most():
    row = normalize_option_chain(_unquoted("2026-08-14"), 7800.0, "2026-08-14")[0]
    assert row["mid"] == round(black_scholes_mid(7800.0, 7800.0, 6.5 / YEAR_HOURS, 0.15, "C"), 2)


def test_n2_a_fetch_after_the_close_leaves_a_small_floor_not_a_day():
    row = normalize_option_chain(_unquoted("2026-08-14"), 7800.0, "2026-08-14",
                                 snapshot_time="2026-08-15T01:00:00Z")[0]
    assert 0 < row["mid"] < black_scholes_mid(7800.0, 7800.0, 1.0 / YEAR_HOURS, 0.15, "C")


def test_n2_a_fetch_before_the_open_still_gets_one_session_at_most():
    # 10:00 UTC is 06:00 New York: ten hours to the close, but only one session of trading.
    row = normalize_option_chain(_unquoted("2026-08-14"), 7800.0, "2026-08-14",
                                 snapshot_time="2026-08-14T10:00:00Z")[0]
    assert row["mid"] == round(black_scholes_mid(7800.0, 7800.0, 6.5 / YEAR_HOURS, 0.15, "C"), 2)


def test_n2_the_snapshot_passes_its_fetch_time_to_the_chain():
    from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot
    from .conftest import make_raw
    raw = make_raw()
    raw["option_chain"] = _unquoted("2026-08-14")
    raw["_meta"] = {**(raw.get("_meta") or {}), "api_snapshot_time": "2026-08-14T19:00:00Z"}
    snap = normalize_snapshot(raw, "2026-08-14")
    spot = snap["current_level"]
    assert snap["option_chain"][0]["mid"] == round(
        black_scholes_mid(spot, 7800.0, 1.0 / YEAR_HOURS, 0.15, "C"), 2)
