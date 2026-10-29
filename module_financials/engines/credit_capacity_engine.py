"""Kredi Kapasitesi Engine — Borç/EBITDA, faiz karşılama, sektör eşik karşılaştırması."""
from __future__ import annotations

from ..config import to_float
from ._inputs import net_debt as _net_debt, not_for_a_bank
from RevenueChaser.core.providers.validator import validate_annual

# Genel eşikler (sektöre göre ayarlanabilir)
DEBT_EBITDA_SAFE = 3.0
DEBT_EBITDA_STRESSED = 4.0
INTEREST_COVERAGE_SAFE = 3.0
INTEREST_COVERAGE_STRESSED = 2.0


def run_credit_capacity(financials: dict, assumptions: dict | None = None) -> dict:
    """En son mali yıldan kaldıraç ve karşılama metriklerini hesaplar."""
    if (bank := not_for_a_bank(financials, "debt / EBITDA and interest-coverage grading")) is not None:
        return bank
    a = dict(assumptions or {})
    annual, warnings = validate_annual(financials.get("annual", []))
    if not annual:
        return {"error": "no financial data", "warnings": warnings}

    latest = annual[-1]
    ebitda = to_float(latest.get("ebitda"))
    ebit = to_float(latest.get("ebit"))
    debt = to_float(latest.get("total_debt"))
    cash = to_float(latest.get("cash"))
    interest = to_float(latest.get("interest_expense"))

    net_debt = _net_debt(debt, cash)

    def _ratio(numerator):
        """(value, state): 'ok', 'unknown' (an input is missing) or 'n/m' (EBITDA <= 0 with debt)."""
        if numerator is None or ebitda is None:
            return None, "unknown"
        if ebitda <= 0:
            # A loss-making company with debt is not "healthy": the ratio means nothing (n/m) and
            # the grade is stressed. With no debt there is nothing to cover (#173 M7, #255).
            return (0.0, "ok") if numerator == 0 else (None, "n/m")
        return numerator / ebitda, "ok"

    gross_leverage, gross_state = _ratio(debt)
    net_leverage, net_state = _ratio(net_debt)
    # Net leverage follows gross debt for a loss-maker: no debt means nothing is stressed, however
    # much cash there is (a cash-rich, debt-free biotech). The ratio is still n/m (#173 M1).
    net_grade_from_gross = ebitda is not None and ebitda <= 0 and debt == 0
    if net_grade_from_gross:
        net_leverage, net_state = None, "n/m"

    # Faiz karşılama: EBITDA/faiz öncelikli; EBIT/faiz fallback (yalnızca faiz > 0 ise anlamlı)
    coverage = None
    coverage_base = "EBITDA"
    coverage_state = "unknown"
    if interest is not None and interest > 0:
        if ebitda is not None and ebitda <= 0:
            coverage_state = "n/m"
        elif ebitda is not None:
            coverage, coverage_state = ebitda / interest, "ok"
        elif ebit is not None:
            coverage, coverage_state = ebit / interest, "ok"
            coverage_base = "EBIT"

    thresholds = dict(a.get("thresholds") or {
        "debt_ebitda_safe": DEBT_EBITDA_SAFE,
        "debt_ebitda_stressed": DEBT_EBITDA_STRESSED,
        "interest_coverage_safe": INTEREST_COVERAGE_SAFE,
        "interest_coverage_stressed": INTEREST_COVERAGE_STRESSED,
    })

    def _grade_leverage(v, state):
        if state == "n/m":
            return "stressed"
        if v is None:
            return "unknown"
        if v < thresholds["debt_ebitda_safe"]:
            return "healthy"
        if v < thresholds["debt_ebitda_stressed"]:
            return "moderate"
        return "stressed"

    def _grade_coverage(v, state):
        if state == "n/m":
            return "stressed"
        if v is None:
            return "unknown"
        if v > thresholds["interest_coverage_safe"]:
            return "healthy"
        if v > thresholds["interest_coverage_stressed"]:
            return "moderate"
        return "stressed"

    return {
        "ticker": financials.get("ticker"),
        "warnings": warnings,
        "fiscal_year": latest.get("fiscal_year"),
        "metrics": {
            "ebitda": ebitda,
            "total_debt": debt,
            "cash": cash,
            "net_debt": net_debt,
            "gross_debt_to_ebitda": gross_leverage,
            "gross_debt_to_ebitda_state": gross_state,
            "net_debt_to_ebitda": net_leverage,
            "net_debt_to_ebitda_state": net_state,
            "interest_coverage": coverage,
            "interest_coverage_state": coverage_state,
            "interest_coverage_base": coverage_base,
        },
        "grades": {
            "gross_leverage": _grade_leverage(gross_leverage, gross_state),
            "net_leverage": (_grade_leverage(gross_leverage, gross_state) if net_grade_from_gross
                             else _grade_leverage(net_leverage, net_state)),
            "interest_coverage": _grade_coverage(coverage, coverage_state),
        },
        "thresholds": thresholds,
    }
