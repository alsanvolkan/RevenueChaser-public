"""Rule tunables are sourced from core.config and are patchable for replay."""
from RevenueChaser.core import config as cfg
from RevenueChaser.module_earnings_radar.rules import (
    rule2_guidance, rule6_macro, rule7_accounting, rule8_sentiment,
)


def test_rule_constants_come_from_config():
    assert rule8_sentiment.RETAIL_FADE == cfg.R8_RETAIL_FADE
    assert rule2_guidance.SURPRISE_CLIP == cfg.R2_SURPRISE_CLIP
    assert rule2_guidance.BEAT_MIN == cfg.R2_BEAT_MIN
    assert rule6_macro.RATE_VETO == cfg.R6_RATE_VETO
    assert rule7_accounting.NI_MISMATCH_TOL == cfg.R7_NI_MISMATCH_TOL


def test_tunables_registry_covers_rule_thresholds():
    for name in ("R8_RETAIL_FADE", "R8_SIGN", "R2_SURPRISE_CLIP", "R3_OM_MAG_DOWN_BEAR",
                 "R6_RATE_VETO", "R7_NI_MISMATCH_TOL", "R4_IV_RV_RICH"):
        assert name in cfg.TUNABLES


def test_r8_sign_is_patchable():
    # Default momentum: crowded retail -> positive.
    r = rule8_sentiment.score(retail_call_share=0.9)
    assert r.score == 20.0
    # Fade polarity (replay override): same input -> negative.
    rule8_sentiment.R8_SIGN = -1
    try:
        r = rule8_sentiment.score(retail_call_share=0.9)
        assert r.score == -20.0
    finally:
        rule8_sentiment.R8_SIGN = cfg.R8_SIGN
