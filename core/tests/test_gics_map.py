"""core.gics_map — the single source of truth for sector names and the two-level path (sector, industry).

Phase 1 (#265, #266): the canonical sector names are UW's 11 Morningstar names; the GICS tree, its groups and
sub-industries, ``resolve_gics`` and the industry alias table are gone.
"""
import pytest

from RevenueChaser.core import gics_map as _g
from RevenueChaser.core.gics_map import (
    SECTOR_ALIASES, SECTORS_11, UNCLASSIFIED, GicsPath, canonical_sector, is_unclassified, sector_or_unclassified,
    sector_path,
)

MORNINGSTAR = ["Basic Materials", "Communication Services", "Consumer Cyclical", "Consumer Defensive", "Energy",
               "Financial Services", "Healthcare", "Industrials", "Real Estate", "Technology", "Utilities"]


class TestSectorNames:
    def test_the_canonical_names_are_the_eleven_morningstar_names(self):
        assert sorted(SECTORS_11) == MORNINGSTAR
        assert sorted(_g.MORNINGSTAR_SECTORS) == MORNINGSTAR

    @pytest.mark.parametrize("name", MORNINGSTAR)
    def test_every_morningstar_name_is_its_own_canonical_sector(self, name):
        assert canonical_sector(name) == name
        assert canonical_sector(name.upper()) == name        # vendor casing

    @pytest.mark.parametrize("old, new", [
        ("Information Technology", "Technology"),
        ("Consumer Discretionary", "Consumer Cyclical"),
        ("Consumer Staples", "Consumer Defensive"),
        ("Financials", "Financial Services"),
        ("Health Care", "Healthcare"),
        ("Materials", "Basic Materials"),
        ("Software", "Technology"),
    ])
    def test_the_old_gics_spellings_map_to_the_morningstar_name(self, old, new):
        assert canonical_sector(old) == new

    def test_every_alias_lands_on_a_morningstar_name(self):
        assert set(SECTOR_ALIASES.values()) <= set(MORNINGSTAR)
        assert set(SECTOR_ALIASES.values()) == set(MORNINGSTAR)

    def test_unknown_values_are_unclassified(self):
        for raw in ("Bogus", "", None, "Other", 5, "Crypto"):
            assert canonical_sector(raw) == UNCLASSIFIED

    def test_the_gics_tree_and_its_helpers_are_gone(self):
        for name in ("GICS_TREE", "SUBINDUSTRY_PATH", "GICS_INDUSTRY", "INDUSTRY_ALIASES", "official_path",
                     "industry_of", "gics_path4", "resolve_gics"):
            assert not hasattr(_g, name), name


class TestSectorPath:
    def test_the_path_is_sector_then_industry(self):
        assert sector_path("Technology", "Software - Application") == ["Technology", "Software - Application"]

    def test_an_old_gics_sector_is_normalised(self):
        assert sector_path("Information Technology", "Semiconductors") == ["Technology", "Semiconductors"]

    def test_an_empty_sector_reads_unclassified_and_an_empty_industry_stays_empty(self):
        assert sector_path("", "") == [UNCLASSIFIED, ""]
        assert sector_path(None, None) == [UNCLASSIFIED, ""]

    def test_the_legacy_other_is_never_emitted(self):
        assert sector_path("Other", "Other") == [UNCLASSIFIED, UNCLASSIFIED]

    def test_the_industry_is_trimmed_and_kept_as_the_provider_wrote_it(self):
        assert sector_path("Energy", "  Oil & Gas E&P ") == ["Energy", "Oil & Gas E&P"]

    def test_a_path_has_exactly_two_levels(self):
        assert len(sector_path("Energy", "x")) == 2


def test_gicspath_carries_the_sector_and_the_industry_only():
    p = GicsPath("Energy", "Oil & Gas Midstream")
    assert (p.sector, p.industry) == ("Energy", "Oil & Gas Midstream")
    assert [f for f in p.__dataclass_fields__] == ["sector", "industry"]


# ── R3 (#86): the Unclassified label ─────────────────────────────────────────
class TestUnclassifiedLabel:
    def test_the_label(self):
        assert UNCLASSIFIED == "Unclassified"

    def test_is_unclassified_is_true_for_the_label_and_the_legacy_other(self):
        assert is_unclassified("Unclassified") is True
        assert is_unclassified("Other") is True
        assert is_unclassified(" Other ") is True            # stored values may carry whitespace

    @pytest.mark.parametrize("label", ["", None, "Materials", "Other Specialty Retail", "other", 0, "Financials"])
    def test_is_unclassified_is_false_for_everything_else(self, label):
        """An empty value is "no value", not "unknown": ``""`` is NOT unclassified."""
        assert is_unclassified(label) is False

    def test_the_identity_master_uses_the_same_label(self):
        from RevenueChaser.core.identity import gics as identity_gics
        assert identity_gics.UNCLASSIFIED == UNCLASSIFIED

    def test_the_identity_master_uses_the_same_sector_names(self):
        from RevenueChaser.core.identity import gics as identity_gics
        assert tuple(identity_gics.MORNINGSTAR_SECTORS) == tuple(_g.MORNINGSTAR_SECTORS)

    def test_sector_or_unclassified_reads_blank_other_and_the_label_as_unclassified(self):
        for raw in ("", None, "Other", "Unclassified", "   "):
            assert sector_or_unclassified(raw) == UNCLASSIFIED
        assert sector_or_unclassified(" Energy ") == "Energy"
