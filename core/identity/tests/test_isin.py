"""ISO 6166 ISIN derived from a US CUSIP (#127). No network, no store.

The pairs below are the published CUSIP/ISIN of six US common stocks. A CINS (first
character a letter) must not be prefixed with ``US``.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core.identity.isin import from_cusip, luhn_ok

# Hand-checked against the Luhn expansion (A-Z -> 10-35) before this module shipped.
PAIRS = (
    ("037833100", "US0378331005"),  # Apple
    ("594918104", "US5949181045"),  # Microsoft
    ("02079K107", "US02079K1079"),  # Alphabet class C
    ("88160R101", "US88160R1014"),  # Tesla
    ("023135106", "US0231351067"),  # Amazon
    ("30231G102", "US30231G1022"),  # Exxon
)


@pytest.mark.parametrize("cusip,isin", PAIRS)
def test_known_us_cusips_derive_the_published_isin(cusip, isin):
    assert from_cusip(cusip) == isin
    assert luhn_ok(isin)


@pytest.mark.parametrize("cusip,isin", PAIRS)
def test_a_mutated_isin_check_digit_fails_luhn(cusip, isin):
    bad = isin[:-1] + ("0" if isin[-1] != "0" else "1")
    assert bad != isin
    assert luhn_ok(bad) is False


def test_a_mutated_cusip_check_digit_is_not_a_us_isin():
    """Apple's check digit is 0. Flipping it must not invent an ISIN."""
    assert from_cusip("037833101") is None
    assert from_cusip("03783310") is None          # too short
    assert from_cusip("0378331000") is None        # too long


def test_a_cins_does_not_get_a_us_prefix():
    """A CUSIP-like string whose first character is a letter is a CINS, not a US CUSIP."""
    assert from_cusip("G12345678") is None
    assert from_cusip("S02079K10") is None
    assert from_cusip("  G12345678") is None       # padding does not turn a CINS into a US CUSIP


def test_blank_and_non_strings_are_none():
    assert from_cusip(None) is None
    assert from_cusip("") is None
    assert from_cusip("   ") is None


def test_lowercase_is_accepted_when_the_cusip_is_otherwise_valid():
    assert from_cusip("02079k107") == "US02079K1079"


def test_luhn_ok_is_false_for_characters_outside_the_alphabet_instead_of_raising():
    """A body that is not A-Z/0-9 (a stray symbol or a non-Latin letter) is not an ISIN, and must not raise."""
    for bad in ("US037833-1005", "US0378\u0416\u04161005", "US\u00e9378331005"):
        assert luhn_ok(bad) is False
