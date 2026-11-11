"""Value Chain reads the shared SEC layer (plan 2026-11-11 WP2a, #410 part 1).

``chain_filings`` held 377 filings; the layer (``sec/filings``, ``filing_item``) holds every company's newest annual
report. On the layer the edges come from the layer's business section, the board's filing list is the layer's, and a
Fetch downloads nothing into ``chain_filings`` (Relatedness's Fetch calls the same ``fetch_corpus``). Off the layer
nothing changes. A free-text company name resolves to one ticker through the alias rules, then the spine.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import fundamentals_layer, rawstore
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_value_chain import aliases, engine
from RevenueChaser.rcdata.build.fundamentals import companyfacts, filings
from RevenueChaser.rcdata.tests import fund_fixture as fx
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401  (fixtures)

STATED = "Direct sales to Apple accounted for 29.8% of our net sales. "


@pytest.fixture
def layer(monkeypatch, http_mock):  # noqa: F811
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    fx.spine()
    fx.sec_routes(http_mock, {"789019": fx.msft_facts()})
    ten_k = fx.ten_k().replace("<p>Item 1. Business</p><p>", "<p>Item 1. Business</p><p>" + STATED * 40, 1)
    fx.filing_routes(http_mock, {"789019": fx.submissions(789019, fx.MSFT_FILINGS, "MICROSOFT CORP")},
                     {"msft-10k.htm": ten_k, "msft-def14a.htm": fx.proxy()})
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")
    fx.run(filings.NAME, mode="backfill", to="2026-10-28")
    assert fundamentals_layer.reads_layer()
    monkeypatch.setattr(engine, "_default_tickers", lambda: ["MSFT", "ORCL"])
    return http_mock


def test_the_edges_come_from_the_layers_text(layer):
    # A keyword ``other`` stays beside a stated edge (test_value_chain_customers_g6); the stated one is the point.
    edges = [e for e in engine.build_edges() if e["src"] == "MSFT" and e["dst_ticker"] == "AAPL"
             and e["relation"] in ("customer", "supplier")]
    assert [(e["relation"], e["share_pct"]) for e in edges] == [("customer", pytest.approx(29.8))]
    assert rawstore.get_all("chain_filings") == []


def test_the_board_lists_the_layers_filings_without_text(layer):
    (row,) = engine._board_filings_rows()
    assert (row["ticker"], row["form"], row["filed"], row["accession"]) == ("MSFT", "10-K", "2025-07-30", fx.K25)
    assert "item_text" not in row and "text" not in row


def test_a_fetch_downloads_nothing_on_the_layer(layer):
    got = engine.fetch_corpus()
    assert (got["corpus"], got["cached"], got["pending"], got["fetched"]) == ("layer", 1, 1, 0)
    assert rawstore.get_all("chain_filings") == []


def test_the_layer_is_not_the_corpus_before_sec_filings_has_built(layer, monkeypatch):
    from RevenueChaser.rcdata import ops
    built = ops.ever_built
    monkeypatch.setattr(ops, "ever_built", lambda name: name != filings.NAME and built(name))
    assert fundamentals_layer.reads_layer() and engine._board_filings_rows() == []
    assert engine.fetch_corpus(no_fetch=True).get("corpus") is None


def test_off_the_layer_the_corpus_is_chain_filings():
    rawstore.write_rows("chain_filings", [{"ticker": "MSFT", "cik": "789019", "form": "10-K", "filed": "2025-07-30",
                                           "accession": "a", "primary_doc": "u", "item_text": STATED,
                                           "quality": "ok", "fetched_at": 1.0}])
    assert [r["ticker"] for r in engine._board_filings_rows()] == ["MSFT"]
    assert any(e["dst_ticker"] == "AAPL" for e in engine.build_edges())


SPINE = aliases.spine_names([{"ticker": "MCK", "name": "McKesson Corp"}, {"ticker": "ABC1", "name": "Acme Holdings"},
                             {"ticker": "ABC2", "name": "ACME Inc"}])


@pytest.mark.parametrize("name, ticker", [
    ("Apple Inc.", "AAPL"),                 # the alias list
    ("Mc Kesson Corporation", "MCK"),       # the spine
    ("Apple Pay", None),                    # a product, not a company
    ("Apple and Dell", None),               # two companies
    ("Acme", None),                         # a name two issuers share
    ("Customer A", None),
    ("", None),
])
def test_resolve(name, ticker):
    assert (aliases.resolve(name, SPINE) or {}).get("ticker") == ticker


def test_resolve_rate_counts_each_field():
    profiles = {"VRTX": {"customers": [{"name": "Mc Kesson Corporation"}, {"name": "Customer A"}],
                         "suppliers": [{"name": "Lonza"}], "named_competitors": ["Apple"]},
                "X": {"customers": [], "suppliers": None}}
    got = aliases.resolve_rate(profiles, SPINE)
    assert got["customers"] == {"names": 2, "resolved": 1, "rate": 0.5}
    assert got["suppliers"] == {"names": 1, "resolved": 0, "rate": 0.0}
    assert got["named_competitors"]["resolved"] == 1
    assert got["all"] == {"names": 4, "resolved": 2, "rate": 0.5}
