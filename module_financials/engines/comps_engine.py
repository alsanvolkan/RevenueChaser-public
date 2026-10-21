"""Comps Engine — rakip çarpanları (EV/EBITDA, EV/Revenue, P/E) + persentil + implied valuation."""
from __future__ import annotations

import logging
from typing import Callable

from ..config import to_float
from ._inputs import net_debt as _net_debt
from RevenueChaser.core.providers.validator import validate_annual

logger = logging.getLogger(__name__)


def _pct(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {"p25": None, "p50": None, "p75": None, "count": 0}
    s = sorted(vals)
    n = len(s)
    def q(p: float) -> float:
        rank = (n - 1) * p
        lo = int(rank)
        hi = min(lo + 1, n - 1)
        w = rank - lo
        return s[lo] * (1 - w) + s[hi] * w
    return {"p25": q(0.25), "p50": q(0.50), "p75": q(0.75), "count": n}


def _default_fetch(ticker: str) -> dict:
    import yfinance as yf
    try:
        return yf.Ticker(ticker).info or {}
    except Exception as exc:  # pragma: no cover - network
        logger.debug("yfinance info %s: %s", ticker, exc)
        return {}


def run_comps(financials: dict, peers: list[str],
              fetch_info: Callable[[str], dict] | None = None) -> dict:
    """Rakip çarpan analizi; fetch_info test için enjekte edilebilir."""
    fetch = fetch_info or _default_fetch
    annual, warnings = validate_annual(financials.get("annual", []))
    if not annual:
        return {"error": "target financial data missing", "warnings": warnings}

    latest = annual[-1]
    target_ebitda = to_float(latest.get("ebitda"))
    target_revenue = to_float(latest.get("revenue"))
    target_ni = to_float(latest.get("net_income"))
    target_debt = to_float(latest.get("total_debt"))
    target_cash = to_float(latest.get("cash"))
    target_shares = to_float(financials.get("shares_outstanding"))

    rows = []
    ev_ebitda, ev_rev, pe = [], [], []
    for p in peers:
        info = fetch(p)
        ev = to_float(info.get("enterpriseValue"))
        ebitda = to_float(info.get("ebitda"))
        revenue = to_float(info.get("totalRevenue"))
        mc = to_float(info.get("marketCap"))
        ni = to_float(info.get("netIncomeToCommon"))
        pe_val = to_float(info.get("trailingPE"))
        if pe_val is None and mc is not None and ni not in (None, 0):
            pe_val = mc / ni

        ev_eb = ev / ebitda if (ev and ebitda) else None
        ev_r = ev / revenue if (ev and revenue) else None
        if ev_eb is not None: ev_ebitda.append(ev_eb)
        if ev_r is not None: ev_rev.append(ev_r)
        if pe_val is not None: pe.append(pe_val)

        rows.append({
            "ticker": p,
            "market_cap": mc,
            "enterprise_value": ev,
            "ev_ebitda": ev_eb,
            "ev_revenue": ev_r,
            "pe": pe_val,
        })

    def _implied(median_mult, target_metric):
        if median_mult is None or target_metric is None:
            return None
        return median_mult * target_metric

    med_ev_ebitda = _pct(ev_ebitda)["p50"]
    med_ev_rev = _pct(ev_rev)["p50"]
    med_pe = _pct(pe)["p50"]

    implied_ev_ebitda = _implied(med_ev_ebitda, target_ebitda)
    implied_ev_rev = _implied(med_ev_rev, target_revenue)
    implied_equity_pe = _implied(med_pe, target_ni)

    net_debt = _net_debt(target_debt, target_cash)     # None when either is unknown

    def _per_share_ev(ev):
        if ev is None or net_debt is None or target_shares is None:
            return None
        return (ev - net_debt) / target_shares

    def _per_share_eq(eq):
        if eq is None or target_shares is None:
            return None
        return eq / target_shares

    return {
        "ticker": financials.get("ticker"),
        "warnings": warnings,
        "peers": rows,
        "multiples": {
            "ev_ebitda": _pct(ev_ebitda),
            "ev_revenue": _pct(ev_rev),
            "pe": _pct(pe),
        },
        "implied": {
            "ev_ebitda": implied_ev_ebitda,
            "ev_revenue": implied_ev_rev,
            "equity_pe": implied_equity_pe,
            "per_share_ev_ebitda": _per_share_ev(implied_ev_ebitda),
            "per_share_ev_revenue": _per_share_ev(implied_ev_rev),
            "per_share_pe": _per_share_eq(implied_equity_pe),
            "net_debt": net_debt,
        },
    }
