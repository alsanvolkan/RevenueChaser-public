"""The Settings / Data control board (design v28), end to end, against a real dashboard and a real browser.

The board is the Data tab of Data/Health (#492); the Health tab follows. A tier press only drafts and Apply saves
the universe; Fetch puts one row in the builder queue; Fill and "Fill this range" are off by default; Refresh the
example rows reads a serving file read-only. None of them reaches a provider. On a 390 x 844 phone the board is one
column, nothing reaches past the right edge, and every target is at least 44 px, in light and in dark.
"""
from __future__ import annotations

import re

import pytest

from .test_settings_newdata import (PHONE, _APP, _requests, browser, chrome, dashboard,  # noqa: F401 - fixtures
                                    market)


def _open(browser, dashboard, scheme="dark"):
    browser.emulate(*PHONE, scheme=scheme)
    browser.open(f"{dashboard.url}/view/{_APP}", ready="!!document.getElementById('ctl') && !!window.CTL")
    browser.eval(f"document.documentElement.setAttribute('data-theme', {scheme!r})")


def _tab(browser, name: str):
    """Press one of the page's tabs (#331 step 1), as a viewer does, and let it lay out."""
    browser.eval(f"document.getElementById('tab-{name}').click()")
    browser.settle()


def test_the_data_tab_is_on_top_and_the_old_sections_are_gone(dashboard, browser):
    _open(browser, dashboard)
    order = browser.eval("Array.prototype.map.call(document.querySelectorAll('#ctl .wrap > details.sec'),"
                         " function(d){ return d.id + (d.open ? '+' : ''); })")
    assert order == ["sec-flow+"], "the Database tab"
    assert browser.eval("document.getElementById('panel-health').hidden") is True, "Database is the tab on open"
    fetch = browser.eval("Array.prototype.map.call(document.querySelectorAll('#panel-health .ctl .wrap > details.sec'),"
                         " function(d){ return d.id; }).filter(function(i){ return i; })")
    assert fetch[0] == "sec-health" and fetch[-1] == "sec-crosscheck" and "sec-universe" in fetch, \
        f"the Fetch/Health tab: {fetch}"
    assert browser.eval("!document.getElementById('old-settings') && !document.getElementById('panel-fetch')")
    shown = browser.eval("Array.prototype.filter.call(document.querySelectorAll('#ctl .fpanel'),"
                         " function(p){ return !p.hidden; }).length")
    assert shown == 1, "one file panel at a time"


