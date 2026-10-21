"""Security-type rules for the identity master (issue #85, Part 2).

Ported from the Identity R1 research script (issue #84). Pure functions of one plain dict per
ticker (``inputs``, see :mod:`core.identity.master` for the shape); no store, no network.

Order of the rules (later ones win where noted):

1. OpenFIGI ``securityType`` / ``securityType2`` mapped to a type;
2. SEC overrides: BDC (Form N-54A, or 10-K together with N-2) wins over CEF (N-CSR / N-CSRS /
   NPORT-P, unless OpenFIGI says ETP); SPAC (SIC 6770 or an "Acquisition/Merger Corp" name)
   replaces a common-like type, **unless the shell was closed out** (8-K item 5.06);
3. the Nasdaq Trader ETF flag makes an ETP;
4. name rules (preferred, warrant) when nothing has typed the ticker yet;
5. Unusual Whales ``issue_type`` as the last fallback, else ``unknown``;
6. FDIC: a common-like ticker with no SEC periodic filings that matches an FDIC institution is
   a ``bank_nonsec``.

Every rule returns ``(type, source_label)`` with the same labels R1 used.
"""
from __future__ import annotations

import re

# Types that operate a business and stay on the boards / types that are dropped from them.
# The user decided: BDCs stay, SPACs are excluded. ``core/universe.py`` reads these (R4, #87).
OPERATING_TYPES = {"common", "adr", "reit", "bank_nonsec", "bdc", "mlp", "royalty_trust"}
EXCLUDED_TYPES = {"cef", "etp", "spac", "preferred", "unit", "warrant"}

N2_FORMS = {"N-2", "N-2/A", "N-2ASR", "N-2MEF", "N-2 POSASR"}
CEF_FORMS = {"N-CSR", "N-CSRS", "NPORT-P"}
SPAC_NAME = re.compile(r"\b(acquisition|merger)s?\s+(corp|corporation|co|company|ltd|limited|inc|holdings?)\b", re.I)
_COMMON_LIKE = (None, "common", "unit", "other")

BDC_SOURCE = "sec_forms(N-54A or 10-K+N-2)"
CEF_SOURCE = "sec_forms(N-CSR/N-CSRS/NPORT-P)"
FDIC_SOURCE = "fdic_name_match(no SEC periodic filings)"


def openfigi_type(st, st2) -> str | None:
    """Map OpenFIGI ``securityType`` / ``securityType2`` to one of our types (``None`` if OpenFIGI said nothing)."""
    st = st if isinstance(st, str) else ""
    st2 = st2 if isinstance(st2, str) else ""
    if st == "Common Stock":
        return "common"
    if st in ("ADR", "NY Reg Shrs", "GDR", "EDR") or st2 == "Depositary Receipt":
        return "adr"
    if st == "Closed-End Fund":
        return "cef"
    if st in ("ETP", "ETF", "Open-End Fund", "Mutual Fund") or st2 in ("ETP",):
        return "etp"
    if st == "REIT":
        return "reit"
    if st in ("MLP", "Ltd Part") or st2 == "Partnership Shares":
        return "mlp"
    if st in ("Preference", "Preferred") or st2 in ("Preference", "Preferred"):
        return "preferred"
    if st == "Unit":
        return "unit"
    if st == "Royalty Trst":
        return "royalty_trust"
    if st in ("Equity WRT", "Warrant"):
        return "warrant"
    if st2 == "Common Stock":
        return "common"
    return "other" if st else None


def sec_class(forms) -> str:
    """Filer class from a set of form types (R1's ``sec_facts``)."""
    f = set(forms or [])
    if "N-54A" in f or ("10-K" in f and f & N2_FORMS):
        return "bdc"
    if f & CEF_FORMS:
        return "cef"
    if f & {"10-K", "10-Q", "10-K405", "10-KT"}:
        return "domestic_filer"
    if f & {"20-F", "40-F", "6-K"}:
        return "foreign_filer"
    if f & {"S-1", "F-1", "S-4", "F-4", "10-12B", "10-12G", "S-11"}:
        return "new_registrant"
    return "no_periodic_filings"


def uw_type(v) -> str | None:
    """Unusual Whales ``issue_type`` mapped to our vocabulary (``equity`` = plain common stock)."""
    return {"ETF": "etp", "ADR": "adr", "Unit": "unit", "Common Stock": "equity"}.get(v or "", None)


def sic_int(v) -> int | None:
    try:
        s = str(v).strip()
        return int(s) if s and s.lower() not in ("nan", "none") else None
    except ValueError:
        return None


def sec_present(inputs: dict) -> bool:
    """True when the SEC submissions source answered with a real filer record."""
    sec = inputs.get("sec")
    return bool(sec) and sec.get("status", "ok") == "ok"


def filer_class(inputs: dict) -> str:
    return sec_class((inputs.get("sec") or {}).get("forms")) if sec_present(inputs) else "not_in_sec"


def spac_signals(ticker: str, inputs: dict) -> tuple[bool, bool, bool]:
    """``(by_sic_6770, by_name_rule, shell_closed)`` for a ticker."""
    sec = inputs.get("sec") if sec_present(inputs) else None
    sec = sec or {}
    by_sic = sic_int(sec.get("sic")) == 6770
    by_name = bool(SPAC_NAME.search(str(inputs.get("name") or ""))
                   or SPAC_NAME.search(str(sec.get("name") or "")))
    return by_sic, by_name, bool(sec.get("shell_closed"))


def classify_type(ticker: str, inputs: dict) -> tuple[str, str]:
    """``(type, source_label)`` for one ticker. Never raises on missing sources."""
    figi = inputs.get("openfigi") or {}
    ft = openfigi_type(figi.get("security_type"), figi.get("security_type2"))
    typ, src = ft, ("openfigi" if ft else None)

    cls = filer_class(inputs)
    by_sic, by_name, closed = spac_signals(ticker, inputs)
    is_spac = (by_sic or by_name) and not closed
    if cls == "bdc":
        typ, src = "bdc", BDC_SOURCE
    elif cls == "cef" and ft != "etp":
        typ, src = "cef", CEF_SOURCE
    elif is_spac and typ in _COMMON_LIKE:
        typ, src = "spac", "sec_sic_6770" if by_sic else "name_rule(Acquisition/Merger Corp)"

    nasdaq = inputs.get("nasdaq") or {}
    if nasdaq.get("is_etf") is True:
        typ, src = "etp", "nasdaq_etf_flag"

    names = f"{nasdaq.get('security_name') or ''} {inputs.get('name') or ''}"
    if typ is None and (re.search(r"preferred|depositary shares?,? each representing", names, re.I)
                        or re.search(r"-P[A-Z]?$", ticker)):
        typ, src = "preferred", "name_rule(preferred / depositary shares)"
    if typ is None and re.search(r"\bwarrants?\b", names, re.I):
        typ, src = "warrant", "name_rule(warrant)"
    if typ is None:
        ut = uw_type((inputs.get("uw") or {}).get("security_type"))
        if ut:
            typ, src = ("common" if ut == "equity" else ut), "uw_issue_type"
        else:
            typ, src = "unknown", "none"

    # FDIC: no SEC periodic filings and the other signals say common stock
    if cls in ("no_periodic_filings", "not_in_sec") and typ in ("common", "unknown", "other"):
        match = (inputs.get("fdic") or {}).get("match")
        if match:
            typ, src = "bank_nonsec", FDIC_SOURCE
    return typ, src
