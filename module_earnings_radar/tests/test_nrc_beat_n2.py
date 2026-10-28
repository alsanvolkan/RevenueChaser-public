"""Fable review N2 (Earnings Radar, NRC): the event-beat slot read "beat" in the thesis text before "miss", so R2's
bear reason "beat trend declining" and its mixed reason scored as a beat (45) instead of a miss (30) or neutral
(50). The slot now reads R2's own result in the card's rule audit (its score's sign, and its "strong beat" reason);
the thesis keyword read stays only for a card without one. Offline."""
from RevenueChaser.module_earnings_radar.models import TradeCard
from RevenueChaser.module_earnings_radar.pipeline import nrc_from_card


def _beat(score, reasons, thesis=None, available=True):
    audit = {"rule2_guidance": {"available": available, "score": score, "reasons": reasons}}
    card = TradeCard(ticker="AAA", direction_score=0, key_thesis=thesis if thesis is not None else "; ".join(reasons),
                     rotation_quadrant="", rule_audit=audit)
    return nrc_from_card(card)["beat"]


def test_a_declining_misser_is_a_miss_not_a_beat(rawstore_tmp_db):
    assert _beat(-45.0, ["consistent miss (-6.0%)", "beat trend declining (recent -8.0% vs earlier -2.0%)"]) == 30.0
    assert _beat(-15.0, ["beat trend declining (recent -1.0% vs earlier 2.0%)"]) == 30.0


def test_mixed_guidance_is_neutral(rawstore_tmp_db):
    assert _beat(0.0, ["mixed guidance quality — beat trend declining (recent 1.0% vs earlier 3.0%)"]) == 50.0


def test_beats_keep_their_slots(rawstore_tmp_db):
    assert _beat(45.0, ["strong beat history (6.0% recency-wtd)", "high beat rate (88%)"]) == 60.0
    assert _beat(15.0, ["high beat rate (80%)"]) == 45.0


def test_r2_unavailable_is_neutral_whatever_the_thesis_says(rawstore_tmp_db):
    assert _beat(None, ["no earnings history"], thesis="strong beat in revenue", available=False) == 50.0


def test_a_card_without_an_r2_audit_keeps_the_keyword_read(rawstore_tmp_db):
    card = TradeCard(ticker="AAA", direction_score=0, key_thesis="consistent miss; beat trend declining",
                     rotation_quadrant="")
    assert nrc_from_card(card)["beat"] == 30.0, "miss is read before beat"
