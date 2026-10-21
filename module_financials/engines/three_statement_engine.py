"""Üç Tablo Modeli Engine — IS/BS/CF projeksiyonu + borç takvimi + balance check.

Model yapısı gereği birebir dengelenir (ΔNakit = CFO + CFI + CFF):
  ΔCash + ΔAR + ΔInventory + ΔPP&E  =  ΔAP + ΔDebt + ΔEquity

Nakit açığı "revolver" (plug borç) ile kapatılır; fazla nakit revolver'ı azaltır.
"""
from __future__ import annotations

from ..config import DEFAULT_EBITDA_MARGIN, DEFAULT_REVENUE_GROWTH, DEFAULT_TAX_RATE, PROJECTION_YEARS, to_float
from RevenueChaser.core.providers.validator import validate_annual

from ._inputs import DNA_ASSUMED_ZERO, pick


def _get(rec: dict, key: str, default: float | None = None) -> float | None:
    v = to_float(rec.get(key))
    return v if v is not None else default


def run_three_statement(financials: dict, assumptions: dict | None = None) -> dict:
    """Üç tablo projeksiyonu çalıştırır."""
    a = dict(assumptions or {})
    annual, warnings = validate_annual(financials.get("annual", []))
    if not annual:
        return {"error": "insufficient financial data", "warnings": warnings}

    latest = annual[-1]
    rev0 = _get(latest, "revenue")
    if not rev0:
        return {"error": "no revenue in latest year", "warnings": warnings}

    n = int(pick(a, "projection_years", PROJECTION_YEARS))
    if n < 1:
        return {"error": "projection_years must be at least 1", "warnings": warnings}
    growth = list(a.get("revenue_growth") or DEFAULT_REVENUE_GROWTH)[:n]
    if len(growth) < n:
        warnings.append(f"revenue growth given for {len(growth)} years, last rate held for "
                        f"the other {n - len(growth)} of {n}")
        growth += [growth[-1]] * (n - len(growth))
    # The user's rate beats the statements' (a typed 0 is a 0), then history, then the default (#173 M19).
    tax_rate = pick(a, "tax_rate", _get(latest, "tax_rate", DEFAULT_TAX_RATE))
    tax_rate_source = ("assumption" if to_float(a.get("tax_rate")) is not None
                       else "statements" if _get(latest, "tax_rate") is not None else "default")   # FIN-9 B

    # The opening balance sheet needs debt and equity: unknown is not zero.
    missing = [k for k in ("total_debt", "total_equity") if _get(latest, k) is None]
    if missing:
        return {"error": f"missing {', '.join(missing)} in the latest year: opening balance sheet unknown",
                "warnings": warnings}

    # Marj ve oranlar (tarihselden türet, varsayım override'ına izin ver)
    ebitda0 = _get(latest, "ebitda")
    ebitda_margin = pick(a, "ebitda_margin", ebitda0 / rev0 if ebitda0 is not None else DEFAULT_EBITDA_MARGIN)
    dep0 = _get(latest, "depreciation")
    dep_pct = pick(a, "depreciation_pct_rev", dep0 / rev0 if dep0 is not None else 0.0)
    if dep0 is None and to_float(a.get("depreciation_pct_rev")) is None:
        warnings.append(DNA_ASSUMED_ZERO)                 # FIN-2 / DF41
    capex0 = _get(latest, "capex")
    capex_pct = pick(a, "capex_pct_rev", capex0 / rev0 if capex0 is not None else 0.05)
    interest_rate = pick(a, "interest_rate", 0.05)
    payout = pick(a, "payout_ratio", 0.0)
    min_cash_pct = pick(a, "min_cash_pct_rev", 0.02)

    # İşletme günleri (varsayım yoksa tarihselden)
    ar0, inv0, ap0 = _get(latest, "receivables"), _get(latest, "inventory"), _get(latest, "payables")
    dso = pick(a, "dso", (ar0 / rev0 * 365) if ar0 is not None else 45.0)
    dio = pick(a, "dio", (inv0 / rev0 * 365) if inv0 is not None else 60.0)
    dpo = pick(a, "dpo", (ap0 / rev0 * 365) if ap0 is not None else 45.0)

    # Başlangıç bilanço
    cash = _get(latest, "cash", min_cash_pct * rev0)       # a reported 0 stays 0; missing takes the minimum
    debt = _get(latest, "total_debt")
    equity = _get(latest, "total_equity")
    ar = ar0 if ar0 is not None else rev0 * dso / 365
    inv = inv0 if inv0 is not None else rev0 * dio / 365
    ap = ap0 if ap0 is not None else rev0 * dpo / 365
    ta0 = _get(latest, "total_assets")
    ca0 = _get(latest, "current_assets")
    ppe = (ta0 - ca0) if (ta0 is not None and ca0 is not None) else (ta0 - (cash + ar + inv)) if ta0 is not None else 0.0

    # Yapısal "diğer" kalem plug'i: modele dahil edilmeyen hesapları emer ve
    # başlangıç bilançosunu dengeli kılar (projeksiyon boyunca sabit).
    modeled_assets0 = cash + ar + inv + ppe
    modeled_liab_eq0 = ap + debt + equity
    other_assets = modeled_liab_eq0 - modeled_assets0

    # Mevcut borcun eşit amortismanı (projeksiyon boyunca)
    annual_repay = debt / n if debt > 0 else 0.0
    revolver = 0.0

    rev_prev = rev0
    proj = []

    for i in range(n):
        year = i + 1
        revenue = rev_prev * (1 + growth[i])

        ebitda = revenue * ebitda_margin
        dna = revenue * dep_pct
        ebit = ebitda - dna
        interest = interest_rate * (debt + revolver)
        pretax = ebit - interest
        tax = max(0.0, pretax) * tax_rate
        net_income = pretax - tax
        dividends = max(0.0, net_income) * payout

        capex = revenue * capex_pct

        ar_new = revenue * dso / 365
        inv_new = revenue * dio / 365
        ap_new = revenue * dpo / 365
        d_nwc = (ar_new - ar) + (inv_new - inv) - (ap_new - ap)

        cfo = net_income + dna - d_nwc
        cfi = -capex

        # CFF: amortisman + temettü + revolver (plug)
        cff = -annual_repay - dividends
        cash_end = cash + cfo + cfi + cff

        # Plug: nakit < min_cash ise revolver çek, fazlaysa öde
        min_cash = min_cash_pct * revenue
        if cash_end < min_cash:
            draw = min_cash - cash_end
            revolver += draw
            cash_end = min_cash
        else:
            repay = min(revolver, cash_end - min_cash)
            revolver -= repay
            cash_end -= repay

        ppe += capex - dna

        debt_end = debt - annual_repay + revolver
        equity_end = equity + net_income - dividends

        total_assets = cash_end + ar_new + inv_new + ppe + other_assets
        total_liab_eq = ap_new + debt_end + equity_end
        residual = total_assets - total_liab_eq

        proj.append({
            "year": year,
            "revenue": revenue,
            "growth": growth[i],
            "ebitda": ebitda,
            "dna": dna,
            "ebit": ebit,
            "interest": interest,
            "pretax": pretax,
            "tax": tax,
            "net_income": net_income,
            "dividends": dividends,
            "cfo": cfo,
            "capex": capex,
            "cfi": cfi,
            "cff": cff,
            "revolver_draw": revolver,
            "cash": cash_end,
            "ar": ar_new,
            "inventory": inv_new,
            "ap": ap_new,
            "ppe": ppe,
            "other_assets": other_assets,
            "total_debt": debt_end,
            "equity": equity_end,
            "total_assets": total_assets,
            "total_liab_eq": total_liab_eq,
            "balance_residual": residual,
        })

        rev_prev = revenue
        cash = cash_end
        debt = debt_end - revolver  # vadeli borç (revolver hariç)
        equity = equity_end
        ar, inv, ap = ar_new, inv_new, ap_new

    balanced = all(abs(p["balance_residual"]) < 1.0 for p in proj)
    max_residual = max(abs(p["balance_residual"]) for p in proj)

    return {
        "ticker": financials.get("ticker"),
        "warnings": warnings,
        "assumptions": {
            "revenue_growth": growth,
            "ebitda_margin": ebitda_margin,
            "dep_pct_rev": dep_pct,
            "capex_pct_rev": capex_pct,
            "tax_rate": tax_rate,
            "tax_rate_source": tax_rate_source,
            "interest_rate": interest_rate,
            "payout_ratio": payout,
            "dso": dso, "dio": dio, "dpo": dpo,
            "min_cash_pct_rev": min_cash_pct,
        },
        "projection": proj,
        "balance_check": {
            "balanced": balanced,
            "max_residual": max_residual,
        },
    }
