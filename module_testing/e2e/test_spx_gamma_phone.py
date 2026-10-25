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

MAP_SHORT = "document.querySelector('.chart-map .main-svg').getBoundingClientRect().height <= 0.7 * innerHeight + 1"
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
    from RevenueChaser.module_spx_gamma.run import _export_chart as export
    from RevenueChaser.module_spx_gamma.tests.conftest import make_snapshot

    snap = make_snapshot()

    def frag(fig):
        # The real export path (run._export_chart), so a change to its to_html config is seen here.
        return export(fig, Path(tmp_path) / "charts", "c", png=False)[2]

    (Path(tmp_path) / "charts").mkdir(exist_ok=True)
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
  // Chromium reports a color-mix() colour as color(srgb 0.2 0.8 0.6 / 0.16), not rgb(): read both, and say so
  // when a colour is neither, instead of skipping it (#195 review MUST 2).
  var unparsed = [];
  function parse(c){ var m = c.match(/^rgba?\(([^)]+)\)/), k = 1;
    if(!m){ m = c.match(/^color\(srgb ([^)]+)\)/); k = 255; }
    if(!m){ if(c !== 'transparent') unparsed.push(c); return null; }
    var p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
    return {r: p[0] * k, g: p[1] * k, b: p[2] * k, a: p.length > 3 ? p[3] : 1}; }
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
  return worst.concat(unparsed.map(function(c){ return 'unparsed colour ' + c; })); })()"""


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
    if size == PHONE:
        browser.wait_for(MAP_SHORT)
    got = browser.eval("""(function(){ return Array.prototype.map.call(document.querySelectorAll('.chart'), function(c){
      var g = c.querySelector('.js-plotly-plot'); var s = g.querySelector('.main-svg').getBoundingClientRect();
      var plot = g.layout.margin; return {box: c.getBoundingClientRect().width, svg: s.width, graph: g.getBoundingClientRect().width,
        h: g.getBoundingClientRect().height, vh: window.innerHeight, mr: plot.r, auto: g.layout.autosize}; }); })()""")
    assert len(got) == 3
    for c in got:
        assert c["svg"] <= c["box"] + 1 and c["graph"] <= c["box"] + 1, c
        assert c["auto"] is True, c
    if size == PHONE:
        browser.wait_for(MAP_SHORT)
        got[0]["mr"] = browser.eval("document.querySelector('.js-plotly-plot').layout.margin.r")
        assert all(c["mr"] <= 16 for c in got[:1]), f"the position map keeps its desktop margin: {got[0]}"
        assert got[0]["h"] <= 0.7 * got[0]["vh"] + 1, got[0]


def test_a_chart_gets_its_margin_back_when_the_window_grows(board, browser):
    _open(browser, board, PHONE)
    browser.emulate(*DESKTOP, scheme="dark")
    browser.wait_for("document.querySelector('.js-plotly-plot').layout.margin.r > 100")



# ── PR #195 review: the map's height, inside the shell and on a phone ──
FRAME_READY = ("(function(){ var f = document.querySelector('.frame.on'); try {"
               " return !!(f && f.getAttribute('src') && f.contentDocument.readyState === 'complete'"
               " && f.contentDocument.location.href !== 'about:blank'"
               " && f.contentDocument.querySelectorAll('.js-plotly-plot .main-svg').length >= 3); } catch (e) { return false; } })()")
FRAME_CHARTS = ("Array.prototype.map.call(document.querySelector('.frame.on').contentDocument.querySelectorAll('.chart'),"
                " function(c){ return Math.round(c.querySelector('.js-plotly-plot').getBoundingClientRect().height); })")


def test_the_position_map_is_700_px_tall_on_a_desktop_inside_the_shell(board, browser):
    """#195 review MUST 1: min(700px, 70vh) made the map 486 px in the shell at 1280x800."""
    browser.emulate(*DESKTOP, scheme="dark")
    browser.open(f"{board.url}/#{TAB}", ready=FRAME_READY)
    assert browser.eval(FRAME_CHARTS) == [700, 460, 460]


def test_only_the_map_follows_the_screen_height_on_a_phone(board, browser):
    _open(browser, board, PHONE)
    browser.wait_for(MAP_SHORT)
    heights = browser.eval("Array.prototype.map.call(document.querySelectorAll('.chart'), function(c){"
                           " return Math.round(c.querySelector('.js-plotly-plot').getBoundingClientRect().height); })")
    assert heights[0] <= 0.7 * PHONE[1] + 1 and heights[1:] == [460, 460], heights
    box = browser.eval("Array.prototype.map.call(document.querySelectorAll('.chart'), function(c){"
                       " return Math.round(c.getBoundingClientRect().height - c.querySelector('.js-plotly-plot')"
                       ".getBoundingClientRect().height); })")
    assert all(extra <= 1 for extra in box), f"a chart box is taller than its chart: {box}"


def test_the_tornado_labels_stay_inside_the_chart_on_a_phone(board, browser):
    """#195 review SHOULD 1: the 16 px margin pushed the level labels past the chart's right edge."""
    _open(browser, board, PHONE)
    browser.wait_for(MAP_SHORT)
    browser.wait_for("document.querySelectorAll('.chart')[1].querySelector('.js-plotly-plot')._rcNarrow === true")
    probe = """(function(){ var c = document.querySelectorAll('.chart')[1], s = c.querySelector('.main-svg').getBoundingClientRect();
      return Array.prototype.filter.call(c.querySelectorAll('g.annotation'), function(a){
        var r = a.getBoundingClientRect(); return r.width > 0 && (r.right > s.right + 1 || r.left < s.left - 1); })
        .map(function(a){ return a.textContent.slice(0, 24); }); })()"""
    try:
        browser.wait_for(f"{probe}.length === 0", timeout=8)         # the footer shrinks after the relayout
    except RuntimeError:
        pass
    out = browser.eval(probe)
    assert not out, f"tornado labels outside the chart: {out}"


