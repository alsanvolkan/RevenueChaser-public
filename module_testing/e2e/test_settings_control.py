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
    # A first visit opens on Database (the Watch lists tab left for its own board, 13 Nov 2026).
    assert browser.eval("document.getElementById('panel-data').hidden") is False, "Database is the tab on open"
    assert browser.eval("!document.getElementById('panel-watch')")
    browser.eval("document.getElementById('tab-data').click()")
    browser.settle()


def _tab(browser, name: str):
    """Press one of the page's tabs (#331 step 1), as a viewer does, and let it lay out."""
    browser.eval(f"document.getElementById('tab-{name}').click()")
    browser.settle()


def test_the_data_tab_is_on_top_and_the_old_sections_are_gone(dashboard, browser):
    _open(browser, dashboard)
    order = browser.eval("Array.prototype.map.call(document.querySelectorAll('#ctl .wrap > details.sec'),"
                         " function(d){ return d.id + (d.open ? '+' : ''); })")
    assert order == ["sec-flow+"], "the Database tab"
    assert browser.eval("document.getElementById('panel-health').hidden") is True, "Fetch/Health is not shown beside Database"
    fetch = browser.eval("Array.prototype.map.call(document.querySelectorAll('#panel-health .ctl .wrap > details.sec'),"
                         " function(d){ return d.id; }).filter(function(i){ return i; })")
    # #715 (avo, 12 Nov 2026): Health, the queue, Cross-check and the backups are the framed Grafana board's;
    # under the frame the HTML keeps the universe (and a group for a file that has a fill, which has no id).
    assert fetch == ["sec-universe"], f"the Fetch/Health tab: {fetch}"
    assert browser.eval("!!document.querySelector('#panel-health #live-frame')"), "the board's frame heads the tab"
    assert browser.eval("!document.getElementById('old-settings') && !document.getElementById('panel-fetch')")
    # #715 step 3: the files are stacked folded sections, none hidden; the first one is open.
    files = browser.eval("Array.prototype.map.call(document.querySelectorAll('#ctl details.fpanel:not(.bfp)'),"
                         " function(p){ return (p.hidden ? 'hidden:' : '') + (p.open ? 'open' : 'shut'); })")
    from RevenueChaser.rcdata import serving
    assert files[0] == "open" and set(files[1:]) == {"shut"} and len(files) == len(serving.KINDS), files
    assert browser.eval("!document.getElementById('fpick')"), "no picker under the Database tab"


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
           "getComputedStyle(document.querySelector('#panel-health .ctl .swt')).backgroundColor]; })()"
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


def test_the_page_offers_no_one_off_fetch_and_no_switch(dashboard, browser, market, provider_routes):
    """#715 (avo, 12 Nov 2026): a dataset's Fetch, Pause and the switches are pressed in the framed Grafana board
    (it needs avo's Grafana login), so the HTML under the frame offers none of them and queues nothing."""
    before = len(provider_routes.requests())
    _open(browser, dashboard)
    _tab(browser, "health")
    left = browser.eval("Array.prototype.map.call(document.querySelectorAll('#panel-health button[data-job]'),"
                        " function(b){ return b.getAttribute('data-job'); })")
    assert set(left) <= {"fetch", "picture", "fill", "copy"} and "fetch" in left, left
    assert browser.eval("document.querySelectorAll('#panel-health .swt[data-name], #panel-health #sec-queue').length") == 0
    assert _requests(market) == [], "opening the tab queues nothing"
    assert len(provider_routes.requests()) == before


def test_fetch_the_universe_is_still_pressed_on_the_page_and_asks_no_provider(dashboard, browser, market,
                                                                             provider_routes):
    """The one queueing press left in HTML (#715): it answers with a pill beside the button, puts at most one row in
    the builder's queue and never reaches a provider itself."""
    before = len(provider_routes.requests())
    _open(browser, dashboard)
    _tab(browser, "health")
    btn = "document.querySelector('#panel-health button[data-job=\"fetch\"]')"
    dataset = browser.eval(btn + ".getAttribute('data-dataset')")
    browser.eval(btn + ".click()")
    said = ("(function(){ var b = " + btn + ", id = b.getAttribute('data-jid');"
            " var p = id && document.querySelector('[data-for=\"' + id + '\"]'); return p ? p.className + ' ' + "
            "p.textContent : ''; })()")
    browser.wait_for(said + " !== ''")
    pill = browser.eval(said)
    rows = [(r["dataset"], r["status"], r["requested_by"]) for r in _requests(market)]
    if pill.startswith("jst st-q queued"):
        assert rows == [(dataset, "queued", "settings")], rows
    else:
        assert pill.startswith("jst st-f") and rows == [], f"neither queued nor refused: {pill} {rows}"
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
