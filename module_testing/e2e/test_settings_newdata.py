"""PR 2: the "New data system" section of Settings, end to end, against a real dashboard and a real browser.

Refresh of ``module_settings`` makes no provider call; a Fetch click puts one row in the builder queue and the queue
card shows it; a paused dataset is skipped by one builder pass (``build run --once``, a subprocess on the same
scratch roots) and the next Refresh shows its ``skipped: paused`` check. On a 390 x 844 phone the section is one
column, nothing in it reaches past the right edge, and every button is at least 44 px, in light and in dark.

These replace ``test_phone_shell``'s ``module_settings`` cases (red on main since 27 Oct: the old inventory's two
``<code>`` elements reach past a phone's edge); the old inventory is removed in PR 21.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from RevenueChaser.core.reporting_common import THEME_TOKENS

from .harness.cdp_browser import Page, find_chrome
from .harness.shared import ops_rows, running_dashboard

_APP = "module_settings"
PHONE = (390, 844)
REPO_PARENT = Path(__file__).resolve().parents[3]


@pytest.fixture
def market(tmp_path):
    return {"RC_MARKET_DIR": str(tmp_path / "market"), "RC_MARKET_RAW_DIR": str(tmp_path / "market_raw")}


@pytest.fixture
def dashboard(e2e_dirs, e2e_env, tmp_path, market):
    """A dashboard on its own market roots, so its queue is this test's alone."""
    store, cache = e2e_dirs
    with running_dashboard(tmp_path, store, cache, {**e2e_env, **market}) as dash:
        yield dash


@pytest.fixture(scope="module")
def chrome():
    path = find_chrome()
    if path is None:
        msg = "no Chrome/Chromium for the Settings phone check: set RC_CHROME or install chromium"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg + " (a skip on CI would leave the check unenforced)")
        pytest.skip(msg)
    return path


@pytest.fixture
def browser(chrome):
    with Page(chrome) as page:
        yield page


def _requests(market) -> list[dict]:
    return ops_rows(market["RC_MARKET_DIR"], "build_requests")


def _open_board(browser, dashboard, scheme="dark"):
    browser.emulate(*PHONE, scheme=scheme)
    browser.open(f"{dashboard.url}/view/{_APP}", ready="!!document.getElementById('newdata')")
    browser.eval(f"document.documentElement.setAttribute('data-theme', {scheme!r})")
    # Since the control board (design v28) these sections sit folded below it: open the fold, as a reader would.
    browser.eval("document.getElementById('old-settings').open = true")


LAYOUT = r"""(function(){
  var sec = document.getElementById('newdata'), vw = document.documentElement.clientWidth;
  var r = sec.getBoundingClientRect();
  var wide = Array.prototype.filter.call(sec.querySelectorAll('*'), function(el){
    var b = el.getBoundingClientRect(); return b.width > 0 && b.right > r.right + 0.5;
  }).map(function(el){ return el.tagName + '.' + el.className; });
  var cards = Array.prototype.map.call(sec.querySelectorAll('.nd-card'), function(el){
    var b = el.getBoundingClientRect(); return {l: Math.round(b.left), w: Math.round(b.width)}; });
  var small = Array.prototype.filter.call(sec.querySelectorAll('button, input, summary'), function(el){
    var b = el.getBoundingClientRect(); return b.width > 0 && (b.height < 44 || b.width < 44);
  }).map(function(el){ return el.tagName + ' ' + el.textContent.trim().slice(0, 20); });
  return {vw: vw, secRight: r.right, secScroll: sec.scrollWidth, secW: sec.clientWidth, wide: wide.slice(0, 8),
          cards: cards, small: small, buttons: sec.querySelectorAll('button').length,
          bg: getComputedStyle(sec).backgroundColor};
})()"""


def _rgb(hex_value: str) -> str:
    v = hex_value.lstrip("#")
    return "rgb({}, {}, {})".format(*(int(v[i:i + 2], 16) for i in (0, 2, 4)))


def _card_js(ds: str) -> str:
    return f"document.querySelector('#newdata .nd-dataset[data-dataset=\"{ds}\"]')"


def _backfill_click(browser, ds: str, day: str) -> str:
    card = _card_js(ds)
    # The card holds two date inputs since the schedule fields landed (the backfill day and the hold), so the
    # backfill day is taken by its own hook, not as "the first date input".
    browser.eval(f"{card}.querySelector('[data-nd-from]').value = {day!r}")
    browser.eval(f"{card}.querySelector('button[data-nd-act=\"backfill\"]').click()")
    return browser.eval(f"{card}.querySelector('[data-nd-msg]').textContent")
