"""Ortak Sector & Theme taksonomisi testleri (ağ yok)."""
from __future__ import annotations

from RevenueChaser.core import taxonomy as t


# ── C21 (#383): a theme is measured or absent, never guessed ─────────────────
def test_assign_themes_with_no_pinned_themes_is_empty(rawstore_tmp_db):
    """No pinned medium theme → no label at all: not the sector guess, not a keyword hit."""
    assert t.assign_themes("NONE") == []
    assert t.assign_themes(None) == []
    assert t.assign_themes("") == []


def test_assign_themes_returns_the_pinned_themes_unchanged(monkeypatch):
    monkeypatch.setattr(t, "themes_for", lambda ticker: ["Memory", "Optics"])
    assert t.assign_themes("AAA") == ["Memory", "Optics"]


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


def test_sort_categories_puts_unclassified_and_other_last():
    assert t.sort_categories(["Zeta", "Unclassified", "Alpha", "Other", "Mid"]) == [
        "Alpha", "Mid", "Zeta", "Other", "Unclassified"]
    assert t.sort_categories(["Unclassified", "unclassified ", "Beta"]) == ["Beta", "Unclassified"]
