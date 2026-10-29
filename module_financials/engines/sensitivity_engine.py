"""Duyarlılık Engine — WACC × büyüme matrisi (tek/iki yönlü tablo)."""
from __future__ import annotations

from ..config import TERMINAL_GROWTH
from ._inputs import not_for_a_bank, pick
from .dcf_engine import run_dcf

WACC_DELTAS = [-0.02, -0.01, 0.0, 0.01, 0.02]
GROWTH_DELTAS = [-0.01, 0.0, 0.01]


def run_sensitivity(financials: dict, base_assumptions: dict | None = None) -> dict:
    """Base DCF üzerinden WACC × terminal büyüme matrisi üretir."""
    if (bank := not_for_a_bank(financials, "a WACC x growth grid of the DCF")) is not None:
        return bank
    a = dict(base_assumptions or {})
    base = run_dcf(financials, a)
    if "error" in base:
        return base

    base_wacc = base["wacc"]["wacc"]
    base_g = pick(a, "terminal_growth", TERMINAL_GROWTH)

    waccs = [base_wacc + d for d in WACC_DELTAS]
    growths = [base_g + d for d in GROWTH_DELTAS]

    matrix = []
    for g in growths:
        values = []
        for w in waccs:
            r = run_dcf(financials, {**a, "wacc_override": w, "terminal_growth": g})
            values.append(r.get("valuation", {}).get("per_share_gordon"))
        matrix.append({"terminal_growth": g, "values": values})

    return {
        "ticker": financials.get("ticker"),
        "wacc_axis": waccs,
        "growth_axis": growths,
        "base_wacc": base_wacc,
        "base_terminal_growth": base_g,
        "base_per_share": base.get("valuation", {}).get("per_share_gordon"),
        "matrix": matrix,
    }
