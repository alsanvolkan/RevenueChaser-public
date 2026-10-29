"""Rule 5: Positioning & short squeeze.

Who is positioned how, and what does that imply for the stock's reaction to
earnings? Two independent components, at least one of which must be present or
the rule is unavailable (never a zero fill):

1. **Institutional lean** — from UW flow-recent, by the print's side (docs/rules/redesign.md R5, review S4):
   bullish = ask-side calls + bid-side puts, bearish = bid-side calls + ask-side puts, mid dropped, institutional
   = size >= 10 AND premium >= $10k (``aggregate_flow``). The net is scaled by the sided premium (a one-sided
   but small imbalance scores less than a clean sweep); a net under ``INST_FLOW_MIN_NET`` is Thin (present, no
   lean); no side served is no lean; a material multi-leg package is left out and tagged ``package-risk``.
2. **Squeeze tag** — ``none`` / ``fuel`` from short interest as % of float or the borrow fee (redesign.md R5, #256):
   no Z ladder, no score, and low short interest is not bearish. Since G9 the inputs are the ownership store's
   (``stored_inputs.short_rows``): FINRA's shares short and days to cover, IBKR's borrow fee and shares available
   as a count. The store has no float, so today the tag reads the borrow fee alone; FINRA's shares, days to cover
   and the shares available ride as raw metrics. No utilization is made up (review Part B).

Honest inputs:
- missing fields are ``None``, never 0, so a missing borrow fee cannot silently
  read as "cheap to borrow"; with neither tag field the squeeze is missing, not ``none``;
- retail-call euphoria is NOT scored here — that fade lives in R8 (sentiment).
  Scoring the same 65%+ retail-call share in both R5 and R8 double-faded it.
"""
from typing import Optional

from RevenueChaser.core.config import INST_FLOW_MIN_NET, SCORE_RANGE
from RevenueChaser.module_earnings_radar.models import RuleResult

# The squeeze tag's lines. Default chosen, change if avo prefers: 20% of float is the short interest the old
# absolute fallback called high; a 10% annual borrow fee is far above the general-collateral rate (well under 1%).
FUEL_SI_PCT = 20.0
FUEL_FEE_PCT = 10.0


def score(
    short_interest_pct_float: Optional[float] = None,
    days_to_cover: Optional[float] = None,
    borrow_fee: Optional[float] = None,
    short_interest: Optional[float] = None,
    shares_available: Optional[float] = None,
    institutional_net_delta: Optional[float] = None,
    institutional_call_premium: Optional[float] = None,
    institutional_put_premium: Optional[float] = None,
    institutional_side_served: Optional[bool] = None,
    institutional_package_premium: Optional[float] = None,
) -> RuleResult:
    """``institutional_net_delta`` is the lean net (bullish minus bearish side premium) and the call/put premiums
    the bullish and bearish side premium. ``institutional_side_served`` False says the tape carried no print side,
    so there is no lean (the flow component is absent, never neutral). ``short_interest`` (FINRA's shares short),
    ``days_to_cover`` and ``shares_available`` (IBKR's count) are shown, never scored."""
    available = False
    score_val = 0.0
    reasons: list[str] = []
    raw_metrics: dict = {}
    signals_present = 0
    signal_slots = 2  # flow + squeeze

    # ── Component 1: institutional lean by print side ──
    flow_signal = False
    if institutional_side_served is False:
        reasons.append("no print side: institutional lean unavailable")
        institutional_net_delta = None
    if institutional_net_delta is not None and institutional_net_delta != 0 \
            and abs(institutional_net_delta) < INST_FLOW_MIN_NET:
        # Thin is present (a real but small tape), not missing, and is no lean (redesign.md R5).
        reasons.append("thin institutional lean")
        raw_metrics["inst_net"] = round(float(institutional_net_delta), 2)
        flow_signal = True
        signals_present += 1
    elif institutional_net_delta is not None and institutional_net_delta != 0:
        ic = float(institutional_call_premium or 0)
        ip = float(institutional_put_premium or 0)
        total_inst = ic + ip
        if total_inst > 0:
            imbalance = institutional_net_delta / total_inst  # -1..+1
            score_val += 20 * imbalance
            raw_metrics["inst_flow_imbalance"] = round(imbalance, 3)
            if imbalance > 0:
                reasons.append("institutional bullish lean")
            else:
                reasons.append("institutional bearish lean")
        else:
            score_val += 20 if institutional_net_delta > 0 else -20
            if institutional_net_delta > 0:
                reasons.append("institutional bullish lean")
            else:
                reasons.append("institutional bearish lean")
        raw_metrics["inst_net"] = round(float(institutional_net_delta), 2)
        flow_signal = True
        signals_present += 1
    if institutional_package_premium is not None and institutional_package_premium >= INST_FLOW_MIN_NET:
        reasons.append("package-risk: multi-leg premium left out of the lean")
        raw_metrics["inst_package"] = round(float(institutional_package_premium), 2)

    # ── Component 2: the squeeze tag (none / fuel), present when one of its fields is ──
    for key, value in (("si_pct", short_interest_pct_float), ("borrow_fee", borrow_fee),
                       ("days_to_cover", days_to_cover), ("short_interest", short_interest),
                       ("shares_available", shares_available)):
        if value is not None:
            raw_metrics[key] = round(float(value), 2)
    if short_interest_pct_float is not None or borrow_fee is not None:
        signals_present += 1
        fuel = []
        if short_interest_pct_float is not None and short_interest_pct_float >= FUEL_SI_PCT:
            fuel.append(f"short interest {short_interest_pct_float:.1f}% of float")
        if borrow_fee is not None and borrow_fee >= FUEL_FEE_PCT:
            fuel.append(f"borrow fee {borrow_fee:.1f}%")
        raw_metrics["squeeze"] = "fuel" if fuel else "none"
        reasons.append(f"squeeze fuel ({', '.join(fuel)})" if fuel else "squeeze: none")

    available = signals_present > 0
    if not available:
        return RuleResult(
            score=None,
            available=False,
            coverage=0,
            reasons=reasons + ["no positioning signal available"],
            raw_metrics=raw_metrics,
        )

    score_val = max(SCORE_RANGE[0], min(SCORE_RANGE[1], score_val))
    coverage = min(signals_present / signal_slots, 1.0)

    return RuleResult(
        score=round(score_val, 1),
        available=available,
        coverage=coverage,
        reasons=reasons if reasons else ["neutral positioning"],
        raw_metrics=raw_metrics,
    )
