"""Value Chain: the edge list escapes every field it writes into ``innerHTML`` (ADR-X1, #175 review).

``drawEdges`` put ``l.relation`` into a ``class`` attribute and into the badge text raw. The
store's relation column is an enum today, so nothing reached it, but a ninth ``rcEsc`` call
site is cheaper than the next review finding. The real script runs under node.
"""
from __future__ import annotations

import json

from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_value_chain import report

EVIL = "\"'><img src=x onerror=__x(1)>"
ESCAPED = "&lt;img src=x onerror=__x(1)&gt;"


def _page() -> str:
    p = {"as_of": "2026-09-17T00:00:00+00:00",
         "filings": [{"ticker": "NVDA", "name": "NVIDIA", "form": "10-K", "filed": "2026-02-01",
                      "quality": "ok", "market_cap": 1e12, "sector": "Technology",
                      "industry": "Software - Application"},
                     {"ticker": "TSM", "name": "TSMC", "form": "20-F", "filed": "2026-02-01",
                      "quality": "ok", "market_cap": 1e12}],
         "edges": [{"src": "NVDA", "dst_name": "TSMC", "dst_ticker": "TSM", "relation": "supplier",
                    "snippet": "we buy wafers from TSMC", "filed": "2026-02-01"}],
         "universe_total": 1, "pending": 0}
    return report.render_value_chain_dashboard(p)


def _edges(tmp_path, link: dict) -> str:
    tail = """
drawEdges([%s]);
console.log(JSON.stringify(document.getElementById('edges').innerHTML));
""" % json.dumps(link)
    return run_board_js(_page(), tail, tmp_path)


def test_a_benign_edge_renders_its_relation_as_before(tmp_path):
    html = _edges(tmp_path, {"src": "NVDA", "dst": "TSM", "relation": "supplier", "hop": 1,
                             "snippet": "we buy wafers"})
    assert '<span class="rel r-supplier">supplier</span>' in html


def test_the_relation_is_escaped_in_the_class_and_in_the_text(tmp_path):
    html = _edges(tmp_path, {"src": "NVDA", "dst": "TSM", "relation": EVIL, "hop": 1, "snippet": "s"})
    assert "<img" not in html
    assert 'class="rel r-&quot;&#39;&gt;' in html           # attribute: the quote cannot end it
    assert html.count(ESCAPED) == 2                         # attribute value and badge text


def test_the_other_fields_of_an_edge_stay_escaped(tmp_path):
    html = _edges(tmp_path, {"src": EVIL, "dst": EVIL, "relation": "supplier", "hop": 1, "snippet": EVIL})
    assert "<img" not in html and ESCAPED in html
