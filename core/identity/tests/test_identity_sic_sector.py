"""The SIC -> sector fallback table (issue #272): the explicit 263-code table #265 deleted, written in the 11
Morningstar names, and the raw-SIC helpers the master uses."""
from __future__ import annotations

from collections import Counter

import pytest

from RevenueChaser.core.gics_map import MORNINGSTAR_SECTORS, canonical_sector
from RevenueChaser.core.identity import sic_sector


def test_the_table_holds_the_263_explicit_codes():
    assert len(sic_sector.SIC_SECTOR) == 263
    assert all(isinstance(c, int) and 0 < c <= 9999 for c in sic_sector.SIC_SECTOR)


def test_no_code_is_listed_under_two_sectors():
    listed = [c for codes in sic_sector._BY_SECTOR.values() for c in codes]
    assert len(listed) == len(set(listed)) == len(sic_sector.SIC_SECTOR)


def test_every_value_is_one_of_the_eleven_morningstar_names():
    assert set(sic_sector.SIC_SECTOR.values()) <= set(MORNINGSTAR_SECTORS)
    assert set(sic_sector.SIC_SECTOR.values()) == set(MORNINGSTAR_SECTORS), "each of the 11 names has codes"
    for sector in sic_sector.SIC_SECTOR.values():
        assert canonical_sector(sector) == sector, "a name the canonical map would rename is not stored"


def test_the_codes_per_sector_are_those_of_the_deleted_table_mapped_through_canonical_sector():
    """Counts measured on core/sic_gics.py at 570ab1d^ (_EXPLICIT) after canonical_sector (#272)."""
    assert Counter(sic_sector.SIC_SECTOR.values()) == {
        "Consumer Cyclical": 56, "Technology": 33, "Consumer Defensive": 31, "Healthcare": 25, "Basic Materials": 24,
        "Financial Services": 23, "Industrials": 21, "Communication Services": 21, "Energy": 13, "Real Estate": 8,
        "Utilities": 8}


@pytest.mark.parametrize("code, sector", [
    (7372, "Technology"), (3674, "Technology"), (3571, "Technology"),
    (6022, "Financial Services"), (6021, "Financial Services"), (6770, "Financial Services"),
    (2834, "Healthcare"), (3841, "Healthcare"), (8062, "Healthcare"),
    (4911, "Utilities"), (4931, "Utilities"), (1311, "Energy"), (2911, "Energy"),
    (5331, "Consumer Cyclical"), (3711, "Consumer Cyclical"), (5812, "Consumer Cyclical"),
    (2000, "Consumer Defensive"), (5411, "Consumer Defensive"), (100, "Consumer Defensive"),
    (6798, "Real Estate"), (4813, "Communication Services"), (4731, "Industrials"), (8742, "Industrials"),
    (2810, "Basic Materials"), (1040, "Basic Materials")])
def test_known_codes_map_to_their_sector(code, sector):
    assert sic_sector.sector_for(code) == sector
    assert sic_sector.sector_for(f"{code:04d}") == sector, "the SEC writes the code as a 4-digit string"


@pytest.mark.parametrize("code", [7389, 1000, 6500, 1400, 4400, 4700, 8880, 3990, 9999, 5555])
def test_a_code_that_is_not_listed_is_unknown_not_a_guess(code):
    """The division-range guesses were removed in #86: 7389, 6500 ... stay unmapped."""
    assert sic_sector.sector_for(code) is None


@pytest.mark.parametrize("value", [None, "", "  ", "abc", "nan", "None", 0, "0000", -5, 12345, 7.5e9, True])
def test_a_blank_or_malformed_code_has_no_sector(value):
    assert sic_sector.sector_for(value) is None


def test_the_code_is_read_from_text_and_numbers_alike():
    for value in (7372, "7372", " 7372 ", 7372.0):
        assert sic_sector.sector_for(value) == "Technology"


# -- the raw SIC columns ---------------------------------------------------------------------------
@pytest.mark.parametrize("value, four, group2", [
    ("2834", "2834", "28"), (3841, "3841", "38"), ("0100", "0100", "01"), (100, "0100", "01"),
    ("6770", "6770", "67"), ("  7372", "7372", "73"), (9995, "9995", "99")])
def test_a_code_is_four_digits_and_its_group_is_the_two_digit_major_group(value, four, group2):
    assert sic_sector.parts(value) == (four, group2)


@pytest.mark.parametrize("value", [None, "", "x", 0, "0000", 10000, -1, False])
def test_parts_of_nothing_is_none(value):
    assert sic_sector.parts(value) is None


# -- which code a ticker has ------------------------------------------------------------------------
def test_the_code_is_the_submissions_row_first_then_the_stores():
    assert sic_sector.code_from({"sec": {"sic": "3841"}, "sic_store": "2834"}) == (3841, "sec_submissions")
    assert sic_sector.code_from({"sec": {"sic": ""}, "sic_store": "2834"}) == (2834, "sec_company_sic")
    assert sic_sector.code_from({"sec": None, "sic_store": "0100"}) == (100, "sec_company_sic")
    assert sic_sector.code_from({"sec": {"sic": "6022"}}) == (6022, "sec_submissions")


def test_no_code_in_either_place_is_none():
    for inputs in ({}, {"sec": None}, {"sec": {}, "sic_store": None}, {"sec": {"sic": "x"}, "sic_store": "0000"}):
        assert sic_sector.code_from(inputs) is None


def test_a_submissions_code_the_table_does_not_list_does_not_fall_through_to_the_stores():
    assert sic_sector.code_from({"sec": {"sic": "7389"}, "sic_store": "2834"}) == (7389, "sec_submissions")
