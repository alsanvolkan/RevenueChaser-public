"""Rule 6: Macro veto — 0% composite weight, never a direction vote.

Answers one question for ``select_action``: should this event be blocked
(``NO_TRADE``) because the name is extremely rate-sensitive? A high
``|beta|`` vs the 10Y yield means duration risk; only the extreme bucket
sets a veto flag. The score is audit-only (like R4's vol-edge).

Live input (wired):
- OLS beta of ticker daily returns vs Δ 10Y yield (FRED DGS10 in decimal,
  so beta ≈ equity duration). ``|beta| > 2`` is a reason; ``|beta| > 4``
  is a veto. Fewer than 20 paired observations, or zero yield variance,
  is unavailable — never a 0 fill.

Not wired (optional kwargs, pipeline does not pass them):
- FX tailwind / FX exposure, regulatory / geopolitical text, consensus
  crowding. ``regulatory_risk="low"`` is not a signal.
"""
from typing import Optional

from RevenueChaser.core.config import (
    SCORE_RANGE,
    R6_MIN_OBS as MIN_OBS,
    R6_RATE_HIGH as RATE_HIGH,
    R6_RATE_VETO as RATE_VETO,
)
from RevenueChaser.core.models.events import RuleResult


def yield_beta(
    stock_by_date: dict[str, float],
    yield_by_date: dict[str, float],
    min_obs: int = MIN_OBS,
) -> Optional[float]:
    """OLS beta of stock simple returns vs Δ 10Y yield (decimal).

    FRED DGS10 arrives in percent (4.25); divide by 100 so a duration-10
    name prints near −10. Common dates only. ``None`` when the window is
    too short or yield did not move.
    """
    if not stock_by_date or not yield_by_date:
        return None
    dates = sorted(set(stock_by_date) & set(yield_by_date))
    if len(dates) < min_obs + 1:
        return None
    r, dy = [], []
    for i in range(1, len(dates)):
        p0, p1 = stock_by_date[dates[i - 1]], stock_by_date[dates[i]]
        y0, y1 = yield_by_date[dates[i - 1]], yield_by_date[dates[i]]
        if p0 in (None, 0) or p1 is None or y0 is None or y1 is None:
            continue
        r.append((p1 - p0) / p0)
        dy.append((y1 - y0) / 100.0)
    if len(r) < min_obs:
        return None
    n = len(r)
    mean_r = sum(r) / n
    mean_dy = sum(dy) / n
    var_dy = sum((x - mean_dy) ** 2 for x in dy)
    if var_dy == 0:
        return None
    cov = sum((ri - mean_r) * (di - mean_dy) for ri, di in zip(r, dy))
    return cov / var_dy


def _regulatory_is_severe(text: str) -> bool:
    low = text.lower()
    return "high" in low or "sanction" in low


def score(
    rate_sensitivity: Optional[float] = None,
    rate_event_overlap: Optional[bool] = None,
    beta_tstat: Optional[float] = None,
    beta_to_10y_real_yields: Optional[float] = None,
    constant_currency_growth: Optional[float] = None,
    reported_growth: Optional[float] = None,
    consensus_long: Optional[bool] = None,
    regulatory_risk: Optional[str] = None,
    geopolitical_risk: Optional[str] = None,
    fx_exposure: Optional[float] = None,
) -> RuleResult:
    score_val = 0.0
    reasons: list[str] = []
    flags: list[str] = []
    raw_metrics: dict = {}
    live = False

    if rate_sensitivity is not None:
        live = True
        raw_metrics["rate_sensitivity"] = round(float(rate_sensitivity), 3)
        mag = abs(rate_sensitivity)
        # Badge (reason) on |beta| > 4; NO_TRADE only when a rate event
        # (FOMC/CPI) overlaps the earnings window ±1 trading day. A bare high
        # duration is a sector trait, not an earnings veto (design doc).
        if mag > RATE_VETO:
            reasons.append(f"extreme rate sensitivity ({rate_sensitivity:.1f}x)")
            raw_metrics["rate_badge"] = True
            noisy = beta_tstat is not None and abs(beta_tstat) < 2.0
            raw_metrics["noisy_beta"] = noisy
            if rate_event_overlap and not noisy:
                score_val -= 15
                flags.append("extreme rate sensitivity")
                reasons.append("rate event (FOMC/CPI) overlaps earnings — macro veto")
        elif mag > RATE_HIGH:
            score_val -= 15
            reasons.append(f"high rate sensitivity ({rate_sensitivity:.1f}x)")
        if rate_event_overlap and mag <= RATE_HIGH:
            reasons.append("rate event overlap but duration not extreme — no veto")

    if beta_to_10y_real_yields is not None:
        live = True
        raw_metrics["beta_to_10y_real_yields"] = round(float(beta_to_10y_real_yields), 3)
        if abs(beta_to_10y_real_yields) > 0.5:
            score_val -= 10
            reasons.append("strong yield beta")

    if constant_currency_growth is not None and reported_growth is not None:
        live = True
        fx_tailwind = reported_growth - constant_currency_growth
        raw_metrics["fx_tailwind"] = round(fx_tailwind, 4)
        if fx_tailwind > 0.02:
            score_val -= 10
            reasons.append("FX tailwind inflating reported growth")

    if consensus_long is True:
        live = True
        score_val -= 15
        reasons.append("consensus crowded positioning")

    if regulatory_risk and _regulatory_is_severe(regulatory_risk):
        live = True
        score_val -= 20
        reasons.append(f"regulatory risk: {regulatory_risk}")
        flags.append(f"regulatory: {regulatory_risk}")

    if geopolitical_risk:
        text = str(geopolitical_risk).strip()
        if text and text.lower() not in ("low", "none", "n/a"):
            live = True
            score_val -= 20
            reasons.append(f"geopolitical risk: {text}")

    if fx_exposure is not None and abs(fx_exposure) > 0.25:
        live = True
        score_val -= 10
        reasons.append(f"significant FX exposure ({fx_exposure:.0%})")

    if not live:
        return RuleResult(
            score=None,
            available=False,
            coverage=0,
            reasons=["no macro signal available"],
            flags=[],
            raw_metrics=raw_metrics,
        )

    score_val = max(SCORE_RANGE[0], min(SCORE_RANGE[1], score_val))
    return RuleResult(
        score=round(score_val, 1),
        available=True,
        coverage=1.0,
        reasons=reasons if reasons else ["no macro concerns"],
        flags=flags,
        raw_metrics=raw_metrics,
    )
