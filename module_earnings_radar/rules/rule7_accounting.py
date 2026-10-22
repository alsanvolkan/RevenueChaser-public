"""Rule 7: Accounting quality — signed Sloan accruals.

Is reported earnings cash-backed, or booked ahead of cash? One live input:

    accruals = (net_income − cash_from_operations) / total_assets

Positive accruals (NI ≫ CFO) are the Sloan red flag and lean the direction
score down. Negative accruals (CFO ≫ NI) are cash-rich, not manipulation —
they get the same +20 as low positive accruals, never a penalty.

Honest inputs:
- all three of NI, CFO, TA required and TA ≠ 0, else unavailable (None, never 0);
- NI comes from the income statement and is cross-checked against the
  cash-flow statement's own ``net_income`` on the same ``fiscal_date_ending``.
  If both are present and disagree (a YTD income row joined to a quarterly CFO
  row, or vice versa) the quarter is a period mismatch → Sloan is NOT computed
  (unavailable), never forced;
- coverage is 1.0 when Sloan is present, else 0 — the score is not scaled;
- deferred revenue / RPO are **not** scored here (R3 SaaS already owns them).
"""
from RevenueChaser.core.config import SCORE_RANGE, R7_NI_MISMATCH_TOL as NI_MISMATCH_TOL
from RevenueChaser.module_earnings_radar.models import RuleResult


def _get(row, key, default=None):
    return row.get(key, default) if isinstance(row, dict) else default


def _ni_mismatch(ni, cash_ni) -> bool:
    if ni is None or cash_ni is None:
        return False
    denom = max(abs(ni), 1.0)
    return abs(ni - cash_ni) > NI_MISMATCH_TOL * denom


def score(fundamentals_history: list[dict]) -> RuleResult:
    if not fundamentals_history:
        return RuleResult(
            score=None, available=False, coverage=0,
            reasons=["no fundamental data"],
        )

    latest = fundamentals_history[0]
    ni = _get(latest, "net_income")
    cfo = _get(latest, "cash_from_operations")
    ta = _get(latest, "total_assets")

    if ni is None or cfo is None or ta is None or ta == 0:
        return RuleResult(
            score=None, available=False, coverage=0,
            reasons=["no Sloan inputs"],
        )

    if _ni_mismatch(ni, _get(latest, "cash_net_income")):
        return RuleResult(
            score=None, available=False, coverage=0,
            reasons=["Sloan period mismatch (IS net_income vs cash-flow net_income)"],
        )

    accruals = (ni - cfo) / ta
    reasons: list[str] = []
    flags: list[str] = []
    score_val = 0.0

    if accruals > 0.10:
        score_val = -30
        flags.append(f"high Sloan accruals ({accruals:.2%})")
        reasons.append(f"high Sloan accruals ({accruals:.2%})")
    elif accruals > 0.05:
        score_val = -10
        reasons.append(f"elevated accruals ({accruals:.2%})")
    elif accruals < -0.05:
        score_val = 20
        reasons.append(f"cash well above earnings ({accruals:.2%})")
    else:
        score_val = 20
        reasons.append(f"low accruals ({accruals:.2%})")

    score_val = max(SCORE_RANGE[0], min(SCORE_RANGE[1], score_val))
    return RuleResult(
        score=round(score_val, 1),
        available=True,
        coverage=1.0,
        reasons=reasons,
        flags=flags,
        raw_metrics={"accruals": round(accruals, 4)},
    )
