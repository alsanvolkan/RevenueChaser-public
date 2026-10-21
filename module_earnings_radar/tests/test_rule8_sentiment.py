"""R8 — Sentiment. Table-driven, no network.

Pin: retail call share >65% -> +20 unscaled (momentum; R8_SIGN default +1);
<=65% with a real share is available at 0; no data -> unavailable (None, never
0). LM / small-lot kwargs still score if passed; pipeline does not pass them.
"""
import pytest

from RevenueChaser.module_earnings_radar.rules.rule8_sentiment import score


def test_no_data_unavailable_not_zero():
    r = score()
    assert r.available is False
    assert r.score is None
    assert r.coverage == 0


def test_retail_call_share_momentum_unscaled():
    r = score(retail_call_share=0.70)
    assert r.available is True
    assert r.score == 20.0
    assert r.coverage == 1.0
    assert any("extreme retail call buying" in x for x in r.reasons)


def test_retail_below_threshold_no_penalty():
    r = score(retail_call_share=0.40)
    assert r.available is True
    assert r.score == 0.0
    assert r.coverage == 1.0
    assert r.raw_metrics["retail_call_share"] == 0.40


def test_small_lot_call_share_penalty_if_passed():
    r = score(small_lot_call_share=0.70)
    assert r.available is True
    assert r.score == -15.0
    assert any("small-lot call euphoria" in x for x in r.reasons)


def test_combined_retail_and_small_lot_unscaled():
    r = score(retail_call_share=0.70, small_lot_call_share=0.70)
    assert r.available is True
    assert r.score == 5.0
    assert r.coverage == 1.0


def test_prepared_remarks_high_uncertainty():
    r = score(prepared_remarks="uncertain uncertain uncertain")
    assert r.available is True
    assert r.score == -15.0
    assert any("high uncertainty" in x for x in r.reasons)


def test_tone_delta_qa_more_uncertain():
    r = score(prepared_remarks="the business is growing well.",
              qa_section="we may face uncertain uncertain uncertain conditions.")
    assert r.available is True
    assert r.score < 0
    assert any("more uncertain in Q&A" in x for x in r.reasons)
