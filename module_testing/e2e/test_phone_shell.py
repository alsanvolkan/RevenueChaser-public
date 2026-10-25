"""The shell on a phone, in a real browser against the real dashboard (#184).

At an exact 390 px viewport (device emulation, ``harness/cdp_browser.py``): the page never scrolls sideways,
the nav is reachable through the drawer, the shell's touch targets are at least 44 px, and light and dark
both render, by the device's scheme and by the toggle. A 1280 px desktop keeps its sidebar.

The dashboard runs on a throwaway store with every provider pointed at the mock server (``conftest.py``);
nothing here starts a job. Boards still wider than a phone scroll inside their own frame, which is each
board's own PR; this checks the shell around them.
"""
from __future__ import annotations

import os

import pytest

from RevenueChaser.core.reporting_common import THEME_STORAGE_KEY, THEME_TOKENS

from .harness.cdp_browser import Page, find_chrome

PHONE = (390, 844)
DESKTOP = (1280, 800)


def _rgb(hex_value: str) -> str:
    v = hex_value.lstrip("#")
    return "rgb({}, {}, {})".format(*(int(v[i:i + 2], 16) for i in (0, 2, 4)))


DARK_BG, LIGHT_BG = _rgb(THEME_TOKENS["dark"]["bg"]), _rgb(THEME_TOKENS["light"]["bg"])

# The active board frame has loaded its page (the shell fits the frame on that load).
FRAME_READY = ("(function(){ var f = document.querySelector('.frame.on'); try {"
               " return !!(f && f.getAttribute('src') && f.contentDocument.readyState === 'complete'"
               " && f.contentDocument.location.href !== 'about:blank'); } catch (e) { return false; } })()")

LAYOUT = r"""(function(){
  var vw = document.documentElement.clientWidth;
  function box(el){ var r = el.getBoundingClientRect(); return {l: r.left, r: r.right, t: r.top, w: r.width, h: r.height}; }
  var vh = window.innerHeight;
  function shown(el){ var r = el.getBoundingClientRect(); var cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && r.right > 0 && r.left < vw
           && r.bottom > 0 && r.top < vh; }
  var content = document.getElementById("content");
  var frame = document.querySelector(".frame.on");
  var small = [];
  document.querySelectorAll(".topbar button, .topbar input, .sidebar .nav-item, .sidebar .nav-header, .rc-open-issues > summary")
    .forEach(function(el){ if(!shown(el)) return;
      var b = box(el.closest("label") || el);       // a labelled checkbox is tapped through its label
      if(b.h < 44 || b.w < 44) small.push((el.id || el.className || el.tagName) + " " + Math.round(b.w) + "x" + Math.round(b.h)); });
  var wide = Array.prototype.filter.call(document.querySelectorAll("body *"), function(el){
    if(el.closest(".sidebar") && !document.body.classList.contains("nav-open")) return false;
    var r = el.getBoundingClientRect(); return r.width > 0 && r.right > vw + 0.5 && getComputedStyle(el).position !== "fixed";
  }).map(function(el){ return el.tagName + (el.id ? "#" + el.id : "") + "." + el.className; });
  return {vw: vw, docScroll: document.documentElement.scrollWidth, bodyScroll: document.body.scrollWidth,
          contentScroll: content.scrollWidth, contentW: content.clientWidth,
          frameW: frame ? frame.getBoundingClientRect().width : null, wide: wide.slice(0, 8),
          // How much of the screen's height the board frame actually gets.
          frameShown: frame ? Math.max(0, Math.min(frame.getBoundingClientRect().bottom, vh)
                                          - Math.max(frame.getBoundingClientRect().top, 0)) : 0, vh: vh,
          sidebar: box(document.getElementById("sidebar")), toggle: box(document.getElementById("nav-toggle")),
          toggleShown: shown(document.getElementById("nav-toggle")),
          expanded: document.getElementById("nav-toggle").getAttribute("aria-expanded"),
          small: small, bg: getComputedStyle(document.body).backgroundColor,
          theme: document.documentElement.getAttribute("data-theme"),
          active: (document.querySelector(".nav-item.on") || {}).getAttribute
                  ? document.querySelector(".nav-item.on").getAttribute("data-tab") : null,
          title: document.getElementById("tab-title").textContent};
})()"""


