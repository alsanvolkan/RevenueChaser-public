"""Sector classification — the single source of truth for sector names and the two-level path.

Since Phase 1 (#265, #266) classification is **UW's two levels**: the *sector*, one of the 11 Morningstar names
(Basic Materials, Communication Services, Consumer Cyclical, Consumer Defensive, Energy, Financial Services,
Healthcare, Industrials, Real Estate, Technology, Utilities; ``docs/openapi.yaml`` "Market General Sector"), and the
*industry*, the provider's own text from the same row (``Software - Application``, ``Oil & Gas E&P``). The GICS
tree, its industry groups and sub-industries are gone, and so is the rename to GICS names.

Every board reads its sector labels from here. A spelling the Morningstar list does not use (the old GICS names
``Information Technology``, ``Health Care``, ``Financials``... from stored rows and 13F's own pipeline) is an
**adapter key** in :data:`SECTOR_ALIASES` only: :func:`canonical_sector` maps it to the Morningstar name.

``Unclassified`` (:data:`UNCLASSIFIED`) is THE label for "no sector value" (R3, #86): the identity master writes
it, and every read here emits it. The pre-R3 pipeline wrote ``Other``; a stored ``Other`` is still read as unknown
(:func:`is_unclassified`), so rows the master has not reached are normalised at read time.

Non-equity instruments (crypto, indices, macro) are NOT sectors — they carry an ``asset_class`` label on their own
and resolve to an unknown sector.
"""
from __future__ import annotations

from dataclasses import dataclass


# The one label for "no sector value" (R3, #86). The identity master writes it; ``sector_path`` and
# ``canonical_sector`` emit it. The legacy pre-R3 spelling ``Other`` is still READ as unknown (see
# ``is_unclassified``) but is never emitted by those.
UNCLASSIFIED = "Unclassified"
_LEGACY_UNKNOWN = "Other"


def is_unclassified(label) -> bool:
    """True for ``Unclassified`` and the legacy ``Other``: a stored value that means "no sector value".

    An empty value (``""`` / ``None``) is NOT unclassified — it is "no value given", which several
    readers keep distinct (a level that was empty stays empty).
    """
    return isinstance(label, str) and label.strip() in (UNCLASSIFIED, _LEGACY_UNKNOWN)


def sector_or_unclassified(label) -> str:
    """A sector label for display: blank, ``Other`` and ``Unclassified`` all read ``Unclassified``."""
    if not isinstance(label, str) or not label.strip() or is_unclassified(label):
        return UNCLASSIFIED
    return label.strip()


def _norm(value: str) -> str:
    """Lowercase + unify dash variants so vendor strings match the alias table."""
    s = (value or "").strip().lower()
    for dash in ("\u2014", "\u2013", "\u2012", "\u2212"):
        s = s.replace(dash, "-")
    return " ".join(s.split())


# Unusual Whales' own sector names, which Yahoo's ``.info`` uses too (``docs/openapi.yaml``, "Market General
# Sector", alphabetical as the enum lists them). The canonical sector names (#265, #266).
# ``Unclassified`` is appended by consumers (filters), never here.
MORNINGSTAR_SECTORS: tuple[str, ...] = (
    "Basic Materials", "Communication Services", "Consumer Cyclical", "Consumer Defensive", "Energy",
    "Financial Services", "Healthcare", "Industrials", "Real Estate", "Technology", "Utilities",
)
SECTORS_11: list[str] = list(MORNINGSTAR_SECTORS)

# Vendor / older sector spelling (lowercased) -> the Morningstar name. Adapter only: stored rows from before the
# rename, 13F's own pipeline and ``ticker_scores`` snapshots carry the GICS spellings.
SECTOR_ALIASES: dict[str, str] = {
    "technology": "Technology",
    "information technology": "Technology",
    "software": "Technology",
    "communication services": "Communication Services",
    "consumer discretionary": "Consumer Cyclical",
    "consumer cyclical": "Consumer Cyclical",
    "consumer staples": "Consumer Defensive",
    "consumer defensive": "Consumer Defensive",
    "energy": "Energy",
    "financials": "Financial Services",
    "financial services": "Financial Services",
    "health care": "Healthcare",
    "healthcare": "Healthcare",
    "industrials": "Industrials",
    "materials": "Basic Materials",
    "basic materials": "Basic Materials",
    "real estate": "Real Estate",
    "utilities": "Utilities",
}


def canonical_sector(raw) -> str:
    """The Morningstar sector for a vendor or older sector string; anything else reads ``Unclassified``."""
    return SECTOR_ALIASES.get(_norm(raw) if isinstance(raw, str) else "", UNCLASSIFIED)


@dataclass(frozen=True)
class GicsPath:
    """The spine's classification of one ticker: the sector and the industry text (``Unclassified`` when unknown)."""

    sector: str
    industry: str


def sector_path(sector, industry) -> list[str]:
    """``[sector, industry]`` for the filter tree.

    The stored sector is normalised onto the Morningstar names (``canonical_sector``); an empty or unknown one
    reads ``Unclassified``. The industry is the stored text, trimmed; ``Unclassified`` and the legacy ``Other``
    read ``Unclassified``, an empty one stays ``""`` (the tree stops at the sector).
    """
    ind = industry.strip() if isinstance(industry, str) else ""
    if is_unclassified(ind):
        ind = UNCLASSIFIED
    return [canonical_sector(sector), ind]


__all__ = [
    "MORNINGSTAR_SECTORS", "SECTORS_11", "SECTOR_ALIASES", "GicsPath", "canonical_sector", "sector_path",
    "UNCLASSIFIED", "is_unclassified", "sector_or_unclassified",
]
