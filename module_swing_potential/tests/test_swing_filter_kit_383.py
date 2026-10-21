"""#383 C1c: the Swing Screener's universal filters are the shared filter kit.

The board keeps its own **Multiplier** pills and search box; Market cap / Earnings / Index / Sector /
Theme come from the rc_filters bar. Its rows carry the store's filter fields (``gics`` + ``theme_ids``),
added at render time so a Refresh stays network-free.
"""
from __future__ import annotations

import json
import re


def _gate() -> dict:
    return {"name": "Minervini", "passed": True, "score": "3/3", "known": 1, "total": 1,
            "rules": [{"label": "rule", "passed": True, "threshold": "> 1", "unit": "ratio",
                       "actual": 1.2}]}


def _candidate(ticker: str, *, sector: str = "Technology",
               industry: str = "Software - Application",
               methods=("minervini",)) -> dict:
    return {"ticker": ticker, "name": f"{ticker} Inc", "price": 10.0, "change_1w": 0.01,
            "change_1m": 0.01, "market_cap": 1e9, "sector": sector, "industry": industry,
            "themes": ["Theme"], "index": ["S&P 500"], "earnings_date": None,
            "methods": list(methods), "bucket": "+".join(methods), "direction_ok": True,
            "quality": 80, "rs_rating": 90, "entry": 10.0, "target": 12.0,
            "stop": 9.0, "rr": 2.0, "atr": 0.5, "gates": {"minervini": _gate()}}


def _render(candidates):
    from RevenueChaser.module_swing_potential import config
    from RevenueChaser.module_swing_potential.report import render_swing_dashboard
    return render_swing_dashboard({"candidates": candidates, "passing": {}, "summary": {},
                                   "universe_total": len(candidates), "is_fixture": False,
                                   "selection_version": config.SELECTION_VERSION})


def test_the_board_renders_the_shared_filter_bar(rawstore_tmp_db):
    html = _render([_candidate("AAA")])
    assert 'id="rc-filters"' in html
    assert "RCFilters.mount" in html
    assert "sectorOk(" not in html and "mcRange(" not in html


def test_the_embedded_rows_equal_the_candidates_and_carry_filter_fields(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    rawstore.write_rows("equity_universe", [
        {"ticker": "AAA", "name": "AAA Inc", "sector": "Technology", "industry": "Software - Application"}])
    candidates = [_candidate("AAA"), _candidate("BBB", sector="", industry="")]
    html = _render(candidates)
    data = json.loads(re.search(r"var BOOT = (.*?);\s*var DATA", html, re.S).group(1))["rows"]
    assert [d["ticker"] for d in data] == [c["ticker"] for c in candidates]
    assert all("gics" in d and "theme_ids" in d for d in data)
    assert data[0]["gics"] == ["Technology", "Software - Application"]
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
        {"ticker": "AAA", "name": "AAA Inc", "sector": "Technology", "industry": "Software - Application"},
        {"ticker": "BBB", "name": "BBB Inc", "sector": "Healthcare", "industry": "Biotechnology"},
        {"ticker": "CCC", "name": "CCC Inc", "sector": "Technology", "industry": "Software - Application"}])


def _swing(ticker, *, sector, industry, methods):
    c = _candidate(ticker, sector=sector, industry=industry, methods=methods)
    c["gates"] = {k: _gate() for k in ("minervini", "weinstein", "qullamaggie")}
    return c


def test_meta_themes_equal_the_catalog_and_the_themed_row_carries_the_id(rawstore_tmp_db):
    from RevenueChaser.core import universe
    _seed_one_theme()
    html = _render([_swing("AAA", sector="Technology", industry="Software - Application", methods=("minervini",)),
                    _swing("BBB", sector="Healthcare", industry="Biotechnology", methods=("minervini",))])
    meta = json.loads(re.search(r"var META = (.*?);\s*var RCF", html, re.S).group(1))
    data = json.loads(re.search(r"var BOOT = (.*?);\s*var DATA", html, re.S).group(1))["rows"]
    assert meta["themes"] == universe.theme_catalog()
    # R2B-1 (avo, "Fix ile al"): an all-caps board has no $300M floor, so Micro is a counted bucket again.
    assert "floor_mc" not in meta
    assert _THEME in data[0]["theme_ids"]
    assert _THEME not in data[1]["theme_ids"]


def test_a_method_toggle_and_a_sector_filter_narrow_more_than_either_alone(tmp_path, rawstore_tmp_db):
    from RevenueChaser.core.tests.board_js import run_board_js
    _seed_one_theme()
    html = _render([
        _swing("AAA", sector="Technology", industry="Software - Application", methods=("minervini", "weinstein")),
        _swing("BBB", sector="Healthcare", industry="Biotechnology", methods=("weinstein",)),
        _swing("CCC", sector="Technology", industry="Software - Application", methods=("minervini",)),
    ])
    out = run_board_js(html, """
function shown(){ var g = _els['grid'].innerHTML; return ['AAA','BBB','CCC'].filter(function(t){ return g.indexOf(t) >= 0; }); }
    function tick(k, on){ METHODS[k] = on; render(); }
    var log = {};
    tick('minervini', false);
    log.all = shown();
    tick('weinstein', true); log.pill = shown();
    tick('weinstein', false);
RCF.set({gics: {key: 'Technology', label: 'Technology', level: 1}});
log.kit = shown();
tick('weinstein', true); log.both = shown();
console.log(JSON.stringify(log));
""", tmp_path)
    both, pill, kit = set(out["both"]), set(out["pill"]), set(out["kit"])
    assert set(out["all"]) == {"AAA", "BBB", "CCC"}
    assert both < pill and both < kit and both == pill & kit
