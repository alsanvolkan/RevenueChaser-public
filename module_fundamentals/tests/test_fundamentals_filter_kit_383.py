"""#383 C1c: the Fundamental Analysis board's universal filters are the shared filter kit.

The board keeps its own **Gate** pills and search box; Market cap / Earnings / Index / Sector /
Theme come from the rc_filters bar. Its rows carry the store's filter fields (``gics`` + ``theme_ids``),
added at render time so a Refresh stays network-free.
"""
from __future__ import annotations

import json
import re


def _gate(name: str) -> dict:
    return {"name": name, "passed": True, "score": "3/3", "rules": []}


def _candidate(ticker: str, *, sector: str = "Technology", industry: str = "Software") -> dict:
    return {"ticker": ticker, "name": f"{ticker} Inc", "price": 50.0, "market_cap": 1e9,
            "sector": sector, "industry": industry, "themes": ["Software"],
            "earnings_date": None, "period": "Q2/2026 vs Q2/2025", "distorted": False,
            "multiplier": 3, "pe": 15.0, "pb": 2.0, "roic": 0.2,
            "gates": {"rule_of_40": _gate("Ro40"), "quality": _gate("Q"), "momentum": _gate("M")}}


def _render(candidates):
    from RevenueChaser.module_fundamentals import config
    from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
    return render_fundamental_dashboard({"candidates": candidates, "passing": {},
                                         "selection_version": config.SELECTION_VERSION,
                                         "summary": {}, "universe_total": len(candidates)})


def test_the_board_renders_the_shared_filter_bar(rawstore_tmp_db):
    html = _render([_candidate("AAA")])
    assert 'id="rc-filters"' in html
    assert "RCFilters.mount" in html
    assert "sectorOk(" not in html and "mcRange(" not in html


def test_the_embedded_rows_equal_the_candidates_and_carry_filter_fields(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    rawstore.write_rows("equity_universe", [
        {"ticker": "AAA", "name": "AAA Inc", "sector": "Technology", "industry": "Software"}])
    candidates = [_candidate("AAA"), _candidate("BBB", sector="", industry="")]
    html = _render(candidates)
    data = json.loads(re.search(r"var BOOT = (.*?);\s*var DATA", html, re.S).group(1))["rows"]
    assert [d["ticker"] for d in data] == [c["ticker"] for c in candidates]
    assert all("gics" in d and "theme_ids" in d for d in data)
    assert data[0]["gics"] == ["Technology", "Software"]
    assert data[1]["gics"] == ["Unclassified", ""]


def test_the_stored_payload_is_not_mutated(rawstore_tmp_db):
    candidates = [_candidate("AAA")]
    before = json.dumps(candidates[0], sort_keys=True)
    _render(candidates)
    assert json.dumps(candidates[0], sort_keys=True) == before
    assert "gics" not in candidates[0]


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
        {"ticker": "AAA", "name": "AAA Inc", "sector": "Technology", "industry": "Software"},
        {"ticker": "BBB", "name": "BBB Inc", "sector": "Healthcare", "industry": "Biotechnology"},
        {"ticker": "CCC", "name": "CCC Inc", "sector": "Technology", "industry": "Software"}])


def _row(ticker, *, sector, industry, multiplier):
    c = _candidate(ticker, sector=sector, industry=industry)
    c["multiplier"] = multiplier
    return c


def _gate_passed(name, passed):
    return {"name": name, "passed": passed, "score": "3/3",
            "rules": [{"id": "r", "label": "r", "unit": "ratio", "passed": passed, "actual": 1.0, "threshold": "x"}]}


def _cand_with(ticker, *, sector, industry, ro40, quality, momentum):
    c = _candidate(ticker, sector=sector, industry=industry)
    c["gates"] = {"rule_of_40": _gate_passed("Ro40", ro40), "quality": _gate_passed("Q", quality),
                  "momentum": _gate_passed("M", momentum)}
    return c


def test_meta_themes_equal_the_catalog_and_the_themed_row_carries_the_id(rawstore_tmp_db):
    from RevenueChaser.core import universe
    _seed_one_theme()
    html = _render([_row("AAA", sector="Technology", industry="Software", multiplier=3),
                    _row("BBB", sector="Healthcare", industry="Biotechnology", multiplier=3)])
    meta = json.loads(re.search(r"var META = (.*?);\s*var RULES", html, re.S).group(1))
    data = json.loads(re.search(r"var BOOT = (.*?);\s*var DATA", html, re.S).group(1))["rows"]
    assert meta["themes"] == universe.theme_catalog()
    # R2B-1 (avo, "Fix ile al"): an all-caps board has no $300M floor, so Micro is a counted bucket again.
    assert "floor_mc" not in meta
    assert _THEME in data[0]["theme_ids"]
    assert _THEME not in data[1]["theme_ids"]


def test_a_rule_toggle_and_a_sector_filter_narrow_more_than_either_alone(tmp_path, rawstore_tmp_db):
    from RevenueChaser.core.tests.board_js import run_board_js
    _seed_one_theme()
    html = _render([
        _cand_with("AAA", sector="Technology", industry="Software", ro40=True, quality=True, momentum=True),
        _cand_with("BBB", sector="Healthcare", industry="Biotechnology", ro40=True, quality=True, momentum=True),
        _cand_with("CCC", sector="Technology", industry="Software", ro40=True, quality=False, momentum=False),
    ])
    out = run_board_js(html, """
function shown(){ var g = _els['grid'].innerHTML; return ['AAA','BBB','CCC'].filter(function(t){ return g.indexOf(t) >= 0; }); }
var log = {};
log.all = shown();
setRule('quality', true); setRule('momentum', true); log.rules = shown();
setRule('quality', false); setRule('momentum', false);
RCF.set({gics: {key: 'Technology', label: 'Technology', level: 1}}); log.kit = shown();
setRule('quality', true); setRule('momentum', true); log.both = shown();
console.log(JSON.stringify(log));
""", tmp_path)
    both, rules, kit = set(out["both"]), set(out["rules"]), set(out["kit"])
    assert set(out["all"]) == {"AAA", "BBB", "CCC"}
    assert both < rules and both < kit and both == rules & kit


def test_a_ticked_rule_shows_only_cards_that_pass_it_an_unknown_is_hidden(tmp_path, rawstore_tmp_db):
    """#386 F2, card 3 (avo): a card shows when it PASSES every ticked rule. A Rule of 40 that is unknown (an
    EBITDA-basis fallback) is not a pass, so the default Rule-of-40 toggle hides it; with no rule ticked it shows."""
    from RevenueChaser.core.tests.board_js import run_board_js
    _seed_one_theme()
    html = _render([
        _cand_with("AAA", sector="Technology", industry="Software", ro40=True, quality=False, momentum=False),
        _cand_with("CCC", sector="Technology", industry="Software", ro40=None, quality=True, momentum=False),
    ])
    out = run_board_js(html, """
function shown(){ var g = _els['grid'].innerHTML; return ['AAA','CCC'].filter(function(t){ return g.indexOf(t) >= 0; }); }
var log = {};
log.default = shown();
setRule('rule_of_40', false); log.none = shown();
console.log(JSON.stringify(log));
""", tmp_path)
    assert out["default"] == ["AAA"]
    assert out["none"] == ["AAA", "CCC"]
