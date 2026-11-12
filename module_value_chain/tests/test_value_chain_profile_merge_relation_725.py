"""#725: ``merge_profile`` keys the profile's edges by (counterparty, relation), not by counterparty alone.

``chain_mentions`` is keyed ``(src, dst_name, relation)``, so one company can be both a customer and a competitor
of the same filer. Keying only by the company made the profile's first edge swallow every other relation of that
company: an XBRL customer edge (with its share of revenue) vanished whenever the profile named the same company as
a competitor, and a profile edge in a second relation could never pick up a stated share.
"""
from __future__ import annotations

from RevenueChaser.module_value_chain import engine

_XBRL_CUSTOMERS = [{"customer": "Intel", "share": 0.2, "period_end": "2025-12-31",
                    "document_date": "2026-02-01", "form": "10-K", "accession": "a"}]


def _by_key(edges: list[dict]) -> dict[tuple[str, str], dict]:
    return {(e["dst_ticker"] or e["dst_name"], e["relation"]): e for e in edges}


def test_an_xbrl_customer_survives_a_profile_that_calls_the_same_company_a_competitor():
    profile = {"named_competitors": ["Intel"]}
    regex = engine.filing_edges("AMKR", "", "2026-02-01", _XBRL_CUSTOMERS)
    assert _by_key(regex)[("INTC", "customer")]["share_pct"] == 20.0      # the input really holds it

    got = _by_key(engine.merge_profile(regex, engine.profile_edges("AMKR", profile, "2026-02-01")))
    assert set(got) == {("INTC", "competitor"), ("INTC", "customer")}
    assert got[("INTC", "customer")]["source"] == "xbrl"
    assert got[("INTC", "customer")]["share_pct"] == 20.0
    assert got[("INTC", "competitor")]["source"] == "profile"


def test_the_profile_still_wins_and_takes_the_share_in_the_same_relation():
    profile = {"customers": [{"name": "Intel", "share": None, "quote": "Intel is our largest"}]}
    got = _by_key(engine.merge_profile(engine.filing_edges("AMKR", "", "2026-02-01", _XBRL_CUSTOMERS),
                                       engine.profile_edges("AMKR", profile, "2026-02-01")))
    assert set(got) == {("INTC", "customer")}
    assert (got[("INTC", "customer")]["source"], got[("INTC", "customer")]["share_pct"]) == ("profile", 20.0)


def test_each_relation_of_one_company_takes_its_own_stated_share():
    """The profile names Intel as a customer and as a supplier; only the customer side has an XBRL share."""
    profile = {"customers": [{"name": "Intel", "share": None, "quote": "q1"}],
               "suppliers": [{"name": "Intel", "share": None, "quote": "q2"}]}
    got = _by_key(engine.merge_profile(engine.filing_edges("AMKR", "", "2026-02-01", _XBRL_CUSTOMERS),
                                       engine.profile_edges("AMKR", profile, "2026-02-01")))
    assert set(got) == {("INTC", "customer"), ("INTC", "supplier")}
    assert got[("INTC", "customer")]["share_pct"] == 20.0
    assert got[("INTC", "supplier")]["share_pct"] is None


def test_a_sentence_share_reaches_the_second_relation_of_the_same_company():
    """A supplier edge of the profile takes the share a sentence states for that supplier, even though the profile
    names the same company as a customer first (``setdefault`` used to hide the supplier row)."""
    text = "Our purchases from Intel accounted for 11.5% of our total product purchases."
    regex = engine.filing_edges("AMKR", text, "2026-02-01")
    assert _by_key(regex)[("INTC", "supplier")]["share_pct"] == 11.5     # the sentence really states it

    profile = {"customers": [{"name": "Intel", "share": None, "quote": "q1"}],
               "suppliers": [{"name": "Intel", "share": None, "quote": "q2"}]}
    got = _by_key(engine.merge_profile(regex, engine.profile_edges("AMKR", profile, "2026-02-01")))
    assert set(got) == {("INTC", "customer"), ("INTC", "supplier")}
    assert got[("INTC", "supplier")]["share_pct"] == 11.5
    assert got[("INTC", "supplier")]["source"] == "profile"
