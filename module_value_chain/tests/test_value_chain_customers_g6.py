"""Value Chain reads ``customer_share`` (G6, build plan PR 20).

The annual report's inline XBRL names its major customers and their share of revenue (``customer_share``, served
by ``sec/filings``). On the fundamentals layer a named customer the alias list knows becomes a ``customer`` edge of
the filer with that share, even when the Item 1 text never says it; a text concentration edge for the same customer
keeps its sentence and gains the share when it stated none; a keyword guess for that name gives way, as it does to
a stated sentence. An anonymised customer ("Customer A") names no company and is no edge. Off the layer nothing
changes. The selection version moves to 3, because a run's edges now carry the XBRL customers.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import fundamentals_layer, rawstore
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_value_chain import engine, history as vc_history
from RevenueChaser.rcdata.build.fundamentals import companyfacts, filings
from RevenueChaser.rcdata.tests import fund_fixture as fx
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401  (fixtures)


def _ten_k_naming_apple() -> str:
    """The fixture's inline-XBRL 10-K with Apple (12% of revenue) and an anonymised Customer B (11%)."""
    html = fx.ixbrl_10k().replace("msft:CustomerAMember", "msft:AppleIncMember")
    return html.replace('contextRef="c-cust24"', 'contextRef="c-custb25"').replace(
        "</ix:resources>", fx._ctx("c-custb25", "2024-07-01", "2025-06-30",
                                   {**fx.CUSTOMER_DIMS, "srt:MajorCustomersAxis": "msft:CustomerBMember"})
        + "</ix:resources>", 1)


@pytest.fixture
def layer(monkeypatch, http_mock):  # noqa: F811
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    monkeypatch.delenv(fundamentals_layer.SOURCE_ENV, raising=False)
    fx.spine()
    fx.sec_routes(http_mock, {"789019": fx.msft_facts()})
    fx.filing_routes(http_mock, {"789019": fx.submissions(789019, fx.MSFT_FILINGS, "MICROSOFT CORP")},
                     {"msft-10k.htm": _ten_k_naming_apple(), "msft-def14a.htm": fx.proxy()})
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")
    fx.run(filings.NAME, mode="backfill", to="2026-10-28")
    assert fundamentals_layer.reads_layer()
    assert {r[0] for r in fx.q("SELECT customer FROM customer_share")} == {"Apple Inc", "Customer B"}
    return http_mock


def _filing(text: str) -> None:
    rawstore.write_rows("chain_filings", [{"ticker": "MSFT", "cik": "789019", "form": "10-K", "filed": "2025-07-30",
                                           "accession": fx.K25, "primary_doc": "u", "item_text": text,
                                           "quality": "ok", "fetched_at": 1.0}])


def _customers(edges, relations=("customer", "supplier")):
    """MSFT's edges to Apple that state a direction (a keyword ``other`` stays beside a stated one, as before)."""
    return [e for e in edges if e["src"] == "MSFT" and e["dst_ticker"] == "AAPL" and e["relation"] in relations]


def test_a_named_xbrl_customer_is_an_edge_with_its_share(layer):
    _filing("We build cloud software for enterprises around the world.")
    (edge,) = _customers(engine.build_edges())
    assert edge["relation"] == "customer" and edge["share_pct"] == pytest.approx(12.0)
    assert edge["filed"] == "2025-07-30" and "12%" in edge["snippet"] and "Apple Inc" in edge["snippet"]
    assert not [e for e in engine.build_edges() if "Customer B" in (e.get("dst_name") or "")]


def test_a_text_concentration_edge_keeps_its_sentence_and_gains_the_share(layer):
    # Two names, two shares, no "respectively": the sentence states no share of Apple's own.
    _filing("Sales to Apple and Dell accounted for 20% and 15% of our net sales.")
    (edge,) = _customers(engine.build_edges())
    assert "Sales to Apple" in edge["snippet"] and edge["share_pct"] == pytest.approx(12.0)


def test_a_stated_share_in_the_text_is_kept(layer):
    _filing("Direct sales to Apple accounted for 29.8% of our net sales.")
    (edge,) = _customers(engine.build_edges())
    assert edge["share_pct"] == pytest.approx(29.8) and "Direct sales to Apple" in edge["snippet"]


def test_a_keyword_guess_gives_way_to_the_xbrl_customer(layer):
    _filing("We compete with suppliers such as Apple in some markets.")
    edges = _customers(engine.build_edges())
    assert [(e["relation"], e["share_pct"]) for e in edges] == [("customer", pytest.approx(12.0))]


def test_off_the_layer_nothing_changes():
    _filing("We build cloud software for enterprises around the world.")
    assert _customers(engine.build_edges(), relations=("customer", "supplier", "other")) == []


def test_the_selection_version_moves_with_the_edges():
    assert vc_history.SELECTION_VERSION == "3"


def test_a_filing_without_text_still_gives_its_xbrl_customers(layer):
    rawstore.write_rows("chain_filings", [{"ticker": "MSFT", "cik": "0000789019", "form": "10-K", "filed": "",
                                           "accession": "", "primary_doc": "", "item_text": "", "quality": "missing",
                                           "fetched_at": 1.0}])
    (edge,) = _customers(engine.build_edges())
    assert edge["share_pct"] == pytest.approx(12.0)


def test_only_one_named_other_company_is_an_edge_and_the_biggest_share_wins():
    rows = [{"customer": "Microsoft Corp", "share": 0.3}, {"customer": "Apple and Dell", "share": 0.25},
            {"customer": "Apple Inc", "share": 0.12, "period_end": "2025-06-30", "source": "wrds/segments"},
            {"customer": "Apple Inc.", "share": 0.10}, {"customer": "Customer A", "share": 0.4}]
    (edge,) = engine._xbrl_customer_edges("MSFT", rows)
    assert (edge["dst_ticker"], edge["share_pct"]) == ("AAPL", 12.0) and "wrds/segments" in edge["snippet"]
