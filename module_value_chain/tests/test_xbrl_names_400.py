"""#400 10-K package WP5: an XBRL major customer is named by the spine, not only by the hand-kept alias list.

The member's label is its name split into words ("Mc Kesson Corporation"), so the two names are compared without
spaces or legal suffixes. An anonymised customer ("Customer A") and a name two companies share give no edge.
"""
from __future__ import annotations

from RevenueChaser.module_value_chain import aliases, engine

SPINE = [{"ticker": "MCK", "name": "McKesson Corp"}, {"ticker": "COR", "name": "Cencora, Inc."},
         {"ticker": "ABC1", "name": "Acme Holdings"}, {"ticker": "ABC2", "name": "ACME Inc"}]


def _customer(name, member, share=0.22):
    return {"customer": name, "member": member, "share": share, "period_end": "2025-12-31", "form": "10-K",
            "accession": "0000875320-26-000010", "document_date": "2026-02-12"}


def test_a_customer_the_alias_list_does_not_know_is_named_by_the_spine():
    names = aliases.spine_names(SPINE)
    assert aliases.resolve_name("Mc Kesson Corporation", names) == "MCK"
    assert aliases.resolve_name("Customer A", names) is None
    assert aliases.resolve_name("Acme", names) is None                  # two companies share it


def test_the_xbrl_customer_edge_uses_the_spine_name():
    rows = [_customer("Mc Kesson Corporation", "vrtx:McKessonCorporationMember"),
            _customer("Cencora", "vrtx:CencoraMember", 0.18), _customer("Customer A", "vrtx:CustomerAMember")]
    edges = engine._xbrl_customer_edges("VRTX", rows, aliases.spine_names(SPINE))
    assert {(e["dst_ticker"], e["share_pct"]) for e in edges} == {("MCK", 22.0), ("COR", 18.0)}
