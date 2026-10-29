"""Fable review S4 and N4 (Earnings Radar, R5 positioning). Offline.

S4: the institutional lean was call premium minus put premium. redesign.md R5 locks it to UW's side: bullish =
ask-side calls + bid-side puts, bearish = bid-side calls + ask-side puts, mid dropped, no side = no lean;
institutional = size >= 10 AND premium >= $10k; a multi-leg package is left out of the lean and tagged. So a tape
of institutions selling calls no longer reads "Institutional buying", and the trade reason names the lean only when
the read is already on its side ("tape on the same side", never "R5 confirms").

N4: a name's squeeze Z was taken against a peer group that held the name itself (self + one peer is always
+/-0.707). It is now taken against the other names of its sector only.
"""
import pytest

from RevenueChaser.core.config import INST_PRINT_MIN_PREMIUM
from RevenueChaser.core.providers.unusual_whales import UWClient
from RevenueChaser.module_earnings_radar.pipeline import _institutional_flow_comment, _trade_flow_part
from RevenueChaser.module_earnings_radar.rules import rule5_positioning
from RevenueChaser.module_earnings_radar.rules.rule5_positioning import score as r5_score

QUOTE = {"nbbo_bid": "1.00", "nbbo_ask": "1.10"}


def _print(option_type, premium, size=20, tags=None, price=None, code="auto", quote=True):
    p = {"option_type": option_type, "premium": str(premium), "size": str(size), "upstream_condition_detail": code,
         "tags": tags or []}
    if price is not None:
        p["price"] = str(price)
    if quote:
        p.update(QUOTE)
    return p


def _agg(prints):
    return UWClient.aggregate_flow(object.__new__(UWClient), "AAA", prints)


def test_the_lean_is_read_from_the_print_side():
    agg = _agg([
        _print("call", 50_000, tags=["ask_side"]),             # bought calls: bullish
        _print("call", 40_000, tags=["bid_side"]),             # sold calls: bearish
        _print("put", 30_000, price="1.08"),                   # no tag, nearer the ask: bought puts, bearish
        _print("put", 20_000, price="1.00"),                   # no tag, at the bid: sold puts, bullish
        _print("call", 100_000, tags=["mid_side"]),            # mid: dropped
        _print("call", 50_000, size=5, tags=["ask_side"]),     # a small lot: retail, never in the lean
        _print("call", INST_PRINT_MIN_PREMIUM - 1, tags=["ask_side"]),   # under the $10k print floor
        _print("call", 200_000, tags=["ask_side"], code="mlft"),         # a package leg: left out, tagged
        _print("put", 60_000, quote=False),                    # no tag and no quote: no side
    ])
    assert agg["inst_bull_side"] == 70_000 and agg["inst_bear_side"] == 70_000 and agg["inst_lean_net"] == 0
    assert agg["inst_mid"] == 100_000 and agg["inst_package"] == 200_000 and agg["inst_no_side"] == 60_000
    assert agg["side_served"] is True
    assert agg["inst_call"] == 50_000 + 40_000 + 100_000 + INST_PRINT_MIN_PREMIUM - 1 + 200_000, \
        "the size buckets Swing and R8 read stay as they were"


def test_institutions_selling_calls_is_a_bearish_lean_not_buying():
    agg = _agg([_print("call", 300_000, tags=["bid_side", "bearish"])])
    assert agg["inst_net"] == 300_000, "the old call-minus-put read: 'Institutional buying'"
    label, net, tone = _institutional_flow_comment(agg)
    assert (label, net, tone) == ("Institutional bearish lean", -300_000, "bear")
    assert _trade_flow_part(agg, "BEARISH") == "institutional bearish lean"
    assert _trade_flow_part(agg, "BULLISH") is None and _trade_flow_part(agg, "MIXED") is None
    r = r5_score(institutional_net_delta=agg["inst_lean_net"], institutional_call_premium=agg["inst_bull_side"],
                 institutional_put_premium=agg["inst_bear_side"], institutional_side_served=agg["side_served"])
    assert r.score == -20.0 and "institutional bearish lean" in r.reasons


def test_no_side_served_is_no_lean():
    agg = _agg([_print("call", 300_000, quote=False)])
    assert agg["side_served"] is False and agg["inst_lean_net"] == 0
    assert _institutional_flow_comment(agg) == ("Lean unavailable (no print side)", 0.0, "neutral")
    assert _trade_flow_part(agg, "BULLISH") is None and _trade_flow_part(agg, "BEARISH") is None
    r = r5_score(institutional_net_delta=None, institutional_side_served=False)
    assert not r.available and "no print side: institutional lean unavailable" in r.reasons


def test_a_quiet_tape_is_not_a_missing_side():
    assert _agg([])["side_served"] is None
    assert _agg([_print("call", 50_000, size=5, quote=False)])["side_served"] is None, "retail only: nothing to judge"
    r = r5_score(institutional_net_delta=0, institutional_side_served=None)
    assert not any("print side" in reason for reason in r.reasons)


def test_a_material_package_is_tagged_and_left_out_of_the_lean():
    agg = _agg([_print("call", 80_000, tags=["ask_side"]), _print("put", 120_000, tags=["ask_side"], code="mlet")])
    label, net, tone = _institutional_flow_comment(agg)
    assert net == 80_000 and tone == "bull" and label == "Institutional bullish lean (package risk)"
    r = r5_score(institutional_net_delta=agg["inst_lean_net"], institutional_call_premium=agg["inst_bull_side"],
                 institutional_put_premium=agg["inst_bear_side"], institutional_side_served=True,
                 institutional_package_premium=agg["inst_package"])
    assert "package-risk: multi-leg premium left out of the lean" in r.reasons


def test_a_small_package_is_no_risk_and_a_small_net_is_thin():
    agg = _agg([_print("call", 20_000, tags=["ask_side"]), _print("put", 12_000, tags=["ask_side"], code="mlet")])
    assert _institutional_flow_comment(agg) == ("Thin flow", 20_000, "neutral")
    r = r5_score(institutional_net_delta=agg["inst_lean_net"], institutional_call_premium=agg["inst_bull_side"],
                 institutional_put_premium=agg["inst_bear_side"], institutional_side_served=True,
                 institutional_package_premium=agg["inst_package"])
    assert r.available and r.score == 0.0 and "thin institutional lean" in r.reasons
    assert not any("package-risk" in reason for reason in r.reasons)

