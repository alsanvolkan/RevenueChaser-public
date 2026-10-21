"""Customer / supplier concentration sentences — the high-precision edge class.

10-K Item 1 often states concentration outright: "Amazon.com, Inc. ... represented
approximately 10.6% of our consolidated revenues", "Direct sales to Apple and
Qualcomm accounted for 29.8% and 11.1% of our net sales, respectively", "Product
purchases from ... Dell Inc. accounted for ... 11% ... of our total product
purchases". Unlike the keyword window in ``mentions``, the relation and its
direction are stated, and there is a weight.

Revenue / sales share -> the named company is a **customer** of the issuer.
Purchases share        -> the named company is a **supplier** of the issuer.
"""
from __future__ import annotations

import re

from . import aliases, sentences

_PCT = re.compile(r"(\d{1,2}(?:\.\d+)?)\s?%")
_GAP = r"(?:[^.;]|(?<=\d)\.(?=\d))"   # any char but a sentence end; "11.1" is fine
_SHARE = re.compile(
    r"\b(?:accounted\s+for|represented|comprised|made\s+up)\b" + _GAP + r"{0,160}?"
    r"\d{1,2}(?:\.\d+)?\s?%" + _GAP + r"{0,120}?\bof\s+(?:our\s+|the\s+company'?s\s+|its\s+)?"
    r"(?:total\s+|consolidated\s+|net\s+|gross\s+|segment\s+|annual\s+|product\s+)*"
    r"(revenues?|sales|net\s+sales|purchases)\b",
    re.I)


def _sentences(text: str):
    for a, b in sentences.spans(text):
        yield text[a:b]


def find(text: str, filed: str = "") -> list[dict]:
    """Concentration edges in ``text``: one per named company per relation."""
    out: dict[tuple[str, str], dict] = {}
    for sent in _sentences(text or ""):
        share = _SHARE.search(sent)
        if not share:
            continue
        relation = "supplier" if share.group(1).lower() == "purchases" else "customer"
        hits = [h for h in aliases.find_aliases(sent) if not h["context_only"]]
        if not hits:
            continue
        pcts = [float(p) for p in _PCT.findall(sent)]
        respectively = "respectively" in sent.lower() and len(pcts) == len(hits)
        snippet = " ".join(sent.split())[:400]
        for i, h in enumerate(hits):
            key = (h["ticker"], relation)
            if key in out:
                continue
            out[key] = {
                "dst_name": h["name"],
                "dst_ticker": h["ticker"],
                "relation": relation,
                "share_pct": pcts[i] if respectively else (pcts[0] if len(pcts) == 1 else None),
                "snippet": snippet,
                "filed": filed,
            }
    return list(out.values())
