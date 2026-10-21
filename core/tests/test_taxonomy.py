"""Ortak Sector & Theme taksonomisi testleri (ağ yok)."""
from __future__ import annotations

from RevenueChaser.core import taxonomy as t


def test_themes_present():
    assert "Semiconductors" in t.THEMES
    assert "Biotechnology" in t.THEMES
    assert "Aerospace & Defense" in t.THEMES
    assert "Photonics" in t.THEMES
    assert "Space" in t.THEMES


def test_classify_biotech():
    assert "Biotechnology" in t.classify_themes(industry="Biotechnology")
    assert "Biotechnology" in t.classify_themes(summary="discovers and develops pharmaceutical therapies")


def test_classify_space_vs_aerospace():
    assert "Space" in t.classify_themes(name="Rocket Lab USA")
    assert "Aerospace & Defense" in t.classify_themes(industry="Aerospace & Defense")


def test_word_boundary_no_false_positive():
    # "space" kelimesi "aerospace" içinde yanlış eşleşmemeli
    assert "Space" not in t.classify_themes(industry="Aerospace & Defense")
    # "oil" -> "soil"/"boil" eşleşmesin
    assert "Oil" not in t.classify_themes(name="Boil Holdings")


def test_resolve_sector_theme():
    sector, themes = t.resolve_sector_theme("X", "Healthcare", industry="Biotechnology")
    assert sector == "Healthcare"
    assert "Biotechnology" in themes
    # an older spelling reads as its Morningstar name
    assert t.resolve_sector_theme("X", "Health Care", industry="Biotechnology")[0] == "Healthcare"
    assert t.resolve_sector_theme("X", "Bogus")[0] == "Unclassified"


# ── R3 (#86): Unclassified is the sector unknown bucket, sorted last ────────
def test_unclassified_sorts_last_in_the_sector_list():
    from RevenueChaser.core.gics_map import UNCLASSIFIED
    assert t.SORTED_SECTORS[-1] == UNCLASSIFIED
    assert "Other" not in t.SORTED_SECTORS


def test_the_sector_list_is_the_eleven_morningstar_names_then_unclassified():
    assert t.SORTED_SECTORS == [
        "Basic Materials", "Communication Services", "Consumer Cyclical", "Consumer Defensive", "Energy",
        "Financial Services", "Healthcare", "Industrials", "Real Estate", "Technology", "Utilities",
        "Unclassified"]


def test_the_gics_industry_group_and_sub_industry_lists_are_gone():
    assert not hasattr(t, "SORTED_INDUSTRY_GROUPS") and not hasattr(t, "SORTED_SUB_INDUSTRIES")
    assert not hasattr(t, "resolve_gics")


def test_the_sector_theme_fallback_is_keyed_by_the_morningstar_names():
    from RevenueChaser.core.gics_map import SECTORS_11
    assert set(t.SECTOR_THEME_FALLBACK) == set(SECTORS_11) | {"Other"}


def test_assign_themes_falls_back_through_a_morningstar_sector_and_its_old_spelling():
    assert t.assign_themes(sector="Healthcare") == ["Biotechnology"]
    assert t.assign_themes(sector="Health Care") == ["Biotechnology"]          # old stored spelling
    assert t.assign_themes(sector="Technology") == ["Software"]
    assert t.assign_themes(sector="Basic Materials") == ["Metals & Mining"]
    assert t.assign_themes(sector="Financial Services") == ["Regional Banks"]
    assert t.assign_themes(sector="Consumer Cyclical") == ["Autonomous Tech"]


def test_the_theme_catch_all_stays_other_and_last():
    """``Other`` is a THEME fallback (not the GICS unknown bucket) and is unchanged."""
    assert t.SORTED_THEMES[-1] == "Other"
    assert t.SECTOR_THEME_FALLBACK["Consumer Defensive"] == "Other"


def test_sort_categories_puts_unclassified_and_other_last():
    assert t.sort_categories(["Zeta", "Unclassified", "Alpha", "Other", "Mid"]) == [
        "Alpha", "Mid", "Zeta", "Other", "Unclassified"]
    assert t.sort_categories(["Unclassified", "unclassified ", "Beta"]) == ["Beta", "Unclassified"]
