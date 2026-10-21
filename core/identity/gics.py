"""Sector and industry by source precedence (issue #85, Part 2; reworked in #265; order extended in #272).

Classification is UW's two levels: the **sector** (one of the 11 Morningstar names in
:data:`core.gics_map.MORNINGSTAR_SECTORS`, stored as the provider names it) and the **industry** (the provider's
own industry text). For one ticker each level takes the first available source, and the source is recorded per
level:

1. ``manual`` (a dict the caller passes; the R7 hook);
2. ``sp1500`` -- the S&P 1500 Wikipedia list, for its own names only. Its GICS sector is written **through**
   :func:`core.gics_map.canonical_sector`, so the column keeps the 11 Morningstar names; a spelling the map does not
   know is no sector. Only the sector is taken: the list's sub-industry is never written, and the industry stays the
   provider text below;
3. ``provider_uw``, then ``provider_yf`` only when UW has no usable sector. A sector is usable when it is one of the
   11 names. The industry comes from the **same provider row** as the sector, and only when that row's sector is the
   chosen sector, so an industry is never combined with a sector of another source. The exception is a sector the
   list decided: the industry is then UW's text (Yahoo's when UW has none) whatever sector that row names, because
   the industry is UW's text for everyone;
4. ``sic_explicit`` -- the explicit SIC table (:mod:`core.identity.sic_sector`, 263 four-digit codes) for a name none
   of the above gave a sector. It gives a sector only; there is no industry text from SIC;
5. ``type_rule`` -- cef / bdc -> Financial Services / Asset Management, bank_nonsec -> Financial Services /
   Banks - Regional; an ETP has no classification at all;
6. ``Unclassified`` (source ``none``).

There is no GICS rename and no correction list.
"""
from __future__ import annotations

from ..gics_map import MORNINGSTAR_SECTORS, UNCLASSIFIED, canonical_sector
from . import sic_sector

LEVELS = ("sector", "industry")

_PROVIDERS = (("uw", "provider_uw"), ("yahoo", "provider_yf"))


def provider_sector(row) -> str | None:
    """The Morningstar sector name of a provider row, else ``None`` (blank, or not one of the 11 names)."""
    sector = (row or {}).get("sector")
    sector = sector.strip() if isinstance(sector, str) else None
    return sector if sector in MORNINGSTAR_SECTORS else None


def list_sector(row) -> str | None:
    """The Morningstar sector of an S&P 1500 list row (its GICS sector through ``canonical_sector``), else ``None``
    (blank, not a string, or a spelling the canonical map does not know)."""
    raw = (row or {}).get("sector")
    if not isinstance(raw, str) or not raw.strip():
        return None
    sector = canonical_sector(raw)
    return sector if sector in MORNINGSTAR_SECTORS else None


def _text(value) -> str | None:
    value = value.strip() if isinstance(value, str) else None
    return value or None


def classify(ticker: str, inputs: dict, typ: str, manual: dict | None = None) -> dict[str, tuple[str, str]]:
    """``{level: (value, source)}`` for the two levels (sector, industry) of one ticker."""
    L: dict = dict.fromkeys(LEVELS)
    S: dict = dict.fromkeys(LEVELS)

    def put(level, val, src):
        if L[level] is None and val:
            L[level], S[level] = val, src

    def done():
        for lv in LEVELS:
            if L[lv] is None:
                L[lv], S[lv] = UNCLASSIFIED, "none"
        return {lv: (L[lv], S[lv]) for lv in LEVELS}

    # 1. manual
    for lv in LEVELS:
        put(lv, (manual or {}).get(lv), "manual")

    # ETPs hold baskets: no classification of their own (a stale provider sector must not leak in)
    if typ == "etp":
        for lv in LEVELS:
            put(lv, UNCLASSIFIED, "type_rule(etp: no GICS)")
        return done()

    # 2. the S&P 1500 list: its sector for its own names; the industry is the provider's text (UW, then Yahoo)
    listed = list_sector(inputs.get("sp1500"))
    if listed and L["sector"] is None:
        put("sector", listed, "sp1500")
        for key, label in _PROVIDERS:
            row = inputs.get(key)
            text = _text((row or {}).get("industry")) if provider_sector(row) else None
            if text:
                put("industry", text, label)
                break

    # 3. provider sector and industry (UW, then Yahoo only where UW has no usable sector), one row for both
    for key, label in _PROVIDERS:
        row = inputs.get(key)
        sector = provider_sector(row)
        if sector and L["sector"] in (None, sector):
            put("sector", sector, label)
            put("industry", _text(row.get("industry")), label)
            break

    # 4. explicit SIC code, for a name nothing above gave a sector
    if L["sector"] is None:
        found = sic_sector.code_from(inputs)
        put("sector", sic_sector.sector_for(found[0]) if found else None, "sic_explicit")

    # 5. type rule
    if typ in ("cef", "bdc") and L["sector"] in (None, "Financial Services"):
        put("sector", "Financial Services", "type_rule")
        put("industry", "Asset Management", "type_rule")
    if typ == "bank_nonsec" and L["sector"] in (None, "Financial Services"):
        put("sector", "Financial Services", "type_rule")
        put("industry", "Banks - Regional", "type_rule")

    # 6. unclassified
    return done()