@pytest.fixture(scope="module")
def chrome():
    path = find_chrome()
    if path is None:
        msg = "no Chrome/Chromium for the phone shell check: set RC_CHROME or install google-chrome / chromium"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg + " (a skip on CI would leave the check unenforced)")
        pytest.skip(msg)
    return path


@pytest.fixture
def browser(chrome):
    with Page(chrome) as page:          # a fresh profile per test: no theme choice carries over
        yield page


def _open(browser, dashboard, size, scheme, tab="runs"):
    browser.emulate(*size, scheme=scheme)
    browser.open(f"{dashboard.url}/#{tab}", ready=FRAME_READY)
    return browser.eval(LAYOUT)


def _click(browser, selector):
    browser.eval(f"document.querySelector({selector!r}).click()")
    browser.settle()                                           # the drawer's slide, however long it takes
    return browser.eval(LAYOUT)


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("tab", ["runs", "module_settings", "module_earnings_radar"])
def test_the_page_never_scrolls_sideways_on_a_phone(dashboard, browser, scheme, tab):
    m = _open(browser, dashboard, PHONE, scheme, tab)
    assert m["vw"] == PHONE[0]
    assert m["docScroll"] <= m["vw"] and m["bodyScroll"] <= m["vw"], f"the page scrolls sideways: {m['wide']}"
    assert m["contentScroll"] <= m["contentW"], "the board pane scrolls sideways"
    assert m["frameW"] is not None and m["frameW"] <= m["contentW"], f"the frame ({m['frameW']}) is wider than its pane"
    assert not m["wide"], f"elements reach past the right edge: {m['wide']}"
    assert m["frameShown"] >= m["vh"] / 2, f"the board gets {m['frameShown']} of {m['vh']} px of the screen"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_nav_is_reachable_through_the_drawer(dashboard, browser, scheme):
    m = _open(browser, dashboard, PHONE, scheme)
    assert m["sidebar"]["r"] <= 0, "the sidebar should start closed, off screen"
    assert m["frameShown"] >= m["vh"] / 2, "the board, not the closed drawer, fills the screen"
    assert m["toggleShown"] and m["expanded"] == "false"
    m = _click(browser, "#nav-toggle")
    assert m["expanded"] == "true"
    assert m["sidebar"]["l"] >= 0 and m["sidebar"]["r"] <= m["vw"], f"the drawer is not on screen: {m['sidebar']}"
    assert not m["small"], f"touch targets under 44 px: {m['small']}"
    m = _click(browser, '.nav-item[data-tab="module_settings"]')
    assert m["active"] == "module_settings" and m["title"] == "Settings / Data health"
    assert m["expanded"] == "false" and m["sidebar"]["r"] <= 0, "choosing a tab closes the drawer"
    m = _click(browser, "#nav-toggle")
    m = _click(browser, "#nav-scrim")
    assert m["expanded"] == "false", "the scrim closes the drawer"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_shells_touch_targets_are_44_px_on_a_phone(dashboard, browser, scheme):
    for tab in ("runs", "module_earnings_radar", "module_sector_rotation"):
        m = _open(browser, dashboard, PHONE, scheme, tab)
        assert not m["small"], f"{tab}: touch targets under 44 px: {m['small']}"


