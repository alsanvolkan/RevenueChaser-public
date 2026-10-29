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


# ── N3: "at the wall" means within 1% of it on either side, not anywhere past it ───────────────
from RevenueChaser.module_spx_gamma.flow_signals import evaluate_flow_signal  # noqa: E402


def _flow_snap(spot: float, call_wall: float | None = 7800.0, put_wall: float | None = None,
               call_wall_vol: float | None = None, flow: list[dict] | None = None) -> dict:
    return {"current_level": spot,
            "levels": {"upper_resistance_band": call_wall, "call_wall_vol": call_wall_vol,
                       "lower_support_band": put_wall},
            "flow_strikes": flow or []}


def _calls(strike: float) -> list[dict]:
    return [{"strike": strike, "call_volume": 3000, "put_volume": 500, "call_volume_ask_side": 2400,
             "call_volume_bid_side": 600}]


def _puts(strike: float) -> list[dict]:
    return [{"strike": strike, "call_volume": 500, "put_volume": 3000, "call_volume_ask_side": 200,
             "call_volume_bid_side": 300}]


def test_n3_a_spot_3pct_above_the_call_wall_is_not_at_it():
    res = evaluate_flow_signal(_flow_snap(spot=8034.0, flow=_calls(7800.0)))
    assert res["signal"] != "SQUEEZE_ALERT"
    assert "at Call Wall" not in res["narrative"]
    assert "holding" not in res["narrative"].lower()


def test_n3_a_spot_within_1pct_above_the_call_wall_still_counts():
    res = evaluate_flow_signal(_flow_snap(spot=7850.0, flow=_calls(7800.0)))
    assert res["signal"] == "SQUEEZE_ALERT"


def test_n3_a_spot_5pct_below_the_put_wall_is_not_a_defended_wall():
    res = evaluate_flow_signal(_flow_snap(spot=7125.0, call_wall=None, put_wall=7500.0, flow=_puts(7500.0)))
    assert res["label"] != "Put Wall Defense"
    assert "holding" not in res["narrative"].lower()


def test_n3_a_spot_just_below_the_put_wall_is_still_at_it():
    res = evaluate_flow_signal(_flow_snap(spot=7480.0, call_wall=None, put_wall=7500.0, flow=_puts(7500.0)))
    assert res["label"] == "Put Wall Defense"


# ── N4: the spot row says where the spot came from ──────────────────────────────────────────────
def _details(raw_over: dict | None = None) -> dict:
    from RevenueChaser.module_spx_gamma import render
    from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot
    from .conftest import make_raw
    raw = make_raw()
    raw.update(raw_over or {})
    return {d["level"]: d for d in render.build_context(normalize_snapshot(raw, "2026-08-14"))["details"]}


def test_n4_a_live_spot_is_not_called_a_verified_candle_close():
    row = _details({"spot": {"source": "yfinance (live)", "value": 7790.0}})["SPX Close / Spot"]
    assert "candle close" not in row["method"].lower()
    assert "live" in row["method"].lower() and "yfinance" in row["method"]
    assert "quote read at fetch time" in row["method"]


def test_n4_a_stored_close_names_its_source():
    source = "market_series SPX (cboe) (close of 2026-08-14)"
    row = _details({"spot": {"source": source, "value": 7784.0}})["SPX Close / Spot"]
    assert source in row["method"]
    assert "Verified" not in row["method"]


def test_n4_without_a_recorded_spot_the_row_names_the_candle_source():
    row = _details()["SPX Close / Spot"]
    assert "yfinance ^GSPC" in row["method"] and "Verified" not in row["method"]


# ── N5: the Put Wall row says which put wall it shows ───────────────────────────────────────────
def test_n5_the_band_put_wall_is_not_labelled_a_provider_level():
    row = _details()["Put Wall"]
    assert row["value"].startswith("7,500")
    assert row["ref"] != "Provider GEX Level"
    assert "181-365" in row["method"]
    assert "positive" not in row["method"].lower()


def test_n5_the_provider_put_wall_keeps_its_provider_label():
    row = _details({"strike_expiry_all": {}})["Put Wall"]
    assert row["ref"] == "Provider GEX Level"
    assert "181-365" not in row["method"]


