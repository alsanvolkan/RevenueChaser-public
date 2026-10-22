"""Ortak "Sector & Theme" taksonomisi — tüm dashboard'lar için tek doğruluk kaynağı.

- Sektör: ``core.gics_map`` (11 Morningstar sector names, UW's two levels: sector + industry) — tek kaynak.
- Tema: yalnızca pinlenmiş NMF medium koşusu (``module_relatedness.themes``). Anahtar-kelime eşleşmesi ve
  sektör tahmini C21 (#383) ile kaldırıldı: bir tema ya ölçülmüştür ya da yoktur — tahmin edilmiş bir etiket
  board'da ölçülmüş olandan ayırt edilemiyordu.
"""
from __future__ import annotations

from RevenueChaser.core.gics_map import SECTORS_11, UNCLASSIFIED


def sort_categories(items) -> list[str]:
    """Sort category labels alphabetically (case-insensitive), 'Other'/'Others' and 'Unclassified' last.

    Universal sort for sector/theme filters across every dashboard. ``Unclassified`` is the sector unknown
    bucket (R3, #86); ``Other`` is the legacy pre-R3 spelling of the same bucket.
    """
    items = [str(i).strip() for i in items if str(i).strip()]
    seen: set[str] = set()
    uniq = []
    for i in items:
        if i.lower() not in seen:
            seen.add(i.lower())
            uniq.append(i)

    def _key(name: str) -> tuple:
        low = name.lower()
        is_other = low in ("other", "others", UNCLASSIFIED.lower()) or low.startswith("other ")
        return (1 if is_other else 0, low)

    return sorted(uniq, key=_key)


# Canonical, already-sorted list for the sector filters (Unclassified always last).
SORTED_SECTORS: list[str] = sort_categories(SECTORS_11 + [UNCLASSIFIED])


def themes_for(ticker: str) -> list[str]:
    """Pinned NMF-run medium themes for a ticker (``[]`` when none/unknown)."""
    if not ticker:
        return []
    try:
        from RevenueChaser.module_relatedness import themes as _th
        return _th.themes_for_ticker(ticker)
    except Exception:  # noqa: BLE001 - the loader must never break a scan
        return []


def themes_for_many(tickers) -> dict[str, list[str]]:
    """``themes_for`` for many tickers in a fixed number of store queries (tickers with none are absent)."""
    try:
        from RevenueChaser.module_relatedness import themes as _th
        return _th.themes_for_tickers(tickers)
    except Exception:  # noqa: BLE001 - the loader must never break a scan
        return {}


def assign_themes(ticker: str | None = None) -> list[str]:
    """The theme labels a board shows for ``ticker``: the pinned NMF medium themes, or ``[]``.

    No fallback (C21, #383): a keyword match or a sector guess reads on a board exactly like a measured
    theme, so a name nobody has classified carries no theme at all.
    """
    return themes_for(ticker) if ticker else []


__all__ = [
    "SECTORS_11", "sort_categories", "SORTED_SECTORS",
    "themes_for", "themes_for_many", "assign_themes",
]
