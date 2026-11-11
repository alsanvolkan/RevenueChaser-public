"""#410 part 2: the Relatedness profile's customers, suppliers and competitors are Value Chain edges.

Each named company becomes a ``source = "profile"`` edge with its quote and stated share, resolved to a ticker
through ``aliases.resolve``; a name it cannot resolve stays as text. For a counterparty the profile names, the
profile's edge wins over a sentence, keyword or XBRL edge (and takes their share when it states none); a filer
without a profile keeps the regex edges.
"""
from __future__ import annotations

import json

from RevenueChaser.core import rawstore
from RevenueChaser.module_value_chain import aliases, engine

TEXT = "Direct sales to Apple accounted for 29.8% of our net sales. We compete with Intel in some markets."
PROFILE = {"customers": [{"name": "Apple Inc.", "type": "company", "share": None, "quote": "Apple is our largest"},
                         {"name": "Acme Widgets", "type": "company", "share": 0.12, "quote": "Acme 12%"}],
           "suppliers": [{"name": "Taiwan Semiconductor", "type": "company", "quote": "TSMC makes our chips"}],
           "named_competitors": ["Amkor Technology", "Intel"]}


def _filing(ticker="AMKR", text=TEXT):
    rawstore.write_rows("chain_filings", [{"ticker": ticker, "cik": "1", "form": "10-K", "filed": "2026-02-01",
                                           "accession": "a", "primary_doc": "u", "item_text": text,
                                           "quality": "ok", "fetched_at": 1.0}])


def _profile(ticker="AMKR", profile=PROFILE):
    rawstore.write_rows("relatedness_profiles", [{"ticker": ticker, "cik": "1", "model": "m", "prompt_version": "v3b",
                                                  "fetched_at": 1.0, "cache_key": "k", "status": "ok",
                                                  "profile_json": json.dumps(profile)}])


def test_profile_edges_resolve_names_and_keep_unresolved_ones_as_text():
    names = aliases.spine_names([{"ticker": "AMKR", "name": "Amkor Technology Inc"}])
    got = {(e["dst_name"], e["dst_ticker"], e["relation"], e["share_pct"])
           for e in engine.profile_edges("AMKR", PROFILE, "2026-02-01", names)}
    assert got == {("Apple Inc.", "AAPL", "customer", None), ("Acme Widgets", "", "customer", 12.0),
                   ("Taiwan Semiconductor", "TSM", "supplier", None), ("Intel", "INTC", "competitor", None)}


def test_the_profile_wins_per_counterparty_and_takes_the_stated_share():
    _filing()
    _profile()
    edges = [e for e in engine.build_edges() if e["src"] == "AMKR"]
    apple = [e for e in edges if e["dst_ticker"] == "AAPL"]
    assert [(e["source"], e["relation"], e["share_pct"], e["snippet"]) for e in apple] == [
        ("profile", "customer", 29.8, "Apple is our largest")]
    assert {e["source"] for e in edges} == {"profile"}          # Intel too: the profile names it
    assert all(e["source"] == "profile" for e in rawstore.get_all("chain_mentions") if e["src"] == "AMKR")


def test_a_filer_without_a_profile_keeps_its_regex_edges():
    _filing("NVTS")
    _profile()                                                    # AMKR's profile, not NVTS's
    edges = engine.build_edges()
    assert edges and {e["source"] for e in edges} == {"regex"}


def test_the_board_names_the_source_of_a_link():
    edges = engine.profile_edges("AMKR", PROFILE, "2026-02-01")
    assert {l["source"] for l in engine.focal_graph(edges, "AMKR")["links"]} == {"profile"}
