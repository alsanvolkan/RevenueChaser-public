"""Company-name normalisation and similarity, shared by the identity adapters and rules.

Ported from the Identity R1 research helper (issue #84). Used to accept or reject a
name-based match (FDIC bank lookup) and to compare provider names.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_ABBR = {"intl": "international", "finl": "financial", "fin": "financial", "svcs": "services",
         "svc": "services", "tech": "technologies", "technology": "technologies", "bk": "bank",
         "natl": "national", "mgmt": "management", "hldgs": "", "hldg": "", "grp": "group",
         "pharma": "pharmaceuticals", "pharmaceutical": "pharmaceuticals", "sys": "systems",
         "commun": "communications", "comm": "communications", "amer": "america", "and": "",
         "ctr": "center", "resrcs": "resources", "res": "resources", "ent": "entertainment"}
_STOP = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "sa", "nv",
         "ag", "se", "holdings", "holding", "group", "the", "class", "a", "b", "c", "common", "stock", "shares",
         "share", "ordinary", "american", "depositary", "depository", "ads", "adr", "each", "representing",
         "units", "unit", "lp", "llc", "l", "p", "new", "de", "del", "com", "cl", "sponsored", "reg",
         "registered", "par", "value", "usd", "0", "01", "001", "0001", "of", "n", "na", "sab", "cv",
         "spa", "asa", "ab", "oyj", "tbk", "bhd", "pte", "kgaa", "ord", "shs", "sh", "beneficial", "interest",
         "adrs", "gdr", "each", "one", "two", "three", "rep", "representing", "sub", "voting", "series"}


def norm_name(s) -> list[str]:
    """Lower-case content tokens of a company name (suffixes dropped, abbreviations expanded)."""
    if not isinstance(s, str):
        return []
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    out = []
    for tok in s.split():
        tok = _ABBR.get(tok, tok)
        if tok and tok not in _STOP:
            out.append(tok)
    return out


def name_score(a, b) -> float | None:
    """Similarity in [0, 1] between two company names; ``None`` if either is empty."""
    ta, tb = norm_name(a), norm_name(b)
    if not ta or not tb:
        return None
    seq = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    sa, sb = set(ta), set(tb)
    contain = len(sa & sb) / min(len(sa), len(sb))
    # prefix match handles FIGI truncation ("INTL BUSINESS MACHINES" vs "International Business Machines")
    pref = sum(1 for x in sa for y in sb if x != y and len(min(x, y, key=len)) >= 4
               and (x.startswith(y) or y.startswith(x)))
    contain = max(contain, min(1.0, (len(sa & sb) + pref) / min(len(sa), len(sb))))
    return round(max(seq, contain), 3)
