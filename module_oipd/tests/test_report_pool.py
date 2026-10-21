"""Implied Probability: the picker pool is the typed universe, but a ticker outside it
(a fund, SPY, ...) can still be opened (#87, Identity R4)."""
from __future__ import annotations

import json

from RevenueChaser.core import rawstore
from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_oipd import engine, report


def _seed_pool():
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "market_cap": cap, "name": t + " Inc", "price": 10.0,
         "sector": "Technology", "industry": "Software - Application", "quote": {"symbol": t}}
        for t, cap in (("AAA", 9e9), ("BBB", 8e9), ("FUND", 7e9))])
    rawstore.ensure_table("security_master")
    rawstore.write_rows("security_master", [
        {"ticker": "AAA", "security_type": "common"},
        {"ticker": "FUND", "security_type": "cef"}])


def test_the_pool_leaves_out_excluded_types(rawstore_tmp_db):
    _seed_pool()
    assert [r["ticker"] for r in report._filter_universe()] == ["AAA", "BBB"]


def test_the_pool_rows_carry_the_two_level_sector_and_industry(rawstore_tmp_db):
    """#266: a picker row is filtered on ``sector`` and ``industry`` (and the ``[sector, industry]`` path); no group or sub-industry."""
    _seed_pool()
    row = report._filter_universe()[0]
    assert (row["sector"], row["industry"]) == ("Technology", "Software - Application")
    assert row["gics"] == ["Technology", "Software - Application"]
    assert "industry_group" not in row and "sub_industry" not in row


def test_the_page_is_on_the_shared_filter_kit(rawstore_tmp_db):
    """#383 C1b: the shared filter bar (rc_filters), then one combobox; the constants kit is gone."""
    html = report.render_oipd_dashboard(engine._empty_payload("AAA", "x"))
    assert '<section class="rcf-bar" id="rc-filters"></section>' in html
    assert "RCFilters.mount(" in html and "RCCombobox.mount(" in html
    assert html.index('id="rc-filters"') < html.index('id="ticker-combo"')
    for gone in ('id="mc-tabs"', 'id="eweek-tabs"', 'id="index-tabs"', 'id="sector"', "initTickerCombo",
                 "buildMcPills", "INDEX_LABELS", 'id="industry-group"', 'id="sub-industry"'):
        assert gone not in html, f"constants kit left: {gone}"


def test_the_filter_meta_comes_from_the_registry_and_the_pinned_themes(rawstore_tmp_db):
    html = report.render_oipd_dashboard(engine._empty_payload("AAA", "x"))
    meta = json.loads(html.split("var META = ", 1)[1].split(";\n", 1)[0])
    assert meta["board"] == "oipd" and meta["earnings_mode"] == "filter"
    assert meta["indexes"] and "S&P 500" in meta["indexes"]
    assert meta["themes"] == {}                       # a fresh store has no pinned run


def test_an_excluded_ticker_is_the_default_but_not_in_the_pool(rawstore_tmp_db):
    _seed_pool()
    html = report.render_oipd_dashboard(engine._empty_payload("FUND", "No cached chain"))
    assert 'var DEFAULT_TICKER = "FUND";' in html
    data = json.loads(html.split("var DATA = ", 1)[1].split(";\nvar META", 1)[0])
    assert [r["ticker"] for r in data] == ["AAA", "BBB"]


def test_the_ticker_combobox_lets_a_free_ticker_be_typed(rawstore_tmp_db):
    html = report.render_oipd_dashboard(engine._empty_payload("AAA", "x"))
    assert "allowFree: true" in html.split("RCCombobox.mount(", 1)[1].split("});", 1)[0]
    assert "clear: true" in html.split("RCCombobox.mount(", 1)[1].split("});", 1)[0]   # the × works


def test_the_page_keeps_an_excluded_ticker_as_its_focal_name(rawstore_tmp_db, tmp_path):
    """Run the board's script: the pool's first name must not replace the focal ticker."""
    _seed_pool()
    html = report.render_oipd_dashboard(engine._empty_payload("FUND", "No cached chain"))
    got = run_board_js(html, "console.log(JSON.stringify({focal: FOCAL, t: window.OIPD_TICKER, "
                             "shown: document.getElementById('ticker-combo').value, "
                             "body: document.body.dataset.ticker, pool: DATA.filter(pass).length}));", tmp_path)
    assert got == {"focal": "FUND", "t": "FUND", "shown": "FUND", "body": "FUND", "pool": 2}


def test_no_filter_set_keeps_every_name_in_the_picker_pool(rawstore_tmp_db, tmp_path):
    """#383 C1b parity: with no filter set the kit passes the same names the old pool did (all of them)."""
    _seed_pool()
    html = report.render_oipd_dashboard(engine._empty_payload("AAA", "x"))
    got = run_board_js(html, "console.log(JSON.stringify({all: DATA.map(function(c){ return c.ticker; }), "
                             "pool: DATA.filter(pass).map(function(c){ return c.ticker; }), "
                             "active: RCF.active()}));", tmp_path)
    assert got == {"all": ["AAA", "BBB"], "pool": ["AAA", "BBB"], "active": False}


def test_the_sector_filter_narrows_the_picker_pool(rawstore_tmp_db, tmp_path):
    """#266 on the kit: the pool is filtered at either of the two sector levels."""
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "market_cap": 9e9, "name": t + " Inc", "price": 10.0, "sector": s, "industry": i,
         "quote": {"symbol": t}}
        for t, s, i in (("AAA", "Healthcare", "Biotechnology"), ("BBB", "Healthcare", "Medical Devices"),
                        ("CCC", "Technology", "Software - Application"))])
    html = report.render_oipd_dashboard(engine._empty_payload("AAA", "x"))
    got = run_board_js(html, """
function pool(){ return DATA.filter(pass).map(function(c){ return c.ticker; }); }
var log = {all: pool()};
RCF.set({gics: {key: "Healthcare", label: "Healthcare", level: 1}}); log.sector = pool();
RCF.set({gics: {key: "Healthcare›Biotechnology", label: "Biotechnology", level: 2}}); log.industry = pool();
console.log(JSON.stringify(log));
""", tmp_path)
    assert got == {"all": ["AAA", "BBB", "CCC"], "sector": ["AAA", "BBB"], "industry": ["AAA"]}
