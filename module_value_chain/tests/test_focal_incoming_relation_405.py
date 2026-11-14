"""#405: an incoming edge is labelled from the focal company's side, not the filer's.

``focal_graph`` adds other issuers' edges that name the focal company with the **filer's** relation. The legend
defines Supplier as "sells to the focal company" and Customer as "buys from the focal company", so NXPI's filing
naming Apple as its customer drew a green "customer" link on Apple's board, while by the legend NXPI is Apple's
supplier. The filing's own relation stays on the link, because the page's sentence quotes it; ``focal_relation``
is what the chip and the colour read.
"""
from __future__ import annotations

from RevenueChaser.module_value_chain import engine

_NXPI_SELLS_TO_APPLE = {"src": "NXPI", "dst_name": "Apple", "dst_ticker": "AAPL", "relation": "customer",
                        "snippet": "Apple accounted for 11% of our revenue.", "filed": "2026-02-01",
                        "share_pct": 11.0, "source": "xbrl"}
_APPLE_BUYS_FROM_TSM = {"src": "AAPL", "dst_name": "TSMC", "dst_ticker": "TSM", "relation": "supplier",
                        "snippet": "We rely on TSMC.", "filed": "2026-02-01", "share_pct": None,
                        "source": "profile"}


def _link(ticker: str, edges: list[dict], **where) -> dict:
    links = [l for l in engine.focal_graph(edges, ticker, depth=1)["links"]
             if all(l[k] == v for k, v in where.items())]
    assert len(links) == 1, links
    return links[0]


def test_an_incoming_customer_edge_is_a_supplier_of_the_focal_company():
    got = _link("AAPL", [_NXPI_SELLS_TO_APPLE], src="NXPI")
    assert got["incoming"] is True
    assert got["relation"] == "customer"            # what NXPI's filing says
    assert got["focal_relation"] == "supplier"      # what the legend says about Apple
    assert got["share_pct"] == 11.0                 # the filer's share, untouched


def test_the_same_edge_on_the_filers_own_board_is_unchanged():
    got = _link("NXPI", [_NXPI_SELLS_TO_APPLE], src="NXPI")
    assert got["incoming"] is False
    assert (got["relation"], got["focal_relation"]) == ("customer", "customer")


def test_an_outgoing_supplier_edge_keeps_its_relation():
    got = _link("AAPL", [_APPLE_BUYS_FROM_TSM], src="AAPL")
    assert (got["relation"], got["focal_relation"], got["incoming"]) == ("supplier", "supplier", False)
