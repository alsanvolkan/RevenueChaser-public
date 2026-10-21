"""#383 C1c: the Asymmetric Growth board's universal filters are the shared filter kit.

The board keeps its own **Points** pills and search box; Market cap / Earnings / Index / Sector /
Theme come from the rc_filters bar (``filters_bar_html`` + ``RCFilters.mount``). Its rows carry the
store's filter fields (``gics`` + ``theme_ids``), added at render time so a Refresh stays network-free. The
rows reach the page through the payload route (S7, K10).
"""
from __future__ import annotations

import json
import re

_THR = {"rev_growth": 0.2, "gross_margin": 0.4, "net_debt_assets": 0.1, "runway": 2.0,
        "dist_52w": 0.1, "vol_ratio": 1.4, "rd_intensity": 0.15, "dilution": 0.2,
        "accelerating": True, "fcf": True, "above_sma50": True}


def _card(ticker: str, *, sector: str = "Technology", industry: str = "Semiconductors") -> dict:
    return {"ticker": ticker, "name": f"{ticker} Inc", "sector": sector, "industry": industry,
            "index": ["S&P 500"], "themes": ["Memory"], "total_points": 8, "price": 100.0,
            "market_cap": 1e11, "rev_growth": 0.5, "gross_margin": 0.5, "ps_ratio": 3.0,
            "fcf": 1e9, "runway_years": 5, "rd_intensity": 0.2, "dilution_yoy": 0.01,
            "tech_breakthrough": True, "dilution_warning": False, "signals": [],
            "earnings_date": None, "inst_holders": None}


def _result(cards):
    return {"asof": "", "summary": {}, "cards": cards, "thresholds": _THR}


def _render(cards):
    from RevenueChaser.module_asymmetric_growth.report import render_asymmetric_growth_hunter_html
    return render_asymmetric_growth_hunter_html(_result(cards))


def _rows(cards):
    """The rows the page loads from the payload route (S7, K10)."""
    from RevenueChaser.module_asymmetric_growth.report import board_payload
    return board_payload(_result(cards))["cards"]


def test_the_board_renders_the_shared_filter_bar(rawstore_tmp_db):
    html = _render([_card("MU")])
    assert 'id="rc-filters"' in html
    assert "RCFilters.mount" in html
    assert "sectorOk(" not in html and "mcRange(" not in html


def test_the_loaded_rows_equal_the_cards_and_carry_filter_fields(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    rawstore.write_rows("equity_universe", [
        {"ticker": "MU", "name": "Micron", "sector": "Technology", "industry": "Semiconductors"}])
    cards = [_card("MU"), _card("AAA", sector="", industry="")]
    data = _rows(cards)
    assert [d["ticker"] for d in data] == [c["ticker"] for c in cards]
    assert all("gics" in d and "theme_ids" in d for d in data)
    assert data[0]["gics"] == ["Technology", "Semiconductors"]
    assert data[1]["gics"] == ["Unclassified", ""]


def test_the_stored_payload_is_not_mutated(rawstore_tmp_db):
    cards = [_card("MU")]
    before = json.dumps(cards[0], sort_keys=True)
    _render(cards)
    assert json.dumps(cards[0], sort_keys=True) == before
    assert "gics" not in cards[0]


_THEME = "r1:c1"


def _seed_one_theme():
    from RevenueChaser.core import rawstore
    rawstore.write_rows("theme_runs", [{"id": "r1", "created_at": "x", "k_fine": 0,
                                        "k_medium": 0, "k_coarse": 1, "jaccard": 1.0, "is_current": 1}])
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": _THEME, "level": "coarse", "parent_id": "", "label": "Memory", "size": 1}])
    rawstore.write_rows("relatedness_theme_members", [
        {"ticker": "AAA", "theme_id": _THEME, "weight": 1.0}])
    rawstore.write_rows("equity_universe", [
        {"ticker": "AAA", "name": "AAA Inc", "sector": "Technology", "industry": "Semiconductors"},
        {"ticker": "BBB", "name": "BBB Inc", "sector": "Healthcare", "industry": "Biotechnology"},
        {"ticker": "CCC", "name": "CCC Inc", "sector": "Technology", "industry": "Semiconductors"}])


def _signals(n_pass: int) -> list:
    subs = [{"label": "s", "value": 1, "unit": "x", "threshold": 0, "op": "mark",
             "param": "rev_growth", "passed": i < n_pass} for i in range(9)]
    return [{"key": "growth", "name": "Growth", "passed": n_pass, "total": 9, "subs": subs}]


def test_meta_themes_equal_the_catalog_and_the_themed_row_carries_the_id(rawstore_tmp_db):
    from RevenueChaser.core import universe
    _seed_one_theme()
    cards = [_card("AAA"), _card("BBB", sector="Healthcare", industry="Biotechnology")]
    html = _render(cards)
    meta = json.loads(re.search(r"var META = (.*?);\s*var PT", html, re.S).group(1))
    data = _rows(cards)
    assert meta["themes"] == universe.theme_catalog()
    # R2B-1 (avo, "Fix ile al"): an all-caps board has no $300M floor, so Micro is a counted bucket again.
    assert "floor_mc" not in meta
    assert _THEME in data[0]["theme_ids"]
    assert _THEME not in data[1]["theme_ids"]


def test_points_pill_and_a_sector_filter_narrow_more_than_either_alone(tmp_path, rawstore_tmp_db):
    from RevenueChaser.core.tests.board_js import run_board_js
    _seed_one_theme()
    cards = [
        dict(_card("AAA"), signals=_signals(9), total_points=9),
        dict(_card("BBB", sector="Healthcare", industry="Biotechnology"), signals=_signals(9), total_points=9),
        dict(_card("CCC"), signals=_signals(4), total_points=4),
    ]
    html = _render(cards)
    out = run_board_js(html, """
function shown(){ var g = _els['grid'].innerHTML; return ['AAA','BBB','CCC'].filter(function(t){ return g.indexOf(t) >= 0; }); }
var log = {};
log.all = shown();
setPt('9'); log.pill = shown();
setPt('all');
RCF.set({gics: {key: 'Technology', label: 'Technology', level: 1}});
log.kit = shown();
setPt('9'); log.both = shown();
console.log(JSON.stringify(log));
""", tmp_path, payload={"asof": "", "cards": _rows(cards)})
    both, pill, kit = set(out["both"]), set(out["pill"]), set(out["kit"])
    assert set(out["all"]) == {"AAA", "BBB", "CCC"}
    assert both < pill and both < kit and both == pill & kit
