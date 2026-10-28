"""DCF Engine — FCF, WACC (CAPM), 5y projeksiyon, terminal değer, NPV/hisse başına değer.

FCFF = NOPAT + D&A − Capex − ΔNWC, NOPAT = EBIT·(1−t).
Terminal değer iki yöntem yan yana: Gordon Growth ve Exit Multiple (EV/EBITDA).
"""
from __future__ import annotations

from ..config import (
    DEFAULT_BETA,
    DEFAULT_CAPEX_PCT_REV,
    DEFAULT_COST_OF_DEBT,
    DEFAULT_DEBT_WEIGHT,
    DEFAULT_EBITDA_MARGIN,
    DEFAULT_EXIT_MULTIPLE,
    DEFAULT_REVENUE_GROWTH,
    EQUITY_RISK_PREMIUM,
    PROJECTION_YEARS,
    RISK_FREE_RATE,
    TERMINAL_GROWTH,
    to_float,
)
from RevenueChaser.core.providers.validator import validate_annual

from ._inputs import (DNA_ASSUMED_ZERO, assumed, effective_tax_rate as _effective_tax_rate, first_known,
                      net_debt as _net_debt, pick)


def _nwc(rec: dict) -> float | None:
    """İşletme NWC = alacaklar + stok − borçlar; eksikse dönen varlık − kısa vadeli yükümlülük."""
    r = to_float(rec.get("receivables"))
    inv = to_float(rec.get("inventory"))
    p = to_float(rec.get("payables"))
    if r is not None and inv is not None and p is not None:
        return r + inv - p
    ca = to_float(rec.get("current_assets"))
    cl = to_float(rec.get("current_liabilities"))
    if ca is not None and cl is not None:
        return ca - cl
    return None


def _historical_fcff(annual: list[dict], tax_rate: float) -> list[dict]:
    """Geçmiş yıl FCF hesaplama (kronolojik)."""
    rows = []
    prev_nwc = None
    for rec in annual:
        ebit = to_float(rec.get("ebit"))
        dep = to_float(rec.get("depreciation"))
        capex = first_known(to_float(rec.get("capex")), to_float(rec.get("capital_expenditure")))
        nwc = _nwc(rec)
        if ebit is None or dep is None or capex is None:
            rows.append({"fiscal_year": rec.get("fiscal_year"), "fcff": None,
                         "ebit": ebit, "nopat": None, "dna": dep, "capex": capex, "dnwc": None})
            prev_nwc = nwc
            continue
        nopat = ebit * (1 - tax_rate)
        dnwc = (nwc - prev_nwc) if (nwc is not None and prev_nwc is not None) else None
        fcff = nopat + dep - capex - (dnwc or 0.0)
        rows.append({"fiscal_year": rec.get("fiscal_year"), "fcff": fcff,
                     "ebit": ebit, "nopat": nopat, "dna": dep, "capex": capex, "dnwc": dnwc})
        prev_nwc = nwc
    return rows


def _wacc(financials: dict, beta: float | None, rf: float, erp: float,
          cost_of_debt: float, tax_rate: float, debt_weight: float | None) -> dict:
    b = beta if beta is not None else first_known(to_float(financials.get("beta")), DEFAULT_BETA)
    ke = rf + b * erp
    kd_after = cost_of_debt * (1 - tax_rate)

    e = to_float(financials.get("market_cap"))
    annual = financials.get("annual", [])
    # One fiscal year: the debt of the latest year only, never an older year's (#173 M15).
    d = to_float(annual[-1].get("total_debt")) if annual else None
    if debt_weight is not None:
        wd, we, source = debt_weight, 1 - debt_weight, "assumption"
    elif e is not None and d is not None and (e + d) > 0:
        we, wd, source = e / (e + d), d / (e + d), "market"
    else:
        # No market mix: the default one, which the board labels (review S1).
        wd, source = DEFAULT_DEBT_WEIGHT, "default"
        we = 1 - wd
    wacc = we * ke + wd * kd_after
    return {
        "beta": b, "cost_of_equity": ke, "cost_of_debt_after_tax": kd_after,
        "equity_weight": we, "debt_weight": wd, "weights_source": source, "wacc": wacc,
    }


