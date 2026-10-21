"""Coarse NMF theme → ROTATION_ETFS focus map (store-only).

Sector Rotation keeps plotting ETFs; the Theme control only focuses which
dots are visible. See docs/dashboard_ui.md §4.
"""
from __future__ import annotations

from RevenueChaser.core import rawstore
from .universe import ROTATION_ETFS

# Hand overlay when relatedness_theme_etfs has no row for a coarse label.
# Primary ETF only; fuzzy-match on the coarse label (lowercased).
# Order matters: more specific patterns first (banks before financials).
_OVERLAY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("bank", "kre"), "KRE"),
    (("semi",), "SMH"),
    (("biotech", "biotech", "xbi"), "XBI"),
    (("health care", "healthcare", "health"), "XBI"),
    (("software", "information technology", "info tech"), "XLK"),
    (("energy", "e&p", "oil"), "XLE"),
    (("gold", "miner"), "GLD"),
    (("aerospace", "defense", "defence"), "ITA"),
    (("airline", "jets"), "JETS"),
    (("utilit",), "XLU"),
    (("real estate", "reit", "xlre"), "XLRE"),
    (("discretionary",), "XLY"),
    (("staple",), "XLP"),
    (("communicat",), "XLC"),
    (("industrial", "infra"), "XLI"),
    (("financial", "xlf"), "XLF"),
]


def _current_run_id() -> str | None:
    try:
        from RevenueChaser.core.universe import current_theme_run
        return current_theme_run()
    except Exception:  # noqa: BLE001 — store-only optional path
        return None


def _overlay_etf(label: str) -> str | None:
    low = (label or "").lower()
    if not low:
        return None
    for needles, etf in _OVERLAY_RULES:
        if any(n in low for n in needles):
            return etf if etf in ROTATION_ETFS else None
    return None


def _jaccard_etfs(theme_id: str) -> list[str]:
    try:
        rows = rawstore.query(
            "SELECT etf, rank FROM relatedness_theme_etfs "
            "WHERE theme_id = ? ORDER BY rank ASC, etf ASC",
            [theme_id],
        )
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        etf = (r.get("etf") or "").strip().upper()
        if not etf or etf in seen or etf not in ROTATION_ETFS:
            continue
        seen.add(etf)
        out.append(etf)
    return out


def _stored_coarse_labels(run_id: str) -> dict[str, str]:
    """``theme_id -> label`` as stored in ``relatedness_themes`` for the run's coarse themes."""
    try:
        rows = rawstore.query(
            "SELECT theme_id, label FROM relatedness_themes "
            "WHERE level = 'coarse' AND (theme_id = ? OR starts_with(theme_id, ?))", [run_id, f"{run_id}:"])
    except Exception:  # noqa: BLE001
        return {}
    return {r["theme_id"]: (r.get("label") or "").strip() for r in rows}


def _coarse_rows() -> list[dict]:
    """``(theme_id, label, stored_label)`` for the current run's coarse themes.

    ``label`` comes from the one vocabulary (``core.universe.theme_catalog``), so the
    Theme control reads like the Relatedness board and Co-movement. ``stored_label``
    is the label ``relatedness_themes`` holds; the hand overlay reads it, so a
    sector-named coarse theme keeps the sector ETF it plotted before the catalog
    renamed it (avo's card on the R2 review, 2026-10-14: "Eski etikete bağlı")."""
    try:
        from RevenueChaser.core.universe import theme_catalog
        catalog = theme_catalog()
    except Exception:  # noqa: BLE001
        return []
    run_id = _current_run_id()
    stored = _stored_coarse_labels(run_id) if run_id else {}
    rows = []
    for tid, t in catalog.items():
        if t.get("level") != "coarse":
            continue
        label = (t.get("label") or "").strip()
        rows.append({"theme_id": tid, "label": label, "stored_label": stored.get(tid) or label})
    rows.sort(key=lambda r: str(r.get("theme_id") or ""))
    return [r for r in rows if r["label"]]


def theme_etfs_for_coarse(label: str | None) -> list[str]:
    """ETF tickers in ROTATION_ETFS for a coarse NMF label, ordered by rank.

    Prefers ``relatedness_theme_etfs`` (Jaccard). If that table has no row for
    the matching coarse theme, falls back to the hand overlay in
    docs/dashboard_ui.md §4 when the label fuzzy-matches. Unknown / empty /
    missing store → ``[]``.
    """
    lab = (label or "").strip()
    if not lab:
        return []
    if not _current_run_id():
        return []
    matches = [r for r in _coarse_rows() if r.get("label") == lab]
    if not matches:
        return []
    # Prefer first theme_id (sorted) that has Jaccard rows; else overlay.
    for r in matches:
        etfs = _jaccard_etfs(r["theme_id"])
        if etfs:
            return etfs
    overlay = _overlay_etf(matches[0]["stored_label"])
    return [overlay] if overlay else []


def coarse_theme_catalog() -> dict[str, list[str]]:
    """``{coarse_label: [etf, ...]}`` for the current pinned run.

    Empty when there is no pin or no coarse themes. Labels come from
    ``core.universe.theme_catalog`` (the one vocabulary) and are made unique in
    appearance order (``Label``, ``Label (2)``, …) without writing the store, so
    the Sector Rotation Theme control matches the Relatedness board and still has
    one key per coarse theme when two share a label. The overlay lookup uses the
    label stored in ``relatedness_themes`` (before the catalog renames it and before
    the suffix is added). Never falls back to ``taxonomy.THEMES``.
    """
    if not _current_run_id():
        return {}
    rows = _coarse_rows()
    if not rows:
        return {}
    used: set[str] = set()
    catalog: dict[str, list[str]] = {}
    for r in rows:
        raw_label = (r.get("label") or "").strip()
        if not raw_label:
            continue
        label = raw_label
        if label in used:
            n = 2
            candidate = f"{raw_label} ({n})"
            while candidate in used:
                n += 1
                candidate = f"{raw_label} ({n})"
            label = candidate
        used.add(label)
        etfs = _jaccard_etfs(r["theme_id"])
        if not etfs:
            overlay = _overlay_etf(r["stored_label"])
            etfs = [overlay] if overlay else []
        catalog[label] = etfs
    return catalog