LAYOUT = r"""(function(){
  var sec = document.getElementById('ctl'), vw = document.documentElement.clientWidth;
  var r = sec.getBoundingClientRect();
  var wide = Array.prototype.filter.call(sec.querySelectorAll('*'), function(el){
    var b = el.getBoundingClientRect(); if (!(b.width > 0) || b.right <= r.right + 0.5) return false;
    for (var p = el.parentElement; p && p !== sec; p = p.parentElement) {
      var o = getComputedStyle(p).overflowX; if (o === 'auto' || o === 'scroll' || o === 'hidden') return false; }
    return true;
  }).map(function(el){ return el.tagName + '.' + el.className; });
  var cards = Array.prototype.map.call(sec.querySelectorAll('.wrap > details.sec'), function(el){
    var b = el.getBoundingClientRect(); return {l: Math.round(b.left), w: Math.round(b.width)}; });
  var small = Array.prototype.filter.call(sec.querySelectorAll('button, input, summary, select'), function(el){
    var b = el.getBoundingClientRect(); return b.width > 0 && (b.height < 44 || b.width < 44);
  }).map(function(el){ return el.tagName + '.' + el.className + ' ' + el.textContent.trim().slice(0, 20); });
  return {vw: vw, secRight: r.right, secScroll: sec.scrollWidth, secW: sec.clientWidth, wide: wide.slice(0, 8),
          cards: cards, small: small.slice(0, 12), buttons: sec.querySelectorAll('button').length,
          docScroll: document.documentElement.scrollWidth};
})()"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("tab,sec_id", [("data", "ctl")])
def test_the_board_is_one_column_on_a_phone_with_44px_targets_and_no_sideways_scroll(dashboard, browser, scheme,
                                                                                    tab, sec_id):
    _open(browser, dashboard, scheme)
    _tab(browser, tab)
    browser.eval(f"document.querySelectorAll('#{sec_id} details').forEach(function(d){{ d.open = true; }})")
    browser.settle()
    m = browser.eval(LAYOUT.replace("'ctl'", f"'{sec_id}'"))
    assert m["vw"] == PHONE[0]
    assert m["docScroll"] <= m["vw"], f"the page scrolls sideways: {m['wide']}"
    assert m["secRight"] <= m["vw"] + 0.5, "the board reaches past the phone's edge"
    assert m["secScroll"] <= m["secW"], "the board scrolls sideways"
    assert not m["wide"], f"elements reach past the board's edge: {m['wide']}"
    assert len({c["l"] for c in m["cards"]}) == 1, f"not one column: {m['cards']}"
    assert len({c["w"] for c in m["cards"]}) == 1, "the sections are not full width"
    assert m["buttons"] >= 6 and not m["small"], f"targets under 44 px: {m['small']}"


def test_the_board_follows_the_shells_theme_both_ways(dashboard, browser):
    _open(browser, dashboard, "light")
    look = "(function(){ var c = getComputedStyle(document.getElementById('ctl')); return [c.color, " \
           "getComputedStyle(document.querySelector('#panel-health .ctl .tile')).backgroundColor]; })()"
    light = browser.eval(look)
    browser.eval("document.documentElement.setAttribute('data-theme', 'dark')")
    dark = browser.eval(look)
    assert light[0] != dark[0] and light[1] != dark[1], f"the board does not change with the theme: {light} {dark}"


def _saved_on(dashboard, tier: str) -> str:
    """The tier's ``data-on`` on a freshly rendered page: what the store holds, not what the browser drafted."""
    html = dashboard.view(_APP, refresh=True).text
    tag = re.search(r'<button type="button" class="useg" data-tier="' + tier + r'"[^>]*>', html)
    assert tag, f"no {tier} segment on the board"
    return re.search(r'data-on="(\d)"', tag.group(0)).group(1)


def test_a_tier_press_only_drafts_and_apply_saves_the_universe(dashboard, browser, market, provider_routes):
    before = len(provider_routes.requests())
    _open(browser, dashboard)
    # This store has no spine, so every set counts 0 names and a draft of them is empty (Apply stays off): give the
    # sets sizes, as a built spine would.
    browser.eval("document.querySelectorAll('#universe .useg').forEach(function(b){ b.setAttribute('data-n', '100'); })")
    browser.eval("document.querySelector('#universe .useg[data-tier=\"R\"]').click()")
    assert browser.eval("document.getElementById('uni-draft').hidden") is False
    assert _saved_on(dashboard, "R") == "1", "a press drafts: nothing is saved before Apply"
    browser.eval("document.getElementById('uni-apply').click()")
    browser.wait_for("!!document.getElementById('toast')")
    said = browser.eval("document.getElementById('toast').textContent")
    assert said.startswith("Applied"), said
    assert _saved_on(dashboard, "R") == "0"
    assert _requests(market) == [], "Apply saves a choice and queues no fetch"
    assert len(provider_routes.requests()) == before


