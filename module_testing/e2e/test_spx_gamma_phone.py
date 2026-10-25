"""The SPX Gamma board on a phone, in a real browser against the real dashboard.

Phone pass on the pilot board: at an exact 390 px viewport (device emulation, ``harness/cdp_browser.py``) the page
never scrolls sideways, every table stacks, every control inside the board is at least 44 px, a tooltip opens by
tap, the board follows the shell's theme (``data-theme``) in light and dark, and the charts fit their container.

The board is rendered from the module's fixture snapshot (demo values, not market data) and written where the
dashboard looks for the newest SPX report; nothing here starts a job or touches a provider.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from RevenueChaser.core import calendar as nyse_calendar
from RevenueChaser.core.reporting_common import THEME_TOKENS

from .harness.cdp_browser import Page, find_chrome

PHONE = (390, 844)
DESKTOP = (1280, 800)
TAB = "module_spx_gamma"


@pytest.fixture(scope="module")
def chrome():
    path = find_chrome()
    if path is None:
        msg = "no Chrome/Chromium for the SPX phone check: set RC_CHROME or install google-chrome / chromium"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg + " (a skip on CI would leave the check unenforced)")
        pytest.skip(msg)
    return path


@pytest.fixture
def browser(chrome):
    with Page(chrome) as page:
        yield page


@pytest.fixture
def board(dashboard, tmp_path, monkeypatch):
    """The demo board, rendered with its three charts and stored as the newest SPX report."""
    monkeypatch.setattr(nyse_calendar, "in_us_session_window", lambda *a, **k: False)
    from RevenueChaser.module_spx_gamma.charts import position_map
    from RevenueChaser.module_spx_gamma.charts.tornado import build_tornado_fig
    from RevenueChaser.module_spx_gamma.render import render_html
    from RevenueChaser.module_spx_gamma.tests.conftest import make_snapshot

    snap = make_snapshot()

    def frag(fig):
        return fig.to_html(full_html=False, include_plotlyjs=False, config={"responsive": True}) if fig else None

    html = render_html(snap, chart_html=frag(position_map.build(snap, include_tornado=False)),
                       tornado_html=frag(build_tornado_fig(snap)),
                       envelope_html=frag(position_map.build_envelope_fig(snap)))
    day = Path(tmp_path) / "spx_gamma" / "reports" / "2026-08-14"
    day.mkdir(parents=True)
    (day / "section.html").write_text(html, encoding="utf-8")
    return dashboard


def _open(browser, board, size, scheme="dark", theme="dark"):
    browser.emulate(*size, scheme=scheme)
    browser.open(f"{board.url}/view/{TAB}")
    browser.eval(f"document.documentElement.setAttribute('data-theme', {theme!r})")
    browser.wait_for("document.querySelectorAll('.js-plotly-plot .main-svg').length >= 3")
    browser.settle()


def test_the_page_never_scrolls_sideways_on_a_phone(board, browser):
    _open(browser, board, PHONE)
    m = browser.eval("""(function(){ var vw = document.documentElement.clientWidth;
      var wide = Array.prototype.filter.call(document.querySelectorAll('body *'), function(e){
        return !(e instanceof SVGElement) && e.getBoundingClientRect().right > vw + 0.5; })
        .map(function(e){ return e.tagName + '.' + e.className; });
      return {vw: vw, scroll: document.documentElement.scrollWidth, wide: wide.slice(0, 8)}; })()""")
    assert m["vw"] == PHONE[0]
    assert m["scroll"] <= m["vw"], f"the page scrolls sideways: {m['wide']}"
    assert not m["wide"], f"elements reach past the right edge: {m['wide']}"


def test_no_inline_minimum_width_is_left(board):
    html = next(Path(board.cache_dir).parent.glob("spx_gamma/reports/*/section.html")).read_text(encoding="utf-8")
    assert "min-width:900px" not in html and "min-width:1100px" not in html


def test_every_table_stacks_on_a_phone_and_keeps_its_labels(board, browser):
    _open(browser, board, PHONE)
    got = browser.eval("""(function(){ var out = [];
      document.querySelectorAll('table').forEach(function(t){
        var tds = t.querySelectorAll('tbody td'); if(!tds.length) return;
        var bad = Array.prototype.filter.call(tds, function(td){ return !td.hasAttribute('data-label'); }).length;
        out.push({cls: t.className, display: getComputedStyle(t).display,
                  row: getComputedStyle(tds[0].parentNode).display, cell: getComputedStyle(tds[0]).display, bad: bad,
                  scrolls: t.scrollWidth > t.clientWidth + 1}); });
      return out; })()""")
    assert len(got) >= 12, f"expected the board's tables, found {len(got)}"
    for t in got:
        assert t["display"] == "block" and t["row"] == "block", f"table not stacked: {t}"
        assert t["cell"] in ("flex", "block") and t["bad"] == 0 and not t["scrolls"], t


def test_tables_stay_tables_on_a_desktop(board, browser):
    _open(browser, board, DESKTOP)
    assert browser.eval("Array.prototype.every.call(document.querySelectorAll('table'),"
                        " function(t){ return getComputedStyle(t).display === 'table'; })")
    assert browser.eval("document.documentElement.scrollWidth") <= DESKTOP[0]


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("theme", ["dark", "light", "auto"])
def test_the_board_follows_the_theme_in_both_device_schemes(board, browser, scheme, theme):
    _open(browser, board, PHONE, scheme, theme)
    expected = theme if theme != "auto" else scheme
    bg = browser.eval("getComputedStyle(document.body).backgroundColor")
    v = THEME_TOKENS[expected]["bg"].lstrip("#")
    assert bg == "rgb({}, {}, {})".format(*(int(v[i:i + 2], 16) for i in (0, 2, 4))), f"{theme}/{scheme}: {bg}"


CONTRAST = """(function(){
  function parse(c){ var m = c.match(/rgba?\\(([^)]+)\\)/); if(!m) return null;
    var p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(Number); return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1}; }
  function over(top, base){ var a = top.a; return {r: top.r * a + base.r * (1 - a), g: top.g * a + base.g * (1 - a),
    b: top.b * a + base.b * (1 - a), a: 1}; }
  function lum(c){ function f(v){ v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); }
    return .2126 * f(c.r) + .7152 * f(c.g) + .0722 * f(c.b); }
  function bgOf(e){ var stack = []; for(var n = e; n; n = n.parentElement){ var c = parse(getComputedStyle(n).backgroundColor);
      if(c && c.a > 0){ stack.push(c); if(c.a === 1) break; } }
    var base = {r: 0, g: 0, b: 0, a: 1}; var cs = getComputedStyle(document.documentElement).backgroundColor;
    base = parse(getComputedStyle(document.body).backgroundColor) || base;
    for(var i = stack.length - 1; i >= 0; i--) base = over(stack[i], base); return base; }
  var worst = [];
  document.querySelectorAll('body *').forEach(function(e){
    if(e instanceof SVGElement || e.closest('.js-plotly-plot') || e.closest('thead')) return;
    var own = Array.prototype.some.call(e.childNodes, function(n){ return n.nodeType === 3 && n.textContent.trim(); });
    var r = e.getBoundingClientRect(); if(!own || !r.width || !r.height || getComputedStyle(e).visibility === 'hidden') return;
    var fg = parse(getComputedStyle(e).color), bg = bgOf(e); if(!fg) return;
    fg = over(fg, bg); var l1 = lum(fg), l2 = lum(bg), ratio = (Math.max(l1, l2) + .05) / (Math.min(l1, l2) + .05);
    if(ratio < 4.5) worst.push(e.tagName + '.' + e.className + ' ' + ratio.toFixed(2) + ' ' + e.textContent.trim().slice(0, 24)); });
  return worst; })()"""


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_text_has_4_5_to_1_contrast_in_both_themes(board, browser, theme):
    _open(browser, board, PHONE, theme, theme)
    browser.eval("document.querySelectorAll('details.collapsible').forEach(function(d){ d.open = true; })")
    low = browser.eval(CONTRAST)
    assert not low, f"{theme}: text under 4.5:1: {low[:10]}"


def test_a_tooltip_opens_by_tap_and_by_keyboard(board, browser):
    _open(browser, board, PHONE)
    box = "document.querySelector('details.tip .ag-tip-box')"
    assert browser.eval(f"{box}.checkVisibility()") is False, "a tooltip must not show before it is asked for"
    size = browser.eval("(function(){ var r = document.querySelector('details.tip > summary').getBoundingClientRect();"
                        " return [r.width, r.height]; })()")
    assert size[0] >= 44 and size[1] >= 44, f"tooltip trigger is {size}"
    browser.eval("document.querySelector('details.tip > summary').scrollIntoView({block: 'center'})")
    x, y = browser.eval("(function(){ var r = document.querySelector('details.tip > summary').getBoundingClientRect();"
                        " return [r.left + r.width / 2, r.top + r.height / 2]; })()")
    for typ in ("mousePressed", "mouseReleased"):
        browser.send("Input.dispatchMouseEvent", type=typ, x=x, y=y, button="left", clickCount=1)
    assert browser.eval("document.querySelector('details.tip').open") is True, "a tap did not open the tooltip"
    r = browser.eval(f"(function(){{ var r = {box}.getBoundingClientRect(); return [r.left, r.right, r.top]; }})()")
    assert r[0] >= 0 and r[1] <= PHONE[0] and r[2] >= 0, f"the tooltip is off screen: {r}"
    browser.press("Escape")
    assert browser.eval("document.querySelector('details.tip').open") is False
    browser.eval("document.querySelector('details.tip > summary').focus()")
    browser.press("Enter")
    assert browser.eval("document.querySelector('details.tip').open") is True, "Enter did not open the tooltip"


def test_the_board_controls_are_44_px_on_a_phone(board, browser):
    _open(browser, board, PHONE)
    small = browser.eval("""(function(){ var out = [];
      document.querySelectorAll('summary, button, a[href], input, select').forEach(function(e){
        if(e.closest('.modebar')) return; var r = e.getBoundingClientRect(); if(!r.width) return;
        if(r.height < 44 || r.width < 44) out.push(e.tagName + '.' + e.className + ' ' + Math.round(r.width) + 'x' + Math.round(r.height)); });
      return out; })()""")
    assert not small, f"controls under 44 px: {small}"


def test_a_focused_summary_shows_a_focus_ring(board, browser):
    _open(browser, board, PHONE)
    browser.eval("document.querySelector('details.collapsible > summary').focus()")
    browser.press("Tab")
    browser.press("Tab", shift=True)
    ring = browser.eval("getComputedStyle(document.activeElement).outlineStyle")
    assert ring != "none", "keyboard focus on a summary is invisible"


@pytest.mark.parametrize("size", [PHONE, DESKTOP])
def test_the_charts_fit_their_container(board, browser, size):
    _open(browser, board, size)
    got = browser.eval("""(function(){ return Array.prototype.map.call(document.querySelectorAll('.chart'), function(c){
      var g = c.querySelector('.js-plotly-plot'); var s = g.querySelector('.main-svg').getBoundingClientRect();
      var plot = g.layout.margin; return {box: c.getBoundingClientRect().width, svg: s.width, graph: g.getBoundingClientRect().width,
        h: g.getBoundingClientRect().height, vh: window.innerHeight, mr: plot.r, auto: g.layout.autosize}; }); })()""")
    assert len(got) == 3
    for c in got:
        assert c["svg"] <= c["box"] + 1 and c["graph"] <= c["box"] + 1, c
        assert c["auto"] is True, c
    if size == PHONE:
        browser.wait_for("document.querySelector('.js-plotly-plot').layout.margin.r <= 16")
        got[0]["mr"] = browser.eval("document.querySelector('.js-plotly-plot').layout.margin.r")
        assert all(c["mr"] <= 16 for c in got[:1]), f"the position map keeps its desktop margin: {got[0]}"
        assert got[0]["h"] <= 0.7 * got[0]["vh"] + 1, got[0]


def test_a_chart_gets_its_margin_back_when_the_window_grows(board, browser):
    _open(browser, board, PHONE)
    browser.emulate(*DESKTOP, scheme="dark")
    browser.wait_for("document.querySelector('.js-plotly-plot').layout.margin.r > 100")