def test_a_first_visit_is_dark_on_any_device(dashboard, browser):
    """avo, 25 Oct 2026: the default is dark. A light phone shows light only once the reader chooses it."""
    assert _open(browser, dashboard, PHONE, "dark")["bg"] == DARK_BG, "the dark look must stay the shell's own"
    m = _open(browser, dashboard, PHONE, "light")
    assert m["bg"] == DARK_BG and m["theme"] == "dark", f"a light device got {m['bg']} on a first visit"
    _click(browser, "#nav-toggle")
    _click(browser, "#theme-toggle")                           # dark -> light
    m = _click(browser, "#theme-toggle")                       # light -> auto: follows the light device
    assert m["theme"] == "auto" and m["bg"] == LIGHT_BG


def test_the_toggle_overrides_the_device_and_is_remembered(dashboard, browser):
    m = _open(browser, dashboard, PHONE, "dark")
    assert m["theme"] == "dark" and m["bg"] == DARK_BG
    _click(browser, "#nav-toggle")                             # on a phone the toggle is in the drawer
    m = _click(browser, "#theme-toggle")                       # dark -> light
    assert m["theme"] == "light" and m["bg"] == LIGHT_BG
    assert browser.eval(f"localStorage.getItem({THEME_STORAGE_KEY!r})") == "light"
    frame_theme = browser.eval('document.querySelector(".frame.on").contentDocument.documentElement'
                               '.getAttribute("data-theme")')
    assert frame_theme == "light", "the board frame is told the choice"
    browser.open(f"{dashboard.url}/#runs", ready=FRAME_READY)
    m = browser.eval(LAYOUT)
    assert m["theme"] == "light" and m["bg"] == LIGHT_BG, "the choice survives a reload"
    _click(browser, "#nav-toggle")
    m = _click(browser, "#theme-toggle")                       # light -> auto (the device says dark)
    assert m["theme"] == "auto" and m["bg"] == DARK_BG
    assert browser.eval(f"localStorage.getItem({THEME_STORAGE_KEY!r})") == "auto"
    m = _click(browser, "#theme-toggle")                       # auto -> dark, the default: stored as nothing
    assert m["theme"] == "dark" and m["bg"] == DARK_BG
    assert browser.eval(f"localStorage.getItem({THEME_STORAGE_KEY!r})") is None


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_desktop_keeps_its_sidebar(dashboard, browser, scheme):
    m = _open(browser, dashboard, DESKTOP, scheme)
    assert m["sidebar"]["l"] == 0 and round(m["sidebar"]["w"]) == 240, "the sidebar is a column on a desktop"
    assert not m["toggleShown"], "no menu button on a desktop"
    assert m["docScroll"] <= m["vw"]


def _pixel(browser, x: float, y: float) -> tuple[int, int, int]:
    """The colour Chrome actually painted at (x, y) CSS px: a computed style cannot see a canvas default."""
    import base64
    import io

    from PIL import Image
    shot = browser.send("Page.captureScreenshot", format="png")
    return Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB").getpixel((int(x), int(y)))


@pytest.mark.parametrize("size", [PHONE, DESKTOP])
@pytest.mark.parametrize("tab", ["module_financials", "module_spx_gamma"])
def test_a_board_without_its_own_background_is_not_painted_white_in_dark(dashboard, browser, size, tab):
    """#190 review: color-scheme:dark on the shell made Chrome paint a board page that declares no colour
    scheme and no background white, inside a dark shell (an empty board renders such a page)."""
    browser.emulate(*size, scheme="dark")
    browser.open(f"{dashboard.url}/#{tab}", ready=FRAME_READY)
    x, y = browser.eval("(function(){ var r = document.querySelector('.frame.on').getBoundingClientRect();"
                        " return [r.left + r.width / 2, r.bottom - 40]; })()")
    r, g, b = _pixel(browser, x, y)
    assert 0.2126 * r + 0.7152 * g + 0.0722 * b < 80, f"{tab} frame painted {(r, g, b)} in dark"


