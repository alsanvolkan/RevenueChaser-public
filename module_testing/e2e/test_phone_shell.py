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

LAYOUT = r"""(function(){
  var vw = document.documentElement.clientWidth;
  function box(el){ var r = el.getBoundingClientRect(); return {l: r.left, r: r.right, t: r.top, w: r.width, h: r.height}; }
  function shown(el){ var r = el.getBoundingClientRect(); var cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && r.right > 0 && r.left < vw; }
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
    browser.open(f"{dashboard.url}/#{tab}", settle=1.0)
    return browser.eval(LAYOUT)


def _click(browser, selector):
    browser.eval(f"document.querySelector({selector!r}).click()")
    browser.eval("new Promise(function(r){ setTimeout(r, 400); })")   # the drawer's 0.2 s transition
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


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_nav_is_reachable_through_the_drawer(dashboard, browser, scheme):
    m = _open(browser, dashboard, PHONE, scheme)
    assert m["sidebar"]["r"] <= 0, "the sidebar should start closed, off screen"
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


def test_light_and_dark_follow_the_device(dashboard, browser):
    assert _open(browser, dashboard, PHONE, "dark")["bg"] == DARK_BG, "the dark look must stay the shell's own"
    assert _open(browser, dashboard, PHONE, "light")["bg"] == LIGHT_BG


def test_the_toggle_overrides_the_device_and_is_remembered(dashboard, browser):
    m = _open(browser, dashboard, PHONE, "dark")
    assert m["theme"] is None and m["bg"] == DARK_BG
    m = _click(browser, "#theme-toggle")                       # auto -> light
    assert m["theme"] == "light" and m["bg"] == LIGHT_BG
    assert browser.eval(f"localStorage.getItem({THEME_STORAGE_KEY!r})") == "light"
    frame_theme = browser.eval('document.querySelector(".frame.on").contentDocument.documentElement'
                               '.getAttribute("data-theme")')
    assert frame_theme == "light", "the board frame is told the choice"
    browser.open(f"{dashboard.url}/#runs", settle=1.0)
    m = browser.eval(LAYOUT)
    assert m["theme"] == "light" and m["bg"] == LIGHT_BG, "the choice survives a reload"
    m = _click(browser, "#theme-toggle")                       # light -> dark
    assert m["theme"] == "dark" and m["bg"] == DARK_BG
    m = _click(browser, "#theme-toggle")                       # dark -> auto (the device says dark)
    assert m["theme"] is None and m["bg"] == DARK_BG
    assert browser.eval(f"localStorage.getItem({THEME_STORAGE_KEY!r})") is None


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_desktop_keeps_its_sidebar(dashboard, browser, scheme):
    m = _open(browser, dashboard, DESKTOP, scheme)
    assert m["sidebar"]["l"] == 0 and round(m["sidebar"]["w"]) == 240, "the sidebar is a column on a desktop"
    assert not m["toggleShown"], "no menu button on a desktop"
    assert m["docScroll"] <= m["vw"]
