"""Value Chain board: one 10-K snippet cannot blank the script (ADR-X1 F3, #144)."""
from __future__ import annotations

from RevenueChaser.core.tests.board_js import PROBE, assert_scripts_intact
from RevenueChaser.module_value_chain import report


def test_a_comment_opener_or_placeholder_in_a_snippet_keeps_the_script_intact(tmp_path):
    p = {"as_of": "2026-09-17T00:00:00+00:00",
         "filings": [{"ticker": "NVDA", "name": PROBE, "form": "10-K", "filed": "2026-02-01",
                      "quality": "ok", "market_cap": 1e12, "sector": "Technology", "industry": PROBE},
                     {"ticker": "TSM", "name": PROBE, "form": "20-F", "filed": "2026-02-01",
                      "quality": "ok", "market_cap": 1e12}],
         "edges": [{"src": "NVDA", "dst_name": PROBE, "dst_ticker": "TSM",
                    "relation": "supplier", "snippet": PROBE, "filed": "2026-02-01"}],
         "universe_total": 1, "pending": 0}
    assert_scripts_intact(report.render_value_chain_dashboard(p), tmp_path)


def _board():
    return report.render_value_chain_dashboard({
        "as_of": "2026-09-17T00:00:00+00:00",
        "filings": [{"ticker": "NVDA", "name": "N", "form": "10-K", "filed": "2026-02-01", "quality": "ok",
                     "market_cap": 1e12, "sector": "Technology", "industry": "x"}],
        "edges": [{"src": "NVDA", "dst_name": "TSM", "dst_ticker": "TSM", "relation": "customer",
                   "snippet": "our supplier", "filed": "2026-02-01"}],
        "universe_total": 1, "pending": 0})


def test_df60_chip_follows_edge_direction(tmp_path):
    """DF60 under node: a customer edge never says "customer list"; each direction gets its own text."""
    from RevenueChaser.core.tests.board_js import run_board_js
    tail = """
drawEdges([{src:"NVDA",dst:"TSM",relation:"customer",snippet:"we purchase from our supplier",hop:1},
           {src:"NVDA",dst:"AMD",relation:"supplier",snippet:"our largest customer",hop:1},
           {src:"NVDA",dst:"INTC",relation:"supplier",snippet:"clean text",hop:1}]);
console.log(JSON.stringify(_els["edges"].innerHTML));
"""
    html = run_board_js(_board(), tail, tmp_path)
    parts = html.split('<div class="edge">')[1:]
    by = {p.split("as a possible")[0].split(" names ")[1].strip(): p for p in parts}
    assert "may be a supplier list" in by["TSM"] and "customer list" not in by["TSM"]
    assert "may be a customer list" in by["AMD"] and "supplier list" not in by["AMD"]
    assert "chip warn" not in by["INTC"]


def test_dead_competitor_and_other_styling_is_gone():
    """DF59: focal_graph returns supplier/customer edges only, so the board has no competitor/other styling."""
    html = _board()
    assert "r-competitor" not in html and "r-other" not in html
    assert "competes with" not in html
