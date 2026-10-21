"""Value Chain extractor fixes from the 2026-09-26 review (S3): incoming edges,
alias hygiene, customer-concentration sentences. No network, no store."""
from __future__ import annotations

import pytest

from RevenueChaser.module_value_chain import aliases, concentration, engine, mentions


def _pairs(text):
    return {(m["dst_ticker"], m["relation"]) for m in mentions.find_mentions(text)}


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
    assert mentions.find_mentions(text) == []


def test_capitalised_company_names_still_match():
    found = _pairs("We purchase memory from Micron Technology, Inc. We also purchase processors "
                   "from Intel. " + "We invest in research and development. " * 6 +
                   "Our customers include Apple and Amazon.")
    assert ("MU", "supplier") in found and ("INTC", "supplier") in found
    assert ("AAPL", "customer") in found and ("AMZN", "customer") in found


def test_all_caps_legal_name_matches():
    assert ("INTC", "supplier") in _pairs("We purchase processors from INTEL CORPORATION.")


def test_hewlett_packard_enterprise_is_hpe_not_hpq():
    found = _pairs("Our customers include Hewlett Packard Enterprise and IBM.")
    assert ("HPE", "customer") in found
    assert not any(t == "HPQ" for t, _ in found)


def test_apple_products_are_not_relations():
    found = _pairs("Customers can pay with Apple Pay at checkout.")
    assert found == {("AAPL", "other")}


def test_same_issuer_is_not_an_edge(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    rawstore.write_rows("chain_filings", [{
        "ticker": "GOOG", "cik": "1", "form": "10-K", "filed": "2026-02-01", "accession": "a",
        "primary_doc": "http://x", "item_text": "Alphabet and Google serve customers. We purchase from TSMC.",
        "quality": "ok"}])
    edges = engine.build_edges()
    assert {e["dst_ticker"] for e in edges} == {"TSM"}


def test_alias_table_has_no_duplicate_keys():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(aliases))
    table = next(n.value for n in ast.walk(tree)
                 if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "ALIASES")
    keys = [k.value for k in table.keys]
    assert len(keys) == len(set(keys))


# ── S3c concentration sentences ────────────────────────────────────────────
def test_concentration_single_customer():
    text = ("In 2025, we served 1.6 million shipping customers daily. In 2025, one customer, "
            "Amazon.com, Inc. and its affiliates, represented approximately 10.6% of our "
            "consolidated revenues, substantially all within U.S. Domestic Package.")
    got = concentration.find(text, "2026-02-01")
    assert [(e["dst_ticker"], e["relation"], e["share_pct"]) for e in got] == [("AMZN", "customer", 10.6)]
    assert "10.6%" in got[0]["snippet"]


def test_concentration_respectively():
    text = ("Our ten largest customers accounted for 72% of our net sales in 2025. Direct sales to "
            "Apple and Qualcomm accounted for 29.8% and 11.1% of our net sales, respectively, for "
            "the year ended December 31, 2025.")
    got = {(e["dst_ticker"], e["share_pct"]) for e in concentration.find(text)}
    assert got == {("AAPL", 29.8), ("QCOM", 11.1)}


def test_concentration_each_of_which():
    text = ("Our two largest revenue customers in fiscal years 2025 were Applied Materials, Inc. and "
            "Lam Research Corporation, each of which accounted for more than 10% of our total revenues.")
    got = {(e["dst_ticker"], e["relation"], e["share_pct"]) for e in concentration.find(text)}
    assert got == {("AMAT", "customer", 10.0), ("LRCX", "customer", 10.0)}


def test_concentration_purchases_are_suppliers():
    text = ("Product purchases from Ingram Micro, Inc., TD Synnex Corporation, and Dell Inc. "
            "accounted for approximately 25%, 23%, and 11%, respectively, of our total product purchases in 2024.")
    got = {(e["dst_ticker"], e["relation"]) for e in concentration.find(text)}
    assert got == {("DELL", "supplier")}


def test_no_share_sentence_no_edge():
    assert concentration.find("Our customers include Apple and Amazon. Revenue grew 12%.") == []


def test_concentration_wins_over_keyword_edge(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    rawstore.write_rows("chain_filings", [{
        "ticker": "AMKR", "cik": "1", "form": "10-K", "filed": "2026-02-01", "accession": "a",
        "primary_doc": "http://x", "quality": "ok",
        "item_text": ("We compete with suppliers such as Apple in some markets. Direct sales to Apple "
                      "accounted for 29.8% of our net sales.")}])
    edges = engine.build_edges()
    aapl = [e for e in edges if e["dst_ticker"] == "AAPL"]
    # The keyword "supplier" guess is replaced; the share sentence has no relation
    # verb of its own, so its keyword mention is a display-only "other" (#61).
    assert [(e["relation"], "29.8%" in e["snippet"]) for e in aapl] == [
        ("customer", True), ("other", True)]
    # D7 (DF57): the stated share rides on the concentration edge into chain_mentions; a keyword edge has none.
    assert [e["share_pct"] for e in aapl] == [29.8, None]
    stored = {(r["dst_ticker"], r["relation"]): r["share_pct"] for r in rawstore.get_all("chain_mentions")}
    assert stored[("AAPL", "customer")] == 29.8 and stored[("AAPL", "other")] is None


def test_concentration_share_rides_on_each_filing_edge():
    """D7 (DF57): ``filing_edges`` keeps the share ``concentration.find`` parsed, per counterparty."""
    text = ("Direct sales to Apple and Qualcomm accounted for 29.8% and 11.1% of our net sales, respectively. "
            "Product purchases from Dell Inc. accounted for approximately 11% of our total product purchases.")
    got = {(e["dst_ticker"], e["relation"]): e["share_pct"] for e in engine.filing_edges("AMKR", text, "2026-02-01")}
    assert got[("AAPL", "customer")] == 29.8 and got[("QCOM", "customer")] == 11.1
    assert got[("DELL", "supplier")] == 11.0
