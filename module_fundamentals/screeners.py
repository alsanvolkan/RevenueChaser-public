"""Fundamental Analysis screeners — pure, auditable gate evaluations.

Each screener returns a ``GateResult``:

    {"name": str, "passed": bool, "score": str, "known": int, "total": int,
     "rules": [{id, label, unit, passed (True / False / None = unknown), actual, threshold}]}

Consumes a precomputed ``metrics`` dict from the pipeline. ``passed`` means the
method fully clears its threshold (Rule of 40 ≥ 40, Quality 3/3, Momentum 3/3).
"""
from __future__ import annotations

from RevenueChaser.core import tristate as tri

from . import config


def _rule(id: str, label: str, unit: str, passed: bool | None, actual, threshold: str) -> dict:
    """``passed`` is True / False / None (unknown: an input is missing, #151); nothing is coerced."""
    return {"id": id, "label": label, "unit": unit, "passed": passed,
            "actual": actual, "threshold": threshold}


def _counted(name: str, rules: list[dict]) -> dict:
    """A gate that needs every rule: it passes only when every rule is known and passed (decision 8-A)."""
    passed, n_known, total = tri.tally(rules)
    return {"name": name, "passed": passed == total, "score": tri.score_text(passed, n_known, total),
            "known": n_known, "total": total, "rules": rules}


def _ro40_text(ro40) -> str:
    """One decimal, but never the line itself for a value under it: 39.96 shows 39.9, not 40 (DF86)."""
    if ro40 is None:
        return "N/A"
    shown = round(ro40, 1)
    if ro40 < config.RO40_MIN <= shown:
        shown = config.RO40_MIN - 0.1
    return f"{shown:g}"


def evaluate_rule_of_40(m: dict) -> dict:
    """Rule of 40 = YoY TTM revenue growth + TTM FCF margin (≥ 40): one test, one rule row (#148 U12).

    The row is True / False against the line, or None (unknown) when either input is missing. The two inputs
    stay on the card and in ``fundamentals_scores``; they are not pass/fail tests of their own.
    """
    ro40 = m.get("ro40")
    basis = m.get("ro40_basis")  # "TTM" (year + YTD - prior YTD) or "FY" (fiscal years): the row says which (#148)
    label = "Revenue growth + FCF margin" + (f" ({basis})" if basis else "")
    # #386 F2: an EBITDA basis means the free-cash-flow margin was unavailable and an EBITDA margin stood in. The
    # value is shown, but the rule is unknown and never decides (the gate cannot pass on it).
    passed = (None if basis and "EBITDA" in basis
              else tri.rule((ro40,), lambda r: r >= config.RO40_MIN))
    rules = [_rule("ro40", label, "pts", passed, ro40, "≥ 40")]
    return {
        "name": "Rule of 40",
        "passed": rules[0]["passed"] is True,
        "score": _ro40_text(ro40),
        "known": sum(1 for r in rules if r["passed"] is not None), "total": len(rules),
        "rules": rules,
    }


def _leverage_rule(nde, net_cash) -> dict:
    """Net Debt / EBITDA < 3×. Review N1: a firm with more cash than debt passes even when EBITDA ≤ 0 leaves the
    ratio undefined (shown as "Net cash"); net debt with EBITDA ≤ 0 stays unknown."""
    if nde is None and net_cash is True:
        return _rule("net_debt_ebitda", "Net Debt / EBITDA < 3×", "text", True, "Net cash", "< 3.0×")
    return _rule("net_debt_ebitda", "Net Debt / EBITDA < 3×", "ratio",
                 tri.rule((nde,), lambda n: n < config.ND_EBITDA_MAX), nde, "< 3.0×")


def evaluate_quality(m: dict) -> dict:
    """Quality (x/3): growth≥20% or ROIC≥15% · gross margin≥30% · ND/EBITDA<3×."""
    growth = m.get("rev_growth_ttm")
    roic = m.get("roic")
    gm = m.get("gross_margin")
    nde = m.get("net_debt_ebitda")

    growth_ok = tri.rule((growth,), lambda g: g >= config.GROWTH_MIN)
    roic_ok = tri.rule((roic,), lambda r: r >= config.ROIC_MIN)
    rules = [
        # One missing half cannot fail the rule, and cannot decide it unless the other half already passed.
        _rule("growth_or_roic", "Growth ≥20% or ROIC ≥15%", "pct",
              tri.either(growth_ok, roic_ok), growth if growth_ok else roic, "≥20% / ≥15%"),
        _rule("gross_margin", "Gross margin ≥30%", "pct",
              tri.rule((gm,), lambda g: g >= config.GM_MIN), gm, "≥ 30%"),
        _leverage_rule(nde, m.get("net_cash")),
    ]
    return _counted("Quality", rules)


def evaluate_momentum(m: dict) -> dict:
    """Momentum (x/3): near 52w high · 6-month RS vs SPY > 0 · above 200d MA."""
    dist = m.get("dist_52w_pct")
    rs6 = m.get("rs_6m")
    above = m.get("above_200d")
    rules = [
        _rule("near_52w_high", "Within 25% of 52-week high", "pct_high",
              tri.rule((dist,), lambda d: d >= (1 - config.DIST_52W_MAX)), dist, "≥ 75% of high"),
        _rule("rs_6m", "6-month RS vs SPY positive", "pp",
              tri.rule((rs6,), lambda r: r > config.RS_6M_MIN), rs6, "> 0 pp"),
        _rule("above_200d", "Price above 200-day MA", "bool",
              tri.rule((above,), lambda a: a is True), above, "> 200d MA"),
    ]
    return _counted("Momentum", rules)


def is_distorted(m: dict) -> bool:
    """[DISTORTED] guardrail: absurd growth or a micro revenue base."""
    growth = m.get("rev_growth_ttm")
    rev = m.get("ttm_revenue")
    if growth is not None and abs(growth) > config.DISTORTED_GROWTH:
        return True
    if rev is not None and rev < config.DISTORTED_REV_MIN:
        return True
    return False
