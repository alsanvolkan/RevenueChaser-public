"""Name similarity used by the FDIC match."""
from __future__ import annotations

from RevenueChaser.core.identity import names as n


def test_name_score_handles_truncation_and_suffixes():
    assert n.name_score("Bank OZK", "BANK OZK") == 1.0
    assert n.name_score("INTL BUSINESS MACHINES CORP", "International Business Machines") >= 0.9
    assert n.name_score("", "x") is None
    assert n.name_score("Apple Inc", "Microsoft Corp") < 0.6


def test_norm_name_drops_suffixes_and_expands_abbreviations():
    assert n.norm_name("Acme Finl Svcs Inc.") == ["acme", "financial", "services"]
    assert n.norm_name(None) == []
