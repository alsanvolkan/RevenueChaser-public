"""Review F3, the board side (G6, build plan PR 13): the stale gate measures the filer, and the card says so.

The old ``edgar_financials`` row was read once and never again (#43 C14), so the 470-day stale gate measured how old
a stored row was, not how old the filer's statements were. On the fundamentals layer the facts are read again on
the day a statement is filed (``rcdata/build/fundamentals/companyfacts``), so the same rule now measures the filer.
The card names the day the newest statement was filed and the day the facts were last read; the summary says where
the statements came from. Off the layer (the old store, the escape hatch) there are no such dates and none is shown.
"""
from __future__ import annotations

from datetime import date, timedelta

from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_fundamentals import pipeline
from RevenueChaser.module_fundamentals.pipeline import build_payload
from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
from RevenueChaser.module_fundamentals.tests.test_fundamental import _m
from RevenueChaser.module_fundamentals.tests.test_fundamentals_w3_386 import _annual_pair, _compute

AS_OF = date(2026, 10, 27)
LAYER_META = {"source": "sec_edgar", "available": True, "layer": True, "newest_filed": "2026-08-01",
              "facts_as_of": "2026-10-27", "extract_version": 2}


def _fresh_pair():
    annual = _annual_pair(year0=2024, year1=2025)
    for rec, end in zip(annual, ("2024-12-31", "2025-12-31")):
        rec["period_ends"] = {"revenue": end}
    return annual


def test_a_row_on_the_layer_names_its_statements(monkeypatch):
    monkeypatch.setattr(pipeline, "_today", lambda: AS_OF)
    annual = _fresh_pair()
    row, drops = _compute(monkeypatch, annual, edgar=lambda t, no_fetch=False: (annual, [], dict(LAYER_META)))
    assert drops == [] and row["statements"] == {"source": "layer", "filed": "2026-08-01", "read": "2026-10-27"}


def test_a_row_off_the_layer_has_no_statement_dates(monkeypatch):
    monkeypatch.setattr(pipeline, "_today", lambda: AS_OF)
    annual = _fresh_pair()
    row, _ = _compute(monkeypatch, annual, edgar=lambda t, no_fetch=False: (annual, [], {"source": "sec_edgar"}))
    assert row["statements"] == {"source": "store", "filed": None, "read": None}


def test_the_stale_gate_reads_the_layers_newest_year(monkeypatch):
    """A filer whose newest annual report ended over 470 days ago is stale on the layer too (the C18 rule)."""
    monkeypatch.setattr(pipeline, "_today", lambda: AS_OF)
    annual = _fresh_pair()
    for rec in annual:
        rec["period_ends"] = {"revenue": (AS_OF - timedelta(days=500)).isoformat()}
    row, drops = _compute(monkeypatch, annual, edgar=lambda t, no_fetch=False: (annual, [], dict(LAYER_META)))
    assert row is None and drops == ["stale_annual"]


def _payload(statements, source="layer"):
    row = {"ticker": "AAA", "name": "Test", "price": 50.0, "market_cap": 1e9, "sector": "Technology",
           "industry": "Software", "themes": [], "earnings_date": None, "period": "Q2/2026 vs Q2/2025",
           "anchor_period_end": "2025-12-31", "statements": statements,
           "metrics": _m(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=50.0, gross_margin=0.35, roic=0.20,
                         net_debt_ebitda=2.0, dist_52w_pct=0.90, rs_6m=0.05, above_200d=True, pe=15.0, pb=2.0)}
    payload = build_payload([row], top_n=10)
    payload["statements_source"] = source
    return payload


def _grid(payload, tmp_path):
    return run_board_js(render_fundamental_dashboard(payload), """
render();
console.log(JSON.stringify({grid: _els['grid'].innerHTML}));
""", tmp_path)["grid"]


def test_the_card_names_the_filing_and_read_days(tmp_path):
    grid = _grid(_payload({"source": "layer", "filed": "2026-08-01", "read": "2026-10-27"}), tmp_path)
    assert "SEC statements filed 2026-08-01, read 2026-10-27" in grid


def test_an_old_store_card_and_a_hostile_date(tmp_path):
    grid = _grid(_payload({"source": "store", "filed": None, "read": None}, source="store"), tmp_path)
    assert "SEC statements filed" not in grid
    grid = _grid(_payload({"source": "layer", "filed": "<img src=x>", "read": None}), tmp_path)
    assert "<img" not in grid and "&lt;img src=x&gt;" in grid


def test_the_page_says_where_the_statements_came_from():
    html = render_fundamental_dashboard(_payload(None))
    assert "the fundamentals layer" in html and "re-read on each filing day" in html
    old = render_fundamental_dashboard(_payload(None, source="store"))
    assert "the fundamentals layer" not in old and "SEC EDGAR" in old
