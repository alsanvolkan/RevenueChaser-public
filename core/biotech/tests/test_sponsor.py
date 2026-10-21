"""Table-driven tests for :func:`core.biotech.sponsor.normalise_sponsor`."""
from __future__ import annotations

import pytest

from RevenueChaser.core.biotech.sponsor import normalise_sponsor

CASES = [
    ("Tenaya Therapeutics, Inc.", "tenaya therapeutics"),
    ("Cellectis S.A.", "cellectis"),
    ("VERTEX PHARMACEUTICALS INC / MA", "vertex pharmaceuticals"),
    ("Quince Therapeutics S.p.A.", "quince therapeutics"),
    ("Scholar Rock Holding Corp", "scholar rock"),
    ("Immunocore Holdings plc", "immunocore"),
    ("Surrozen, Inc./DE", "surrozen"),
    ("4D Molecular Therapeutics, Inc.", "4d molecular therapeutics"),
    ("", ""),
    ("BRISTOL MYERS SQUIBB CO", "bristol myers squibb"),
    ("Bristol-Myers Squibb Company", "bristol myers squibb"),
    ("ELI LILLY & Co", "eli lilly"),
    ("Eli Lilly and Company", "eli lilly"),
    ("ACME BIO INC /DE/", "acme bio"),
    ("Théa Pharma", "thea pharma"),
    ("AB Science", "ab science"),
    ("Genmab A/S", "genmab"),
    ("Y-mAbs Therapeutics, Inc.", "y mabs therapeutics"),
    ("The Medicines Company", "medicines"),
    ("JOHNSON & JOHNSON", "johnson and johnson"),
    ("Johnson and Johnson", "johnson and johnson"),
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_normalise_sponsor(raw, expected):
    assert normalise_sponsor(raw) == expected