def run_dcf(financials: dict, assumptions: dict | None = None) -> dict:
    """DCF değerleme çalıştırır ve yapılandırılmış sonuç döndürür."""
    a = dict(assumptions or {})
    annual, warnings = validate_annual(financials.get("annual", []))
    if not annual:
        return {"error": "insufficient financial data", "warnings": warnings}

    projection_years = int(pick(a, "projection_years", PROJECTION_YEARS))
    if projection_years < 1:
        return {"error": "projection_years must be at least 1", "warnings": warnings}

    # Varsayım override'ları
    growth = list(a.get("revenue_growth") or DEFAULT_REVENUE_GROWTH)[:projection_years]
    if len(growth) < projection_years:
        # More years than growth rates: hold the last given rate (was an IndexError, #173 M14).
        warnings.append(f"revenue growth given for {len(growth)} years, last rate held for "
                        f"the other {projection_years - len(growth)} of {projection_years}")
        growth += [growth[-1]] * (projection_years - len(growth))

    latest = annual[-1]
    rev0 = to_float(latest.get("revenue"))
    ebitda0 = to_float(latest.get("ebitda"))
    dep0 = to_float(latest.get("depreciation"))
    if rev0 is None or rev0 <= 0:
        return {"error": "no revenue in latest year", "warnings": warnings}

    # The user's rate, else the statements' (provision / pretax), else the default (FIN-3; labelled, FIN-9 B).
    # No sector band carries a tax_rate, so that default is DEFAULT_TAX_RATE for every sector (see _inputs).
    tax_rate, tax_rate_source = _effective_tax_rate(a, latest, financials.get("sector"))

    # The base-year rows the statements did not give: the model runs on a default, the board labels it (review S1).
    defaults: list[str] = []
    margin0 = (ebitda0 / rev0) if ebitda0 is not None else DEFAULT_EBITDA_MARGIN
    if ebitda0 is None:
        defaults.append("ebitda_margin")
        if to_float(a.get("ebitda_margin")) is None:
            warnings.append(assumed("EBITDA margin", f"{DEFAULT_EBITDA_MARGIN:.1%}"))
    ebitda_margin = pick(a, "ebitda_margin", margin0)
    dep_pct = (dep0 / rev0) if dep0 is not None else 0.0
    if dep0 is None:
        # EBIT = EBITDA and FCFF adds nothing back: the board must say so (FIN-2 / DF41).
        defaults.append("dep_pct_rev")
        warnings.append(DNA_ASSUMED_ZERO)
    capex_pct = to_float(a.get("capex_pct_rev"))
    if capex_pct is None:
        hist_capex = to_float(latest.get("capex"))
        if hist_capex is not None:
            capex_pct = hist_capex / rev0
        else:
            capex_pct = DEFAULT_CAPEX_PCT_REV
            defaults.append("capex_pct_rev")
            warnings.append(assumed("Capex", f"{DEFAULT_CAPEX_PCT_REV:.1%} of revenue"))

    # NWC/revenue oranı: tarihsel ortalama (varsayım yoksa)
    nwc_pct = to_float(a.get("nwc_pct_rev"))
    if nwc_pct is None:
        ratios = []
        for rec in annual:
            nw = _nwc(rec)
            r = to_float(rec.get("revenue"))
            if nw is not None and r:
                ratios.append(nw / r)
        nwc_pct = sum(ratios) / len(ratios) if ratios else 0.0
        if not ratios:
            defaults.append("nwc_pct_rev")
            warnings.append(assumed("NWC", f"{nwc_pct:.1%} of revenue", "no working-capital lines reported"))

    rf = pick(a, "risk_free", RISK_FREE_RATE)
    erp = pick(a, "equity_risk_premium", EQUITY_RISK_PREMIUM)
    cost_of_debt = pick(a, "cost_of_debt", DEFAULT_COST_OF_DEBT)
    beta = to_float(a.get("beta"))
    debt_weight = to_float(a.get("debt_weight"))
    terminal_growth = pick(a, "terminal_growth", TERMINAL_GROWTH)
    exit_multiple = pick(a, "exit_multiple", DEFAULT_EXIT_MULTIPLE)

    wacc_info = _wacc(financials, beta, rf, erp, cost_of_debt, tax_rate, debt_weight)
    if wacc_info["weights_source"] == "default":
        warnings.append(assumed("WACC weights", f"{wacc_info['equity_weight']:.0%} equity / "
                                f"{wacc_info['debt_weight']:.0%} debt", "market cap or total debt missing"))
    wacc_override = to_float(a.get("wacc_override"))
    wacc = wacc_override if wacc_override is not None else wacc_info["wacc"]
    wacc_info["wacc"] = wacc

    # Projeksiyon
    proj = []
    rev_prev = rev0
    nwc_prev = nwc_pct * rev0
    for i in range(projection_years):
        g = growth[i]
        rev = rev_prev * (1 + g)
        ebitda = rev * ebitda_margin
        dep = rev * dep_pct
        ebit = ebitda - dep
        nopat = ebit * (1 - tax_rate)
        capex = rev * capex_pct
        nwc = nwc_pct * rev
        dnwc = nwc - nwc_prev
        fcff = nopat + dep - capex - dnwc
        disc = (1 + wacc) ** (i + 1)
        proj.append({
            "year": i + 1,
            "revenue": rev,
            "ebitda": ebitda,
            "ebit": ebit,
            "nopat": nopat,
            "dna": dep,
            "capex": capex,
            "dnwc": dnwc,
            "fcff": fcff,
            "discount_factor": disc,
            "pv_fcff": fcff / disc,
        })
        rev_prev = rev
        nwc_prev = nwc

    # Terminal değer (iki yöntem)
    last_fcff = proj[-1]["fcff"]
    last_ebitda = proj[-1]["ebitda"]
    pv_denom = (1 + wacc) ** projection_years
    gordon_tv = last_fcff * (1 + terminal_growth) / (wacc - terminal_growth) if wacc > terminal_growth else None
    gordon_note = None
    if gordon_tv is None:
        gordon_note = (f"Gordon value not defined: terminal growth {terminal_growth:.2%} is not below "
                       f"WACC {wacc:.2%}")
        warnings.append(gordon_note)
    exit_tv = last_ebitda * exit_multiple

    pv_fcff_sum = sum(p["pv_fcff"] for p in proj)
    pv_gordon = (gordon_tv / pv_denom) if gordon_tv is not None else None
    pv_exit = exit_tv / pv_denom

    ev_gordon = pv_fcff_sum + pv_gordon if pv_gordon is not None else None
    ev_exit = pv_fcff_sum + pv_exit

    # One fiscal year, and missing is unknown: debt and cash come from the latest year only, and
    # without both there is no net debt, so no equity value or per-share value (#173 M15).
    total_debt = to_float(latest.get("total_debt"))
    cash = to_float(latest.get("cash"))
    net_debt = _net_debt(total_debt, cash)
    if net_debt is None:
        missing = " and ".join(n for n, v in (("total debt", total_debt), ("cash", cash)) if v is None)
        warnings.append(f"net debt unknown ({missing} missing in FY {latest.get('fiscal_year')}): "
                        "equity value and per-share value not computed")
    shares = to_float(financials.get("shares_outstanding"))

    def _equity(ev):
        if ev is None or net_debt is None:
            return None
        return ev - net_debt

    def _per_share(ev):
        eq = _equity(ev)
        if eq is None or not shares:
            return None
        return eq / shares

    return {
        "ticker": financials.get("ticker"),
        "warnings": warnings,
        "wacc": wacc_info,
        "historical_fcff": _historical_fcff(annual, tax_rate),
        "base_year": {
            "fiscal_year": latest.get("fiscal_year"),
            "revenue": rev0,
            "ebitda": ebitda0,
            "ebitda_margin": margin0,
            "dep_pct_rev": dep_pct,
            "capex_pct_rev": capex_pct,
            "nwc_pct_rev": nwc_pct,
            "defaults": defaults,
        },
        "assumptions": {
            "revenue_growth": growth,
            "ebitda_margin": ebitda_margin,
            "tax_rate": tax_rate,
            "tax_rate_source": tax_rate_source,
            "terminal_growth": terminal_growth,
            "exit_multiple": exit_multiple,
            "projection_years": projection_years,
        },
        "projection": proj,
        "terminal_value": {
            "gordon": gordon_tv,
            "gordon_note": gordon_note,
            "exit_multiple": exit_tv,
            "pv_gordon": pv_gordon,
            "pv_exit": pv_exit,
        },
        "valuation": {
            "pv_fcff": pv_fcff_sum,
            "ev_gordon": ev_gordon,
            "ev_exit": ev_exit,
            "equity_gordon": _equity(ev_gordon),
            "equity_exit": _equity(ev_exit),
            "per_share_gordon": _per_share(ev_gordon),
            "per_share_exit": _per_share(ev_exit),
            "net_debt": net_debt,
            "shares_outstanding": shares,
        },
    }