@pytest.mark.parametrize("tab, button, popover", [
    ("module_sector_rotation", "range-btn", "cal-popover"),
    ("module_earnings_radar", "earnings-range-btn", "earnings-cal-popover"),
    ("module_spx_gamma", "spxgamma-range-btn", "spxgamma-cal-popover"),
])
def test_a_calendar_opens_on_screen_below_its_button_on_a_phone(dashboard, browser, tab, button, popover):
    """#190 review: position:fixed with top:auto left the pop-up's top edge 70-119 px above the screen."""
    _open(browser, dashboard, PHONE, "dark", tab)
    browser.eval(f"document.getElementById({button!r}).click()")
    m = browser.eval(f"""(function(){{
      var p = document.getElementById({popover!r}).getBoundingClientRect();
      var b = document.getElementById({button!r}).getBoundingClientRect();
      return {{top: p.top, bottom: p.bottom, left: p.left, right: p.right, h: p.height, btn: b.bottom,
               vh: window.innerHeight, vw: document.documentElement.clientWidth}};
    }})()""")
    assert m["h"] > 0, "the pop-up did not open"
    assert m["top"] >= m["btn"], f"the pop-up covers its own button or sits above it: {m}"
    assert m["bottom"] <= m["vh"] and m["left"] >= 0 and m["right"] <= m["vw"], f"not fully on screen: {m}"


@pytest.mark.parametrize("tab", ["module_earnings_radar", "module_swing_potential", "module_fundamentals",
                                 "module_sec_13f", "module_sector_rotation"])
def test_the_theme_toggle_does_not_wrap_the_desktop_top_bar(dashboard, browser, tab):
    """#190 review: the toggle pushed the 1280 px top bar onto a second line (56 -> 93 px on Earnings)."""
    browser.emulate(*DESKTOP, scheme="dark")
    browser.open(f"{dashboard.url}/#{tab}", ready=FRAME_READY)
    with_toggle, without = browser.eval("""(function(){
      var bar = document.querySelector('.topbar'), h = bar.getBoundingClientRect().height;
      bar.querySelectorAll('.theme-toggle').forEach(function(b){ b.remove(); });
      return [h, bar.getBoundingClientRect().height];
    })()""")
    assert with_toggle == without, f"{tab}: the toggle grows the top bar from {without} to {with_toggle} px"


def test_a_desktop_toggles_the_theme_from_the_sidebar(dashboard, browser):
    browser.emulate(*DESKTOP, scheme="dark")
    browser.open(f"{dashboard.url}/#runs", ready=FRAME_READY)
    shown = browser.eval("(function(){ var r = document.getElementById('theme-toggle').getBoundingClientRect();"
                         " return r.width > 0 && r.bottom <= innerHeight; })()")
    assert shown, "the sidebar toggle is not on screen"
    browser.eval("document.getElementById('theme-toggle').click()")
    assert browser.eval("document.documentElement.getAttribute('data-theme')") == "light"
    assert browser.eval("document.getElementById('theme-toggle').textContent") == "Theme: Light"


def test_the_summaries_keep_their_arrow_on_a_phone(dashboard, browser):
    """#190 review: the 44 px rule made "What am I looking at?", "Recorded sessions" and "Open issues" lose
    their open/closed arrow on a phone or a touch screen."""
    _open(browser, dashboard, PHONE, "dark", "module_settings")
    shell = browser.eval("Array.prototype.map.call(document.querySelectorAll('details > summary'),"
                         " function(s){ return getComputedStyle(s).display; })")
    board = browser.eval("Array.prototype.map.call(document.querySelector('.frame.on').contentDocument"
                         ".querySelectorAll('details.help > summary'), function(s){"
                         " var r = s.getBoundingClientRect(); return [getComputedStyle(s).display, r.height]; })")
    assert shell and all(d == "list-item" for d in shell), f"shell summaries: {shell}"
    assert board and all(d == "list-item" for d, _ in board), f"board help summary: {board}"
    assert all(h >= 44 for _, h in board), f"the help summary is under 44 px: {board}"


