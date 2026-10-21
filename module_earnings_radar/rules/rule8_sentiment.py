"""Rule 8: Sentiment — retail call share (Phase 1).

Live input (wired): share of call premium from size&lt;10 prints
(``retail_call / (retail_call + inst_call)``). Same size split as R5's
``aggregate_flow``. ``> 65%`` is retail call **momentum** (+20; the polarity is
the tunable ``R8_SIGN``, default +1). The score is **not** scaled by coverage.

Phase 2 (transcripts / LM tone) and small-lot share stay as optional kwargs.
The pipeline does not pass them. Small-lot would re-count the same size&lt;10
prints already in retail share.

No call premium → unavailable (``score=None``, never 0). Coverage is 1.0
when any slot is present, else 0.
"""
import re
from typing import Optional

from RevenueChaser.core.config import SCORE_RANGE, R8_RETAIL_FADE, R8_SIGN
from RevenueChaser.core.models.events import RuleResult
from .lm_dictionary import (
    LM_CONSTRAINING,
    LM_LITIGIOUS,
    LM_NEGATIVE,
    LM_POSITIVE,
    LM_UNCERTAINTY,
)

RETAIL_FADE = R8_RETAIL_FADE


def score(
    prepared_remarks: Optional[str] = None,
    qa_section: Optional[str] = None,
    prev_uncertainty_prepared: Optional[float] = None,
    prev_uncertainty_qa: Optional[float] = None,
    retail_call_share: Optional[float] = None,
    small_lot_call_share: Optional[float] = None,
) -> RuleResult:
    score_val = 0.0
    reasons: list[str] = []
    raw_metrics: dict = {}
    live = False

    if prepared_remarks is not None and qa_section is not None:
        live = True
        score_val, reasons, raw_metrics = _score_transcripts(
            prepared_remarks, qa_section,
            prev_uncertainty_prepared, prev_uncertainty_qa,
        )
    elif prepared_remarks is not None:
        live = True
        prep_words = _tokenize(prepared_remarks)
        prep_len = max(len(prep_words), 1)
        prepared_uncertainty = sum(1 for w in prep_words if w in LM_UNCERTAINTY) / prep_len
        raw_metrics["prepared_uncertainty"] = round(prepared_uncertainty, 4)
        if prepared_uncertainty > 0.03:
            score_val -= 15
            reasons.append(f"high uncertainty in prepared remarks ({prepared_uncertainty:.4f})")

    if retail_call_share is not None:
        live = True
        raw_metrics["retail_call_share"] = retail_call_share
        if retail_call_share > RETAIL_FADE:
            score_val += R8_SIGN * 20
            reasons.append(f"extreme retail call buying ({retail_call_share:.0%})")

    if small_lot_call_share is not None:
        live = True
        raw_metrics["small_lot_call_share"] = small_lot_call_share
        if small_lot_call_share > RETAIL_FADE:
            score_val -= 15
            reasons.append("small-lot call euphoria")

    if not live:
        return RuleResult(
            score=None, available=False, coverage=0,
            reasons=["no sentiment data available"],
        )

    score_val = max(SCORE_RANGE[0], min(SCORE_RANGE[1], score_val))
    return RuleResult(
        score=round(score_val, 1),
        available=True,
        coverage=1.0,
        reasons=reasons if reasons else ["retail share not extreme"],
        raw_metrics=raw_metrics,
    )


def _score_transcripts(prepared_remarks, qa_section,
                       prev_uncertainty_prepared, prev_uncertainty_qa):
    score_val = 0.0
    reasons: list[str] = []
    prep_words = _tokenize(prepared_remarks)
    qa_words = _tokenize(qa_section)
    prep_len = max(len(prep_words), 1)
    qa_len = max(len(qa_words), 1)

    prepared_uncertainty = sum(1 for w in prep_words if w in LM_UNCERTAINTY) / prep_len
    qa_uncertainty = sum(1 for w in qa_words if w in LM_UNCERTAINTY) / qa_len
    prepared_negativity = sum(1 for w in prep_words if w in LM_NEGATIVE) / prep_len
    qa_negativity = sum(1 for w in qa_words if w in LM_NEGATIVE) / qa_len
    prepared_positivity = sum(1 for w in prep_words if w in LM_POSITIVE) / prep_len
    prepared_litigious = sum(1 for w in prep_words if w in LM_LITIGIOUS) / prep_len
    qa_litigious = sum(1 for w in qa_words if w in LM_LITIGIOUS) / qa_len
    prepared_constraining = sum(1 for w in prep_words if w in LM_CONSTRAINING) / prep_len

    raw_metrics = {
        "prepared_uncertainty": round(prepared_uncertainty, 4),
        "qa_uncertainty": round(qa_uncertainty, 4),
        "prepared_negativity": round(prepared_negativity, 4),
        "qa_negativity": round(qa_negativity, 4),
        "prepared_positivity": round(prepared_positivity, 4),
        "prepared_litigious": round(prepared_litigious, 4),
        "qa_litigious": round(qa_litigious, 4),
        "prepared_constraining": round(prepared_constraining, 4),
    }
    tone_delta = qa_uncertainty - prepared_uncertainty
    raw_metrics["tone_delta"] = round(tone_delta, 4)

    if tone_delta > 0.01:
        score_val -= 15
        reasons.append(f"management more uncertain in Q&A (delta={tone_delta:.4f})")
    elif tone_delta < -0.005:
        score_val += 10
        reasons.append("management more confident in Q&A")
    if qa_negativity > prepared_negativity * 1.5:
        score_val -= 10
        reasons.append("negative language increased in Q&A")
    if prepared_positivity > 0.02:
        score_val += 10
        reasons.append("positive language in prepared remarks")
    if prepared_litigious > 0.01 or qa_litigious > 0.01:
        score_val -= 10
        reasons.append("elevated legal/regulatory language")
    if prepared_constraining > 0.02:
        score_val -= 5
        reasons.append("highly constraining/restrictive language")
    if prev_uncertainty_prepared is not None and prev_uncertainty_qa is not None:
        prev_tone_delta = prev_uncertainty_qa - prev_uncertainty_prepared
        delta_change = tone_delta - prev_tone_delta
        raw_metrics["tone_delta_qoq"] = round(delta_change, 4)
        if delta_change > 0.01:
            score_val -= 10
            reasons.append("tone worsening QoQ")
        elif delta_change < -0.01:
            score_val += 10
            reasons.append("tone improving QoQ")
    return score_val, reasons, raw_metrics


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())
