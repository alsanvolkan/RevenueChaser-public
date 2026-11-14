"""#729: a display-only ``other`` mention is dropped once the filing states any relation for that company.

``chain_mentions`` is keyed ``(src, dst_name, relation)``, so an ``other`` row stored beside a stated customer
row draws the same company twice on the board -- once with its relation and share, once relation-less. The guard
in ``filing_edges`` dropped a keyword mention only when its relation was ``supplier`` or ``customer``, and
``other`` means ``mentions.find_mentions`` found no relation verb near the name, which is display-only by its own
docstring. A ``competitor`` mention is a real second relation (#725) and stays.
"""
from __future__ import annotations

from RevenueChaser.module_value_chain import concentration, engine, mentions

# "sales to" is not one of ``mentions._CUSTOMER``'s alternatives, so the same sentence that states the
# relation outright gives a relation-less mention of the same company.
_STATED = "Direct sales to Apple accounted for 29.8% of our net sales."


def _keys(edges: list[dict]) -> set[tuple[str, str]]:
    return {(e["dst_ticker"] or e["dst_name"], e["relation"]) for e in edges}


def test_the_inputs_really_disagree():
    """Without this the test below could pass for the wrong reason."""
    assert [(c["dst_ticker"], c["relation"]) for c in concentration.find(_STATED, "2026-02-01")] \
        == [("AAPL", "customer")]
    assert [(m["dst_ticker"], m["relation"]) for m in mentions.find_mentions(_STATED, "2026-02-01")] \
        == [("AAPL", "other")]


def test_an_other_mention_of_a_stated_company_is_not_an_edge():
    edges = engine.filing_edges("AMKR", _STATED, "2026-02-01")
    assert _keys(edges) == {("AAPL", "customer")}
    assert [e["share_pct"] for e in edges] == [29.8]


def test_a_competitor_mention_of_a_stated_customer_survives():
    text = _STATED + " We compete with Apple in wearables."
    assert _keys(engine.filing_edges("AMKR", text, "2026-02-01")) == {("AAPL", "customer"), ("AAPL", "competitor")}


def test_an_other_mention_of_a_company_the_filing_does_not_state_survives():
    text = "Our revenue grew. Apple is named here without a verb."
    assert _keys(engine.filing_edges("AMKR", text, "2026-02-01")) == {("AAPL", "other")}
