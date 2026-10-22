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
    # EDGAR's split suffix: "A S" must fold like "A/S".
    ("NOVO NORDISK A S", "novo nordisk"),
    ("Novo Nordisk A/S", "novo nordisk"),
    # Characters NFKD deletes instead of folding.
    ("Strauß Pharma GmbH", "strauss pharma"),
    ("Ørsted A/S", "orsted"),
    ("Łukasiewicz Pharma Sp. z o.o.", "lukasiewicz pharma sp z oo"),
    ("Æterna Zentaris Inc", "aeterna zentaris"),
    ("Œuvres Sanofi SA", "oeuvres sanofi"),
    ("Þór Biotech ehf", "thor biotech ehf"),
    ("Đuro Đaković Pharma", "duro dakovic pharma"),
    ("Ħal Pharma Ltd", "hal pharma"),
    ("Ŧest Labs Co", "test labs"),
    ("Sørensen Pharma", "sorensen pharma"),
    # Legal-entity suffixes added with the OOPD orphan-designation provider.
    ("Novo Nordisk SAS", "novo nordisk"),
    ("CSL Pty Ltd", "csl"),
    ("Chiesi Farmaceutici S.r.l.", "chiesi farmaceutici"),
    ("Fresenius KGaA", "fresenius"),
    # Regression: a suffix-looking token that leads the name survives, and a
    # single trailing letter is not a split suffix.
    ("AB Science", "ab science"),
    ("AB Science S.A.", "ab science"),
    ("Nova S", "nova s"),
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_normalise_sponsor(raw, expected):
    assert normalise_sponsor(raw) == expected


def test_edgar_split_suffix_folds_like_the_slash_form():
    assert normalise_sponsor("NOVO NORDISK A S") == normalise_sponsor("Novo Nordisk A/S")
