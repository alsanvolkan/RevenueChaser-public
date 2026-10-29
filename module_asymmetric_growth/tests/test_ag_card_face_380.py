"""#380 AG W1 S3 card face (#66 provenance line, DF78, AG-4, AG-7).

- The card says which filings it read: the anchor fiscal year and its prior year, the three revenue period ends
  (anchor, prior, the year before), and the balance-sheet and cash-flow period ends of the anchor. A record stored
  before #239 has no ``period_ends``: those show "n/a", never an exception.
- 13F Buy / Sell are share counts (``module_sec_13f/engine.py`` ``compute_period_metrics``: the summed per-filer
  share deltas), so the card prints shares, never "$".
- ``ticker_scores`` keeps ``anchor_fy``, ``prior_fy`` and ``period_ends``: a Refresh shows the same line (K4).
Offline.
"""
from __future__ import annotations

from RevenueChaser.core import ticker_store
from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_asymmetric_growth import freeze, report
from RevenueChaser.module_asymmetric_growth.engine import score_ticker
from RevenueChaser.module_asymmetric_growth.tests.test_ag_payload_route_380 import _card, _result

_TECH = {"price": 100.0, "dist_52w_pct": 0.05, "vol_ratio": 1.6, "above_sma50": True}


def _rec(fy, end, revenue, **ends):
    return {"fiscal_year": fy, "revenue": revenue, "gross_profit": revenue * 0.5, "total_assets": 1000.0,
            "operating_cash_flow": revenue * 0.2, "capex": 5.0, "cash": 100.0, "total_debt": 10.0,
            "period_ends": {"revenue": end, **ends}}


def _score(annual):
    fin = {"info": {"country": "United States", "exchange": "NMS", "longName": "Face Co"},
           "annual": annual, "market_cap": 5e9, "source": "sec_edgar"}
    return score_ticker("FACE", fin, _TECH, spine={})


def test_the_card_carries_the_years_and_period_ends_it_read():
    card = _score([_rec(2023, "2023-06-30", 100.0), _rec(2024, "2024-06-29", 120.0),
                   _rec(2025, "2025-06-28", 150.0, total_assets="2025-06-28", operating_cash_flow="2025-06-28")])
    assert (card["anchor_fy"], card["prior_fy"]) == (2025, 2024)
    assert card["period_ends"] == {"basis": "FY", "revenue": ["2025-06-28", "2024-06-29", "2023-06-30"],
                                   "balance_sheet": "2025-06-28", "cash_flow": "2025-06-28"}


def test_balance_sheet_and_cash_flow_fall_back_to_a_sibling_field():
    card = _score([_rec(2024, "2024-12-31", 100.0), _rec(2025, "2025-12-31", 150.0, cash="2025-12-30",
                                                         capex="2025-12-29")])
    assert card["period_ends"]["balance_sheet"] == "2025-12-30"
    assert card["period_ends"]["cash_flow"] == "2025-12-29"


def test_a_record_without_period_ends_gives_none():
    annual = [{"fiscal_year": 2024, "revenue": 100.0}, {"fiscal_year": 2025, "revenue": 150.0}]
    card = _score(annual)
    assert (card["anchor_fy"], card["prior_fy"]) == (2025, 2024)
    assert card["period_ends"] == {"basis": "FY", "revenue": [None, None, None], "balance_sheet": None,
                                   "cash_flow": None}


def test_ticker_scores_keeps_the_provenance(rawstore_tmp_db):
    ends = {"revenue": ["2025-12-31", "2024-12-31", None], "balance_sheet": "2025-12-31", "cash_flow": None}
    ticker_store.write_from_cards([_card("AAA", anchor_fy=2025, prior_fy=2024, period_ends=ends)], asof="x")
    got = ticker_store.all_cards()[0]
    assert (got["anchor_fy"], got["prior_fy"], got["period_ends"]) == (2025, 2024, ends)
    assert ticker_store.card_view(got)["period_ends"] == ends


def _grid(cards, tmp_path):
    result = _result(cards)
    log = run_board_js(report.render_asymmetric_growth_hunter_html(result),
                       "setTier('all');\nconsole.log(JSON.stringify({grid: _els['grid'].innerHTML}));", tmp_path,
                       payload=report.board_payload(result))
    return log["grid"]


def test_the_card_shows_the_provenance_line(tmp_path, rawstore_tmp_db):
    ends = {"revenue": ["2025-06-28", "2024-06-29", "2023-06-30"], "balance_sheet": "2025-06-28",
            "cash_flow": "2025-06-27"}
    grid = _grid([_card("AAA", anchor_fy=2025, prior_fy=2024, period_ends=ends,
                        model_version=freeze.model_version())], tmp_path)
    assert "FY2025 vs FY2024" in grid
    assert "2025-06-28" in grid and "2024-06-29" in grid and "2023-06-30" in grid
    assert "balance sheet 2025-06-28" in grid and "cash flow 2025-06-27" in grid


def test_missing_period_ends_show_n_a(tmp_path, rawstore_tmp_db):
    grid = _grid([_card("OLD")], tmp_path)                                  # a row stored before S3: no fields
    assert "FY n/a" in grid and "revenue n/a" in grid
    grid = _grid([_card("PART", anchor_fy=2025, prior_fy=None,
                        period_ends={"revenue": ["2025-12-31", None, None]})], tmp_path)
    assert "FY2025 vs FY n/a" in grid and "2025-12-31 · n/a · n/a" in grid and "balance sheet n/a" in grid


def test_buy_and_sell_are_share_counts(tmp_path, rawstore_tmp_db):
    grid = _grid([_card("AAA", inst_holders=120, inst_buy_flow=15_000_000.0, inst_sell_flow=3_000_000.0)],
                 tmp_path)
    assert "Buy / Sell (shares)" in grid
    row = grid[grid.index("Buy / Sell (shares)"):].split("</tr>")[0]
    assert "15.0M" in row and "3.0M" in row and "$" not in row
