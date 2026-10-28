"""Rule 5: Positioning & short squeeze.

Who is positioned how, and what does that imply for the stock's reaction to
earnings? Two independent components, at least one of which must be present or
the rule is unavailable (never a zero fill):

1. **Institutional lean** — from UW flow-recent, by the print's side (docs/rules/redesign.md R5, review S4):
   bullish = ask-side calls + bid-side puts, bearish = bid-side calls + ask-side puts, mid dropped, institutional
   = size >= 10 AND premium >= $10k (``aggregate_flow``). The net is scaled by the sided premium (a one-sided
   but small imbalance scores less than a clean sweep); a net under ``INST_FLOW_MIN_NET`` is Thin (present, no
   lean); no side served is no lean; a material multi-leg package is left out and tagged ``package-risk``.
2. **Squeeze potential** — short interest / borrow / utilization / days-to-cover
   as peer-relative Z-scores *within the ticker's sector* (the Morningstar name the spine stores; borrow
   fees are never compared across sectors). The name itself is left out of its own peer group (review N4).
   When fewer than two other in-sector peers exist for short interest, an absolute short-interest fallback
   applies instead.

Honest inputs:
- missing fields are ``None``, never 0, so a missing borrow fee cannot silently
  read as "cheap to borrow";
- an empty / sub-2-peer peer group does NOT mark the rule available and does NOT
  set a neutral score;
- Z weights renormalize over whatever metrics are actually present;
- retail-call euphoria is NOT scored here — that fade lives in R8 (sentiment).
  Scoring the same 65%+ retail-call share in both R5 and R8 double-faded it.
"""
import numpy as np
from typing import Optional

from RevenueChaser.core.config import INST_FLOW_MIN_NET, SCORE_RANGE
from RevenueChaser.module_earnings_radar.models import RuleResult

PEER_STATS = {}
# The ticker of each value in PEER_STATS, in the same order, so a name's Z leaves its own value out (review N4).
PEER_OWNERS = {}

# Squeeze metric weights (renormalized over whatever is present).
_SQ_WEIGHTS = {"si": 0.4, "borrow": 0.3, "util": 0.2, "dtc": 0.1}


def reset_peer_stats() -> None:
    PEER_STATS.clear()
    PEER_OWNERS.clear()


def register_peer_stats(ticker: str, sector: str, stats: dict) -> None:
    """Record a peer's squeeze metrics for its sector (used for Z-scoring).

    ``stats`` uses the same keys as the scorer's squeeze arguments:
    ``si_pct`` / ``borrow_fee`` / ``utilization`` / ``days_to_cover``.
    """
    if sector not in PEER_STATS:
        PEER_STATS[sector] = {"si": [], "borrow": [], "util": [], "dtc": []}
        PEER_OWNERS[sector] = {"si": [], "borrow": [], "util": [], "dtc": []}
    mapping = {
        "si_pct": "si",
        "borrow_fee": "borrow",
        "utilization": "util",
        "days_to_cover": "dtc",
    }
    for src, dst in mapping.items():
        if stats.get(src) is not None:
            PEER_STATS[sector][dst].append(stats[src])
            PEER_OWNERS[sector][dst].append(ticker)


def _peers(sector: str, metric: str, ticker: str) -> list[float]:
    """The sector's values of ``metric`` without the name's own (review N4: self + one peer is always +/-0.707)."""
    owners = PEER_OWNERS.get(sector, {}).get(metric, [])
    return [v for v, owner in zip(PEER_STATS[sector][metric], owners) if not ticker or owner != ticker]


def _z_peer(value: Optional[float], values: list[float]) -> Optional[float]:
    if value is None or len(values) < 2:
        return None
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return None
    mu = np.mean(clean)
    std = np.std(clean, ddof=1) if len(clean) > 1 else 1
    if std == 0:
        return 0.0
    return (value - mu) / std


def score(
    short_interest_pct_float: Optional[float] = None,
    days_to_cover: Optional[float] = None,
    borrow_fee: Optional[float] = None,
    utilization: Optional[float] = None,
    sector: str = "",
    institutional_net_delta: Optional[float] = None,
    institutional_call_premium: Optional[float] = None,
    institutional_put_premium: Optional[float] = None,
    ticker: str = "",
    institutional_side_served: Optional[bool] = None,
    institutional_package_premium: Optional[float] = None,
) -> RuleResult:
    """``institutional_net_delta`` is the lean net (bullish minus bearish side premium) and the call/put premiums
    the bullish and bearish side premium. ``institutional_side_served`` False says the tape carried no print side,
    so there is no lean (the flow component is absent, never neutral). ``ticker`` leaves the name out of its own
    peer group."""
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

    # ── Component 2: squeeze potential (peer Z within the sector) ──
    squeeze_signal = False
    z_by_metric = {}
    if sector and sector in PEER_STATS:
        z_by_metric["si"] = _z_peer(short_interest_pct_float, _peers(sector, "si", ticker))
        z_by_metric["borrow"] = _z_peer(borrow_fee, _peers(sector, "borrow", ticker))
        z_by_metric["util"] = _z_peer(utilization, _peers(sector, "util", ticker))
        z_by_metric["dtc"] = _z_peer(days_to_cover, _peers(sector, "dtc", ticker))
        for metric, z in z_by_metric.items():
            if z is not None:
                raw_metrics[f"z_{metric}"] = round(z, 2)

        present = {m: z for m, z in z_by_metric.items() if z is not None}
        if present:
            squeeze_signal = True
            signals_present += 1
            denom = sum(_SQ_WEIGHTS[m] for m in present)
            squeeze_z = sum(z * _SQ_WEIGHTS[m] for m, z in present.items()) / denom
            raw_metrics["squeeze_z_score"] = round(squeeze_z, 2)
            if squeeze_z > 3:
                score_val += 40
                reasons.append(f"extreme squeeze potential (peer Z={squeeze_z:.1f})")
            elif squeeze_z > 2:
                score_val += 25
                reasons.append(f"elevated squeeze potential (peer Z={squeeze_z:.1f})")
            elif squeeze_z > 1:
                score_val += 15
                reasons.append(f"mild squeeze risk (peer Z={squeeze_z:.1f})")
            elif squeeze_z < -1:
                score_val -= 10
                reasons.append("no squeeze pressure")

    # Absolute short-interest fallback when no in-sector peer Z could be built.
    if not squeeze_signal and short_interest_pct_float is not None:
        raw_metrics["si_pct"] = round(short_interest_pct_float, 2)
        if short_interest_pct_float > 20:
            squeeze_signal = True
            signals_present += 1
            score_val += 15
            reasons.append(f"high short interest ({short_interest_pct_float:.1f}%)")
        elif short_interest_pct_float > 10:
            squeeze_signal = True
            signals_present += 1
            score_val += 5
            reasons.append(f"moderate short interest ({short_interest_pct_float:.1f}%)")

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
