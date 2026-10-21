"""Conflict detection for the identity master (issue #85, Part 2).

Ported from the Identity R1 research script (issue #84). A conflict is a disagreement between
two sources (or a source and the proposed value). Rows are
``{ticker, kind, field, side_a, side_b, note}``; ``(ticker, kind, field)`` is the table key, so
``field`` names what was compared (``security_type:uw_issue_type``, ``name:openfigi``,
``market_cap:uw`` ...). **Conflicts only flag: nothing here changes a value** (the stored
Yahoo market cap stays the cap source whatever UW says).
"""
from __future__ import annotations

import re

from . import names
from . import types as T

NAME_THRESHOLD = 0.6
CAP_RATIO = 10.0
# (OpenFIGI type, proposed type) pairs that are the rules working as designed
EXPECTED = {("cef", "bdc"), ("common", "spac"), ("common", "bank_nonsec"), ("unit", "spac"), ("other", "spac")}


def _row(ticker, kind, field, a, b, note=""):
    return {"ticker": ticker, "kind": kind, "field": field, "side_a": None if a is None else str(a),
            "side_b": None if b is None else str(b), "note": note}


def _name_conflicts(ticker: str, inputs: dict) -> list[dict]:
    stored = inputs.get("name")
    if stored is None or str(stored).strip() == "":
        return []
    candidates = (("sec", (inputs.get("sec") or {}).get("name")),
                  ("openfigi", (inputs.get("openfigi") or {}).get("name")),
                  ("nasdaq", (inputs.get("nasdaq") or {}).get("security_name")))
    scores = {src: names.name_score(stored, other) for src, other in candidates}
    scores = {src: s for src, s in scores.items() if s is not None}
    numeric = re.fullmatch(r"\s*\d+\s*", str(stored)) is not None
    if not scores:
        return []
    if not numeric and any(s >= NAME_THRESHOLD for s in scores.values()):
        return []
    other = dict(candidates)
    return [_row(ticker, "name", f"name:{src}", stored, other[src],
                 "stored name is a number" if numeric else f"similarity {s} < {NAME_THRESHOLD}")
            for src, s in scores.items() if s < NAME_THRESHOLD]


def _type_conflicts(ticker: str, inputs: dict, typ: str, type_source: str) -> list[dict]:
    out = []
    proposed = f"{typ} ({type_source})"
    figi = inputs.get("openfigi") or {}
    ft = T.openfigi_type(figi.get("security_type"), figi.get("security_type2"))
    if ft and ft != typ:
        out.append(_row(ticker, "type", "security_type:openfigi",
                        f"{figi.get('security_type')}/{figi.get('security_type2')}", proposed,
                        "expected override" if (ft, typ) in EXPECTED else "check"))
    cls = T.filer_class(inputs)
    if cls in ("cef", "bdc") and typ not in ("cef", "bdc"):
        out.append(_row(ticker, "type", "security_type:sec_forms", cls, proposed,
                        "SEC fund forms but not typed as a fund"))
    etf = (inputs.get("nasdaq") or {}).get("is_etf")
    if etf is True and ft not in ("etp", None):
        out.append(_row(ticker, "type", "security_type:nasdaq_etf_flag", "Y", figi.get("security_type"),
                        "Nasdaq says ETF, OpenFIGI does not"))
    elif etf is False and ft == "etp":
        out.append(_row(ticker, "type", "security_type:nasdaq_etf_flag", "N", figi.get("security_type"),
                        "OpenFIGI says ETP, Nasdaq does not"))
    raw = (inputs.get("uw") or {}).get("security_type")
    ut = T.uw_type(raw)
    if ut:
        bad = ((ut == "etp") != (typ == "etp")) or (ut == "adr" and typ != "adr") or \
              (ut == "unit" and typ not in ("unit", "spac"))
        if bad:
            out.append(_row(ticker, "type", "security_type:uw_issue_type", raw, proposed,
                            "UW issue type disagrees"))
    by_sic, by_name, closed = T.spac_signals(ticker, inputs)
    if closed and by_sic:
        out.append(_row(ticker, "type", "security_type:spac_shell_closed", "sic 6770",
                        "8-K item 5.06: shell status changed", "SIC still 6770 after de-SPAC; typed as operating"))
    if typ == "spac":
        ind = (inputs.get("uw") or {}).get("industry") or (inputs.get("yahoo") or {}).get("industry")
        if isinstance(ind, str) and ind and ind != "Shell Companies":
            out.append(_row(ticker, "type", "security_type:spac_provider_industry", "spac", ind,
                            "provider sees an operating business"))
    return out


def _cap_conflicts(ticker: str, inputs: dict) -> list[dict]:
    try:
        stored = float(inputs.get("market_cap"))
        other = float((inputs.get("uw") or {}).get("market_cap"))
    except (TypeError, ValueError):
        return []
    if stored <= 0 or other <= 0 or not (stored / other > CAP_RATIO or other / stored > CAP_RATIO):
        return []
    return [_row(ticker, "cap", "market_cap:uw", round(stored), round(other),
                 "caps differ > 10x: stored is higher (flag only, neither value is judged wrong)" if stored > other else
                 "caps differ > 10x: provider is higher (flag only, neither value is judged wrong)")]


def detect(ticker: str, inputs: dict, typ: str, type_source: str) -> list[dict]:
    """Every conflict for one ticker, given the type the rules proposed."""
    return _name_conflicts(ticker, inputs) + _type_conflicts(ticker, inputs, typ, type_source) + \
        _cap_conflicts(ticker, inputs)
