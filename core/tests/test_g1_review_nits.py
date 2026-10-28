"""Opus review nits on PR 6: N1 (a ``nasdaq`` membership is added to, never replaced) and N2 (an unreadable spine file
never fails a board header)."""
from __future__ import annotations

import duckdb

from RevenueChaser.core import index_registry as ir
from RevenueChaser.core import spine_reads, universe
from RevenueChaser.rcdata import serving


def test_listed_nasdaq_names_join_a_nasdaq_membership(monkeypatch):
    monkeypatch.setattr(spine_reads, "membership",
                        lambda index=None: [{"index_slug": "nasdaq", "ticker": "AAA"},
                                            {"index_slug": "sp500", "ticker": "BBB"}])
    out = ir._spine_members([{"ticker": "CCC", "exchange": "Nasdaq"}])
    assert out["nasdaq"] == {"AAA", "CCC"} and out["sp500"] == {"BBB"}


def test_an_unreadable_spine_file_gives_an_empty_provenance(monkeypatch):
    monkeypatch.setattr(universe, "_spine", lambda: True)

    def broken(kind):
        raise duckdb.IOException("truncated file")
    monkeypatch.setattr(serving, "open_read", broken)
    assert universe.spine_provenance() == {"built_at": None, "source_as_of": None}