def test_fetch_queues_one_request_and_asks_no_provider(dashboard, browser, market, provider_routes):
    before = len(provider_routes.requests())
    _open(browser, dashboard)
    _tab(browser, "health")
    browser.eval("document.querySelector('#panel-health .fgrp[data-file=\"market.duckdb\"]').open = true")
    # wait on the button itself, not on any queued pill: another dataset's queue row may show one at render
    one = ("document.querySelector('#panel-health .fgrp[data-file=\"market.duckdb\"] button[data-job=\"one\"]"
           "[data-dataset=\"market_series\"]')")
    browser.eval(one + ".click()")
    browser.wait_for(one + ".getAttribute('data-state') === 'pause'")
    pill = browser.eval("(function(){ var b = " + one + "; var p = document.querySelector('[data-for=\"' + "
                        "b.getAttribute('data-jid') + '\"]'); return p ? p.className + ' ' + p.textContent : ''; })()")
    assert pill.startswith("lg3 st-q queued"), pill
    rows = _requests(market)
    assert [(r["dataset"], r["status"], r["requested_by"]) for r in rows] == [("market_series", "queued", "settings")]
    assert len(provider_routes.requests()) == before, "a request is not a fetch: no provider was asked"


def test_fill_and_fill_this_range_are_off_by_default(dashboard, browser, market, provider_routes):
    before = len(provider_routes.requests())
    _open(browser, dashboard)
    assert browser.eval("window.CTL.fill_enabled") is False
    fills = browser.eval("Array.prototype.map.call(document.querySelectorAll('.ctl [data-job=\"fill\"]'),"
                         " function(b){ return b.disabled; })")
    assert all(fills), "a Fill cell is pressable while the server keeps fills off"
    _tab(browser, "health")
    opened = browser.eval("(function(){ var c = document.querySelector('#panel-health .fgrp .fopen');"
                          " if (!c) return null; c.click(); return c.getAttribute('data-bf'); })()")
    if opened is not None:
        browser.wait_for(f"!!document.querySelector('#bf-{opened} .bfrun')")
        assert browser.eval(f"document.querySelector('#bf-{opened} .bfrun').disabled") is True
    assert _requests(market) == [] and len(provider_routes.requests()) == before


def test_refresh_the_example_rows_reads_the_store_and_says_when_a_file_is_not_built(dashboard, browser,
                                                                                   provider_routes):
    before = len(provider_routes.requests())
    _open(browser, dashboard)
    has = browser.eval("!!document.querySelector('#ctl [data-sample-refresh]')")
    if not has:
        pytest.skip("this store has no sampled table on the board")
    browser.eval("document.querySelector('#ctl [data-sample-refresh]').click()")
    browser.wait_for("(function(){ var b = document.querySelector('#ctl [data-sample-refresh]');"
                     " var n = b.parentNode.querySelector('.dim');"
                     " return n && /not been built|no .* table yet|read again/.test(n.textContent); })()")
    assert len(provider_routes.requests()) == before


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_what_am_i_looking_at_band_follows_the_theme(dashboard, browser, scheme):
    """Seen in the #539 screenshots: in light the Data tab's help box and the band around it stayed dark, because
    they read the page's old dark-only tokens instead of the board's themed ones."""
    _open(browser, dashboard, scheme)
    lum = browser.eval(r"""(function(){
      function l(el){ var m = getComputedStyle(el).backgroundColor.match(/\d+(\.\d+)?/g).map(Number);
        if (m.length > 3 && m[3] === 0) return null;
        return 0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]; }
      function painted(el){ for (; el; el = el.parentElement) { var v = l(el); if (v !== null) return v; } return null; }
      var help = document.querySelector('.helpwrap .help'), band = help.parentElement.parentElement;
      var next = band.nextElementSibling;
      return {box: painted(help), band: painted(help.parentElement),
              gap: next ? next.getBoundingClientRect().top - band.getBoundingClientRect().bottom : 0};
    })()""")
    assert abs(lum["gap"]) < 0.5, f"the page's own background shows between the band and the board: {lum}"
    if scheme == "light":
        assert lum["box"] > 200 and lum["band"] > 200, f"the help band is dark in light: {lum}"
    else:
        assert lum["box"] < 80 and lum["band"] < 80, f"the help band is light in dark: {lum}"
