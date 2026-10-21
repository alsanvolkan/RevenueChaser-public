"""Ortak "Sector & Theme" taksonomisi — tüm dashboard'lar için tek doğruluk kaynağı.

- Sektör: ``core.gics_map`` (11 Morningstar sector names, UW's two levels: sector + industry) — tek kaynak.
- Tema: Rotation Map'in ``ROTATION_ETFS`` (group=="theme") tema adları + "Photonics" + "Space".
- ``classify_themes``: ticker'ı industry/name/summary metnine göre tema(lar)a atar (kelime-sınırı).
"""
from __future__ import annotations

import re

from RevenueChaser.core.gics_map import SECTORS_11, UNCLASSIFIED, canonical_sector
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS


def _rotation_themes() -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for info in ROTATION_ETFS.values():
        if info.get("group") == "theme":
            n = info["name"]
            if n not in seen:
                seen.add(n)
                names.append(n)
    return names


THEMES: list[str] = _rotation_themes() + ["Photonics", "Space", "Other"]


# Sektör -> temsilî tema (keyword eşleşmesi boş kaldığında fallback). Her ticker'a
# en az bir tema atamayı garanti eder; "Other" temalar için catch-all.
SECTOR_THEME_FALLBACK: dict[str, str] = {
    "Technology": "Software",
    "Communication Services": "AI & Technology",
    "Consumer Cyclical": "Autonomous Tech",
    "Consumer Defensive": "Other",
    "Energy": "Oil",
    "Financial Services": "Regional Banks",
    "Healthcare": "Biotechnology",
    "Industrials": "Aerospace & Defense",
    "Basic Materials": "Metals & Mining",
    "Real Estate": "Other",
    "Utilities": "Other",
    "Other": "Other",
}


def sort_categories(items) -> list[str]:
    """Sort category labels alphabetically (case-insensitive), 'Other'/'Others' and 'Unclassified' last.

    Universal sort for sector/theme filters across every dashboard. ``Unclassified`` is the sector unknown
    bucket (R3, #86); ``Other`` is the theme catch-all and the legacy pre-R3 spelling of the same bucket.
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


# Canonical, already-sorted lists for filters (Unclassified always last). The sector list ends with the sector
# unknown bucket; the THEME list keeps its own catch-all, ``Other``.
SORTED_SECTORS: list[str] = sort_categories(SECTORS_11 + [UNCLASSIFIED])
SORTED_THEMES: list[str] = sort_categories(THEMES)

# Tema -> anahtar kelimeler (hepsi \b...\b kelime-sınırıyla eşleştirilir).
THEME_KEYWORDS: dict[str, list[str]] = {
    "AI & Technology": ["artificial intelligence", "machine learning", "deep learning",
                        "generative ai", "ai platform", "ai"],
    "Software": ["software", "application software", "enterprise software"],
    "SaaS / Cloud": ["saas", "software as a service", "subscription software"],
    "Cloud Computing": ["cloud computing", "cloud infrastructure", "cloud"],
    "Semiconductors": ["semiconductor", "semiconductors", "chip", "chips", "wafer",
                       "wafers", "fabless", "microelectronic", "microelectronics",
                       "integrated circuit"],
    "Robotics": ["robot", "robots", "robotics", "drone", "drones", "cobot", "automation"],
    "Industrial Robotics": ["industrial automation", "factory automation",
                            "material handling", "warehouse automation"],
    "Autonomous Tech": ["autonomous", "self-driving", "lidar", "adas", "autonomous vehicle"],
    "Aerospace & Defense": ["aerospace", "defense", "aviation", "aircraft", "missile"],
    "Infra / Reshoring": ["infrastructure", "construction", "reshoring",
                          "heavy equipment", "engineering"],
    "Biotechnology": ["biotechnology", "biotech", "biopharmaceutical", "biopharma",
                      "pharmaceutical", "pharmaceuticals", "therapeutic", "therapeutics",
                      "genomics", "oncology", "immunology", "biologics", "bioscience",
                      "biosciences", "drug", "drugs", "medicine"],
    "Large-Cap Biotech": ["large-cap biotech", "large cap biotech"],
    "Gold": ["gold"],
    "Silver": ["silver"],
    "Lithium / Battery": ["lithium", "battery", "batteries", "energy storage",
                          "cathode", "anode"],
    "Uranium / Nuclear": ["uranium", "nuclear", "isotope", "isotopes"],
    "Uranium Miners": ["uranium miner", "uranium mining"],
    "Oil": ["crude oil", "petroleum", "oil"],
    "Oil & Gas Exploration": ["oil and gas", "natural gas",
                              "exploration and production", "e&p", "exploration"],
    "Metals & Mining": ["mining", "metals", "copper", "mineral", "minerals", "zinc",
                        "nickel", "cobalt", "molybdenum"],
    "Airlines": ["airline", "airlines", "air carrier", "air transport"],
    "Regional Banks": ["regional bank", "community bank", "bank holding",
                       "commercial bank", "savings bank"],
    "Photonics": ["photon", "photonic", "photonics", "laser", "lasers", "optical",
                  "optics", "fiber optic"],
    "Space": ["satellite", "satellites", "rocket", "rockets", "spacecraft",
              "space exploration", "launch vehicle", "space"],
}


def classify_themes(industry: str = "", name: str = "", summary: str = "") -> list[str]:
    """Verilen metinlerden eşleşen tema adlarını döndürür (kelime-sınırı eşleşmesi)."""
    haystack = " ".join([industry, name, summary]).lower()
    matched: list[str] = []
    for theme, kws in THEME_KEYWORDS.items():
        for kw in kws:
            try:
                if re.search(r"\b" + re.escape(kw.lower()) + r"\b", haystack):
                    matched.append(theme)
                    break
            except re.error:  # pragma: no cover
                continue
    return matched


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


def assign_themes(sector: str = "", industry: str = "", name: str = "",
                  summary: str = "", ticker: str | None = None) -> list[str]:
    """``classify_themes`` + sektör fallback — her ticker'a en az bir tema atar.

    When ``ticker`` is given and the pinned NMF theme run has medium themes for
    it, those are returned (the Phase 8 cut-over); otherwise the keyword
    classifier + sector fallback run exactly as before.
    """
    pinned = themes_for(ticker) if ticker else []
    if pinned:
        return pinned
    themes = classify_themes(industry, name, summary)
    if themes:
        return themes
    return [SECTOR_THEME_FALLBACK.get(canonical_sector(sector), "Other")]


def resolve_sector_theme(ticker: str, raw_sector: str = "", industry: str = "",
                         name: str = "", summary: str = "") -> tuple[str, list[str]]:
    """Kanonik sektör (Morningstar adı) + tema(lar) döndürür."""
    return canonical_sector(raw_sector), classify_themes(industry, name, summary)


__all__ = [
    "SECTORS_11", "canonical_sector",
    "THEMES", "THEME_KEYWORDS",
    "classify_themes", "resolve_sector_theme", "assign_themes",
    "SECTOR_THEME_FALLBACK", "sort_categories",
    "SORTED_SECTORS", "SORTED_THEMES",
]
