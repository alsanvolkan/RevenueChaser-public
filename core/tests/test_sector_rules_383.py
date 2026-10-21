"""Common C17 helper (#383 R1): Financial Services and Real Estate are not comparable, from the spine sector.

Decided on Screeners card 4 (avo, 2026-10-10 22:41 Amsterdam): the rule reads the spine's sector
(``equity_universe.sector`` through ``spine_gics``), never a vendor's. Helper only: Fundamentals excludes these
names and AG flags them in their own waves.
"""
from __future__ import annotations

from RevenueChaser.core import rawstore, sector_rules
from RevenueChaser.core.gics_map import SECTORS_11


def _spine(rows):
    rawstore.write_rows("equity_universe", [
        {"ticker": t, "sector": s, "industry": "", "name": t, "market_cap": 1e9} for t, s in rows])


def test_the_two_sectors_are_not_comparable():
    assert sector_rules.NOT_COMPARABLE_SECTORS == frozenset({"Financial Services", "Real Estate"})
    assert sector_rules.NOT_COMPARABLE_SECTORS <= set(SECTORS_11)
    for sector in SECTORS_11:
        want = sector_rules.NOT_COMPARABLE if sector in ("Financial Services", "Real Estate") \
            else sector_rules.COMPARABLE
        assert sector_rules.sector_status(sector) == want


def test_an_older_spelling_counts_as_its_morningstar_name():
    assert sector_rules.sector_status("Financials") == sector_rules.NOT_COMPARABLE
    assert sector_rules.sector_status("") == sector_rules.COMPARABLE         # Unclassified: not excluded here


def test_the_spine_sector_decides_not_the_vendors(rawstore_tmp_db):
    _spine([("REIT", "Real Estate"), ("TECH", "Technology"), ("BANK", "Financial Services")])
    vendor = {"REIT": "Technology", "TECH": "Real Estate", "BANK": "Technology"}   # what .info would say
    assert sector_rules.not_comparable(list(vendor)) == {"REIT": "Real Estate", "BANK": "Financial Services"}
    assert sector_rules.ticker_status("REIT") == sector_rules.NOT_COMPARABLE
    assert sector_rules.ticker_status("TECH") == sector_rules.COMPARABLE


def test_a_name_off_the_spine_is_comparable(rawstore_tmp_db):
    _spine([("REIT", "Real Estate")])
    assert sector_rules.ticker_status("NOPE") == sector_rules.COMPARABLE
    assert sector_rules.not_comparable(["NOPE"]) == {}


def test_a_passed_spine_is_used_as_is(rawstore_tmp_db):
    from RevenueChaser.core.gics_map import GicsPath
    spine = {"X": GicsPath("Real Estate", "REIT - Office")}
    assert sector_rules.ticker_status("x", spine) == sector_rules.NOT_COMPARABLE
    assert sector_rules.not_comparable(["X", "Y"], spine) == {"X": "Real Estate"}
