"""Rule 1: EPS drivers — the estimate trend and its revisions. **Logged only** (0% weight, E6/#372).

The one input is the stored ``estimate_snapshots`` ``0q`` row (D14: yfinance's ``eps_trend`` and ``eps_revisions``
for the current quarter). Two things move the read:

- the 30-day trend of the estimate, ``(current - 30d ago) / max(|30d ago|, 0.10)`` — scaled so +5% is as strong as
  the term gets (the denominator floor keeps a near-zero estimate from exploding the ratio);
- the 30-day revision counts, ``up - down`` over ``up + down`` — analysts raising the number is the driver, and a
  single revision is a full-strength signal either way.

The score is audit-only: ``RULE_WEIGHTS["rule1_drivers"] == 0.0`` keeps it out of the composite, so a card's
direction, confidence and action are the same whether or not a snapshot exists. Missing inputs are ``None`` —
never a 0 that would read as "no change".
"""
from __future__ import annotations

import math
from typing import Optional

from RevenueChaser.module_earnings_radar.models import RuleResult

# The four fields the score needs; the other five only widen the coverage.
REQUIRED = ("eps_trend_current", "eps_trend_30d", "eps_rev_up_30d", "eps_rev_down_30d")
FIELDS = ("eps_trend_current", "eps_trend_7d", "eps_trend_30d", "eps_trend_60d", "eps_trend_90d",
          "eps_rev_up_7d", "eps_rev_up_30d", "eps_rev_down_7d", "eps_rev_down_30d")
TREND_KEYS = ("trend_7", "trend_30", "trend_60", "trend_90")

TREND_SCALE = 0.05        # a +5% 30-day move is the strongest the trend term reads
TREND_WEIGHT = 60.0
REVISION_WEIGHT = 40.0
MIN_ABS_TREND = 0.10      # the denominator floor, in dollars of EPS


def _num(value) -> Optional[float]:
    """A stored cell as a float, or None. None, NaN, infinity and unparseable text are all "missing"."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _count(value: float) -> str:
    """A revision count as text: '5', not '5.0' (yfinance serves whole numbers as floats)."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def score(row: dict | None) -> RuleResult:
    """One ``estimate_snapshots`` ``0q`` row (or None) -> the R1 read, logged only."""
    if not row:
        return RuleResult(score=None, available=False, coverage=0, reasons=["no estimate snapshot"])
    values = {k: _num(row.get(k)) for k in FIELDS}
    if any(values.get(k) is None for k in REQUIRED):
        return RuleResult(score=None, available=False, coverage=0, reasons=["estimate trend incomplete"])

    current = values["eps_trend_current"]
    trend = {label: (None if values[f"eps_trend_{label}d"] is None
                     else (current - values[f"eps_trend_{label}d"]) / max(abs(values[f"eps_trend_{label}d"]),
                                                                         MIN_ABS_TREND))
             for label in ("7", "30", "60", "90")}
    up_30, down_30 = values["eps_rev_up_30d"], values["eps_rev_down_30d"]
    net_30 = up_30 - down_30
    n_30 = up_30 + down_30
    raw = (TREND_WEIGHT * _clamp(trend["30"] / TREND_SCALE)
           + REVISION_WEIGHT * _clamp(net_30 / max(n_30, 1.0)))
    raw_metrics = {**values, **{f"trend_{label}": trend[label] for label in trend}}
    return RuleResult(
        score=round(raw, 1),
        available=True,
        coverage=sum(1 for v in values.values() if v is not None) / len(FIELDS),
        reasons=[f"EPS estimate {trend['30']:+.1%} over 30 days",
                 f"30-day revisions {_count(up_30)} up / {_count(down_30)} down"],
        flags=[],
        raw_metrics=raw_metrics,
    )
