"""Veri doğrulama — eksik/null alan kontrolü + fallback hiyerarşisi."""
from __future__ import annotations

from ..config import to_float

REQUIRED_FIELDS = ("revenue", "ebitda", "capex", "total_debt", "cash")

# ebitda yoksa: ebit + depreciation ile türet
def _derive_ebitda(rec: dict) -> float | None:
    ebit = to_float(rec.get("ebit"))
    dep = to_float(rec.get("depreciation"))
    if ebit is None or dep is None:
        return None
    return ebit + dep


def _derive_capex(rec: dict) -> float | None:
    capex = to_float(rec.get("capital_expenditure"))
    return capex


def validate_annual(annual: list[dict]) -> tuple[list[dict], list[str]]:
    """Zorunlu alanlar için null-check + fallback; eksikleri liste halinde bildirir."""
    warnings: list[str] = []
    out: list[dict] = []
    for rec in annual:
        r = dict(rec)
        # EBITDA fallback: ebit + depreciation
        if to_float(r.get("ebitda")) is None:
            derived = _derive_ebitda(r)
            if derived is not None:
                r["ebitda"] = derived
                warnings.append(f"{r.get('fiscal_year')}: ebitda <- ebit + depreciation")
        # Capex alan adı standardize
        if "capex" not in r or to_float(r.get("capex")) is None:
            derived = _derive_capex(r)
            if derived is not None:
                r["capex"] = derived
        # Zorunlu alan eksik kayıtları işaretle
        missing = [f for f in REQUIRED_FIELDS if to_float(r.get(f)) is None]
        if missing:
            warnings.append(f"{r.get('fiscal_year')}: missing {missing}")
        out.append(r)
    return out, warnings


def latest_valid(annual: list[dict]) -> dict | None:
    """En son zorunlu-alan tam kaydı döndürür."""
    for rec in reversed(annual):
        if all(to_float(rec.get(f)) is not None for f in REQUIRED_FIELDS):
            return rec
    return None