# ── N6: the Brier verdict is measured against the base rate, and the scorecard counts days ──────
import pytest  # noqa: E402

from RevenueChaser.module_spx_gamma import brier  # noqa: E402

CONSTANT = {"Consolidation": 0.45, "Upside": 0.25, "Downside": 0.30}


def _scored(tmp_path, outcomes: list[str]):
    p = tmp_path / "brier.json"
    for i, outcome in enumerate(outcomes):
        day = f"2026-08-{10 + i:02d}"
        brier.record_probs(day, CONSTANT, path=p)
        brier.score_date(day, outcome, path=p)
    return brier.scorecard(path=p)


def test_n6_constant_probabilities_beat_one_third_but_not_the_base_rate(tmp_path):
    card = _scored(tmp_path, ["Consolidation", "Consolidation", "Consolidation", "Upside"])
    assert card["average"] == pytest.approx(0.555)
    assert card["average"] < card["equal_weight_reference"]          # the old test said "calibrated" here
    assert card["base_rate_reference"] == pytest.approx(0.375)       # 1 - (0.75^2 + 0.25^2)
    assert card["calibrated"] is False
    assert card["base_rate_skill_score"] == pytest.approx(1 - 0.555 / 0.375)
    assert "base-rate reference" in card["verdict"]
    assert "is calibrated" not in card["verdict"]


def test_n6_one_outcome_class_cannot_be_judged_against_the_base_rate(tmp_path):
    card = _scored(tmp_path, ["Consolidation", "Consolidation"])
    assert card["base_rate_reference"] == pytest.approx(0.0)
    assert card["calibrated"] is None
    assert card["base_rate_skill_score"] is None


def test_n6_the_scorecard_counts_days_not_weeks(tmp_path):
    from pathlib import Path
    templates = Path(brier.__file__).parent / "templates"
    for name in ("section_position_map.md.j2", "section_position_map.html.j2"):
        text = (templates / name).read_text(encoding="utf-8")
        assert "Scored weeks" not in text and "Scored days" in text, name
    assert "weeks" not in brier.scorecard(path=tmp_path / "none.json")["verdict"]


def test_n6_recorded_but_unscored_days_do_not_move_the_base_rate(tmp_path):
    p = tmp_path / "brier.json"
    for day, outcome in (("2026-08-10", "Consolidation"), ("2026-08-11", "Upside")):
        brier.record_probs(day, CONSTANT, path=p)
        brier.score_date(day, outcome, path=p)
    brier.record_probs("2026-08-12", CONSTANT, path=p)        # today's record, not scored yet
    assert brier.scorecard(path=p)["base_rate_reference"] == pytest.approx(0.5)


# ── N7: the VIX term mode reads the 9-day point too ─────────────────────────────────────────────
from RevenueChaser.module_spx_gamma.normalize import normalize_vix  # noqa: E402
from RevenueChaser.module_spx_gamma.vix import vix_term_mode  # noqa: E402


def test_n7_a_9d_above_spot_vix_is_front_backwardation_even_in_contango():
    assert vix_term_mode({"^VIX": 15.0, "^VIX9D": 17.0, "^VIX3M": 18.0}) == "front backwardation"


def test_n7_the_old_readings_are_unchanged():
    assert vix_term_mode({"^VIX": 15.0, "^VIX9D": 13.0, "^VIX3M": 18.0}) == "contango"
    assert vix_term_mode({"^VIX": 25.0, "^VIX9D": 28.0, "^VIX3M": 22.0}) == "backwardation"
    assert vix_term_mode({"^VIX": 15.0, "^VIX3M": 18.0}) == "contango"      # no 9D: as before
    assert vix_term_mode({"^VIX9D": 17.0, "^VIX3M": 18.0}) is None


def test_n7_a_stored_payload_is_read_with_the_9d_rule():
    vix = normalize_vix({"tickers": {"^VIX": 15.0, "^VIX9D": 17.0, "^VIX3M": 18.0}, "term_mode": "contango"})
    assert vix["term_mode"] == "front backwardation"


def test_n7_the_board_explains_front_backwardation():
    from pathlib import Path
    html = (Path(brier.__file__).parent / "templates" / "section_position_map.html.j2").read_text(encoding="utf-8")
    assert "vix.term_mode == 'front backwardation'" in html
