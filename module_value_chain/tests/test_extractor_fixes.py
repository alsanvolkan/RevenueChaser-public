"""Value Chain extractor fixes from the 2026-09-26 review (S3): incoming edges,
alias hygiene. No network, no store. (The text scans these once drove are gone, #748.)"""
from __future__ import annotations

import pytest

from RevenueChaser.module_value_chain import aliases, engine


def _tickers(text):
    """The companies the alias table names in ``text`` (a product mention such as "Apple Pay" names none)."""
    return {h["ticker"] for h in aliases.find_aliases(text) if not h["context_only"]}


# ── S3a incoming edges ─────────────────────────────────────────────────────
def test_focal_graph_shows_incoming_edges():
    edges = [{"src": "NVDA", "dst_name": "Tsmc", "dst_ticker": "TSM", "relation": "supplier",
              "snippet": "we use TSMC", "filed": "2026-02-01"}]
    g = engine.focal_graph(edges, "TSM")
    assert [(l["src"], l["dst"], l["hop"], l["incoming"]) for l in g["links"]] == [
        ("NVDA", "TSM", 1, True)]
    out = engine.focal_graph(edges, "NVDA")
    assert out["links"][0]["incoming"] is False


def test_incoming_edges_do_not_duplicate_or_seed_hop2():
    edges = [
        {"src": "NVDA", "dst_name": "Tsmc", "dst_ticker": "TSM", "relation": "supplier", "snippet": ""},
        {"src": "AAPL", "dst_name": "Tsmc", "dst_ticker": "TSM", "relation": "supplier", "snippet": ""},
        {"src": "AAPL", "dst_name": "Qualcomm", "dst_ticker": "QCOM", "relation": "supplier", "snippet": ""},
        {"src": "TSM", "dst_name": "Tsmc", "dst_ticker": "TSM", "relation": "other", "snippet": ""},
    ]
    g = engine.focal_graph(edges, "TSM")
    got = sorted((l["src"], l["dst"], l["hop"]) for l in g["links"])
    assert got == [("AAPL", "TSM", 1), ("NVDA", "TSM", 1)]


def test_picker_includes_destination_only_tickers():
    from RevenueChaser.module_value_chain import report
    html = report.render_value_chain_dashboard({
        "as_of": "2026-09-17T00:00:00+00:00",
        "filings": [{"ticker": "NVDA", "name": "NVIDIA Corporation", "form": "10-K",
                     "filed": "2026-02-01", "quality": "ok", "market_cap": 1e12}],
        "edges": [{"src": "NVDA", "dst_name": "Tsmc", "dst_ticker": "TSM", "relation": "supplier",
                   "snippet": "we use TSMC", "filed": "2026-02-01"}],
    })
    assert '"ticker": "TSM"' in html
    assert '"TSM": {"ticker": "TSM"' in html          # a graph is embedded for it


# ── S3b alias hygiene ──────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "Our customers value a regular cadence of product launches and supplier reviews.",
    "We run packaging programs such as our Avo Intel line for customers.",
    "Customers rely on our sub-micron tolerances from suppliers.",
    "Our salesforce sells to customers directly.",
    "Suppliers in the Amazon region require community approval.",
    "Customers rely on coherent optical systems from suppliers.",
    "Private Capital Intel provides customers with data.",
    "Our Intel Graph helps customers hunt threats.",
])
def test_common_words_are_not_companies(text):
    assert _tickers(text) == set()


def test_capitalised_company_names_still_match():
    found = _tickers("We purchase memory from Micron Technology, Inc. We also purchase processors "
                     "from Intel. " + "We invest in research and development. " * 6 +
                     "Our customers include Apple and Amazon.")
    assert {"MU", "INTC", "AAPL", "AMZN"} <= found


def test_all_caps_legal_name_matches():
    assert "INTC" in _tickers("We purchase processors from INTEL CORPORATION.")


def test_hewlett_packard_enterprise_is_hpe_not_hpq():
    found = _tickers("Our customers include Hewlett Packard Enterprise and IBM.")
    assert "HPE" in found and "HPQ" not in found


def test_apple_products_are_not_relations():
    assert _tickers("Customers can pay with Apple Pay at checkout.") == set()


def test_alias_table_has_no_duplicate_keys():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(aliases))
    table = next(n.value for n in ast.walk(tree)
                 if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "ALIASES")
    keys = [k.value for k in table.keys]
    assert len(keys) == len(set(keys))