@pytest.mark.parametrize("tab", ["runs", "module_earnings_radar", "module_sector_rotation", "module_spx_gamma",
                                 "module_sec_13f", "module_financials"])
def test_the_phone_top_bar_leaves_the_board_most_of_the_screen(dashboard, browser, tab):
    """#190 review: the top bar took 262 px of 844 on Earnings and 314 on Sector Rotation."""
    _open(browser, dashboard, PHONE, "dark", tab)
    h = browser.eval("document.querySelector('.topbar').getBoundingClientRect().height")
    assert h <= PHONE[1] / 4, f"{tab}: the top bar is {h} px of {PHONE[1]}"


FOCUS = ("(function(){ var a = document.activeElement; return {tag: a.tagName, id: a.id,"
         " tab: a.getAttribute('data-tab'), inSidebar: !!a.closest('#sidebar'), inMain: !!a.closest('.main')}; })()")


def test_the_drawer_works_from_the_keyboard(dashboard, browser):
    """#190 review: focus stayed on the menu button when the drawer opened, Tab went on into the board behind
    the scrim, and choosing a tab dropped focus to <body>."""
    _open(browser, dashboard, PHONE, "dark", "runs")
    browser.eval("document.getElementById('nav-toggle').focus()")
    browser.press("Enter")
    browser.settle()
    f = browser.eval(FOCUS)
    assert f["inSidebar"] and f["tab"] == "runs", f"opening the drawer left focus on {f}"
    for _ in range(40):                       # well past every control in the drawer
        browser.press("Tab")
        f = browser.eval(FOCUS)
        assert not f["inMain"] and f["tag"] != "IFRAME", f"Tab reached the page behind the drawer: {f}"
    browser.eval("document.querySelector('.nav-item[data-tab=\"module_settings\"]').focus()")
    browser.press("Enter")
    browser.settle()
    m = browser.eval(LAYOUT)
    f = browser.eval(FOCUS)
    assert m["active"] == "module_settings" and m["expanded"] == "false"
    assert f["id"] == "nav-toggle", f"choosing a tab left focus on {f}"
    browser.press("Enter")                    # focus is on the menu button: open again
    browser.settle()
    browser.press("Escape")
    browser.settle()
    assert browser.eval(LAYOUT)["expanded"] == "false"
    assert browser.eval(FOCUS)["id"] == "nav-toggle", "Escape returns focus to the menu button"


def test_the_browser_bar_follows_the_manual_choice(dashboard, browser):
    _open(browser, dashboard, PHONE, "dark", "runs")
    _click(browser, "#nav-toggle")
    _click(browser, "#theme-toggle")                           # auto -> light, on a dark device
    metas = browser.eval("Array.prototype.map.call(document.querySelectorAll('meta[name=theme-color]'),"
                         " function(m){ return m.content; })")
    assert metas and set(metas) == {THEME_TOKENS["light"]["bg"]}, metas


def test_an_open_drawer_closes_when_the_window_grows_past_the_breakpoint(dashboard, browser):
    _open(browser, dashboard, PHONE, "dark", "runs")
    _click(browser, "#nav-toggle")
    browser.emulate(*DESKTOP, scheme="dark")
    browser.wait_for("!document.body.classList.contains('nav-open')", timeout=5)
    assert browser.eval("document.querySelector('.main').inert") is False


def test_a_board_gets_the_choice_again_when_its_tab_is_shown(dashboard, browser):
    """A board that re-rendered its document since it loaded (the shell only marked it on load) is marked
    again when its tab comes back."""
    _open(browser, dashboard, DESKTOP, "dark", "runs")
    browser.eval("document.getElementById('theme-toggle').click()")          # light
    browser.eval("document.querySelector('.frame.on').contentDocument.documentElement.removeAttribute('data-theme')")
    browser.eval("showTab('module_settings'); showTab('runs')")
    assert browser.eval("document.querySelector('.frame.on').contentDocument.documentElement"
                        ".getAttribute('data-theme')") == "light"