FIRST_THEME = """(function(){ new MutationObserver(function(_, o){
  var t = document.documentElement.getAttribute('data-theme');
  if(t && !window.__first){ window.__first = {theme: t, body: !!document.body}; o.disconnect(); }
}).observe(document, {attributes: true, subtree: true}); })()"""


def _stored_theme(browser, board, choice, scheme="dark"):
    browser.emulate(*PHONE, scheme=scheme)
    browser.open(f"{board.url}/view/{TAB}")
    browser.eval(f"localStorage.setItem('rc-theme', {choice!r})")
    browser.send("Page.addScriptToEvaluateOnNewDocument", source=FIRST_THEME)
    browser.open(f"{board.url}/view/{TAB}")


@pytest.mark.parametrize("choice", ["light", "auto"])
def test_a_board_opened_directly_reads_the_stored_theme_before_it_paints(board, browser, choice):
    """#195 review SHOULD 2: opened directly, the board ignored rc-theme and was always dark; in the shell it
    painted dark first. The board's own head script applies the choice, before the body exists."""
    _stored_theme(browser, board, choice, scheme="light")
    first = browser.eval("window.__first")
    assert first == {"theme": choice, "body": False}, first
    bg = browser.eval("getComputedStyle(document.body).backgroundColor")
    assert bg == "rgb(244, 246, 249)", bg


def test_no_stored_theme_is_dark_on_a_light_device(board, browser):
    browser.emulate(*PHONE, scheme="light")
    browser.open(f"{board.url}/view/{TAB}")
    assert browser.eval("document.documentElement.getAttribute('data-theme')") == "dark"
    assert browser.eval("getComputedStyle(document.body).backgroundColor") == "rgb(5, 17, 31)"


# ── #195 review SHOULD 3: tests that guarded nothing, now pinned to the rule they name ──
def test_the_boards_own_focus_rule_draws_the_ring(board, browser):
    """The browser's default outline (style auto) is not this board's :focus-visible rule."""
    _open(browser, board, PHONE)
    browser.eval("document.body.focus()")
    browser.press("Tab")
    ring = browser.eval("(function(){ var a = document.activeElement, s = getComputedStyle(a);"
                        " return [a.tagName, s.outlineStyle, s.outlineWidth, a.matches(':focus-visible')]; })()")
    assert ring[3] is True and ring[1] == "solid" and ring[2] == "2px", ring


def test_the_at_a_glance_grid_is_two_columns_on_a_phone_and_seven_on_a_desktop(board, browser):
    cols = "getComputedStyle(document.querySelector('.ag-grid7')).gridTemplateColumns.split(' ').length"
    _open(browser, board, PHONE)
    assert browser.eval(cols) == 2
    _open(browser, board, DESKTOP)
    assert browser.eval(cols) == 7


def test_the_iron_butterfly_legs_do_not_scroll_on_a_phone(board, browser):
    over = "getComputedStyle(document.querySelector('.ib-legs')).overflowX"
    _open(browser, board, PHONE)
    assert browser.eval(over) == "visible"
    _open(browser, board, DESKTOP)
    assert browser.eval(over) == "auto"


# ── #195 review SHOULD 4: phone readability ──
def test_intraday_is_not_a_label_under_spot_on_a_phone(board, browser):
    """On a desktop 'Intraday' names the row of (VOL) values; stacked under Spot it read as Spot's own."""
    shown = "getComputedStyle(document.querySelector('.ag-h-vol')).display"
    _open(browser, board, PHONE)
    assert browser.eval(shown) == "none"
    _open(browser, board, DESKTOP)
    assert browser.eval(shown) != "none"


def test_a_stacked_summary_metric_is_a_heading_with_a_rule_between_metrics(board, browser):
    _open(browser, board, PHONE)
    got = browser.eval("""(function(){ var tr = document.querySelector('.summary-card table.stack tbody tr'),
      td = tr.firstElementChild, s = getComputedStyle(tr);
      return {label: getComputedStyle(td, '::before').display, rule: s.borderTopWidth, pad: s.paddingTop,
              weight: getComputedStyle(td).fontWeight, cells: tr.children.length}; })()""")
    assert got["label"] == "none", f"the metric name repeats a METRIC label: {got}"
    assert got["rule"] != "0px" and got["pad"] != "0px", f"no separator between metrics: {got}"
    assert int(got["weight"]) >= 600, got


def test_an_iron_butterfly_leg_is_two_columns_on_a_phone(board, browser):
    _open(browser, board, PHONE)
    got = browser.eval("""(function(){ var tr = document.querySelector('.ib-legs table.stack tbody tr'), s = getComputedStyle(tr);
      return [s.display, s.gridTemplateColumns.split(' ').length, Math.round(tr.getBoundingClientRect().height)]; })()""")
    assert got[0] == "grid" and got[1] == 2, got
    assert got[2] <= 150, f"a leg card is {got[2]} px tall"
