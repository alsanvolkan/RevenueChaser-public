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
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from RevenueChaser.core.reporting_common import THEME_TOKENS

from .harness.cdp_browser import Page, find_chrome
from .harness.shared import running_dashboard

_APP = "module_settings"
PHONE = (390, 844)
REPO_PARENT = Path(__file__).resolve().parents[3]


@pytest.fixture
def market(tmp_path):
    return {"RC_MARKET_DIR": str(tmp_path / "market"), "RC_MARKET_RAW_DIR": str(tmp_path / "market_raw"),
            "RC_MARKET_RAW_COPY_DIR": ""}


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
    path = Path(market["RC_MARKET_DIR"]) / "ops.sqlite"
    if not path.exists():
        return []
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM build_requests ORDER BY id")]
    finally:
        con.close()


def _open_board(browser, dashboard, scheme="dark"):
    browser.emulate(*PHONE, scheme=scheme)
    browser.open(f"{dashboard.url}/view/{_APP}", ready="!!document.getElementById('newdata')")
    browser.eval(f"document.documentElement.setAttribute('data-theme', {scheme!r})")
    # Since the control board (design v28) these sections sit folded below it: open the fold, as a reader would.
    browser.eval("document.getElementById('old-settings').open = true")


def test_a_settings_refresh_makes_no_provider_call(dashboard, provider_routes):
    before, yf = len(provider_routes.requests()), len(dashboard.yf_calls())
    r = dashboard.view(_APP, refresh=True)
    assert r.status_code == 200 and 'id="newdata"' in r.text
    assert len(provider_routes.requests()) == before, [q.path for q in provider_routes.requests()[before:]]
    assert len(dashboard.yf_calls()) == yf


def test_a_fetch_click_enqueues_one_row_and_the_queue_shows_it(dashboard, browser, market, provider_routes):
    before = len(provider_routes.requests())
    _open_board(browser, dashboard)
    browser.eval("document.querySelector('#newdata button[data-nd-act=\"fetch\"][data-dataset=\"market_series\"]')"
                 ".click()")
    browser.wait_for("document.getElementById('nd-queue-active').textContent.indexOf('market_series') >= 0")
    rows = _requests(market)
    assert [(r["dataset"], r["status"], r["requested_by"]) for r in rows] == [("market_series", "queued", "settings")]
    msg = browser.eval("document.querySelector('.nd-dataset[data-dataset=\"market_series\"] [data-nd-msg]')"
                       ".textContent")
    assert "Queued as request #" in msg
    assert len(provider_routes.requests()) == before, "a request is not a fetch: no provider was asked"


def test_a_paused_dataset_is_skipped_by_one_builder_pass_and_the_page_shows_it(dashboard, market, e2e_env):
    from RevenueChaser.rcdata.build import DATASETS
    # `run --once` also queues every never-built dataset (#223's due step), and this subprocess is outside the root
    # conftest's network block: pause every dataset (the spine's of PR 5 too), so the pass sends no call at all.
    for name in DATASETS:
        assert dashboard.client.post(f"/api/build/schedule/{name}/pause").status_code == 200, name
    assert dashboard.client.post("/api/build/request/market_series").status_code == 202
    env = {**os.environ, **e2e_env, **market, "RC_STORE_DIR": str(dashboard.store_dir),
           "RC_CACHE_DIR": str(dashboard.cache_dir), "RC_STORE_AUTO_ENSURE": "0"}
    r = subprocess.run([sys.executable, "-m", "RevenueChaser.rcdata.build", "run", "--once"], cwd=str(REPO_PARENT),
                       env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    assert [x["status"] for x in _requests(market)] == ["skipped"]
    html = dashboard.view(_APP, refresh=True).text
    # A dataset row is a <details> since the review's M1, and it holds <details> of its own, so the row ends where
    # the next row begins.
    row = '<details class="nd-card nd-dataset"'
    card = html[html.index('data-dataset="market_series" data-status='):]
    card = card[:card.index(row, 1) if row in card[1:] else len(card)]
    assert "skipped: paused by e2e" in card and ">Resume</button>" in card


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


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_section_is_one_column_on_a_phone_with_44px_targets_and_no_sideways_scroll(dashboard, browser, scheme):
    _open_board(browser, dashboard, scheme)
    browser.eval("document.querySelectorAll('#newdata details').forEach(function(d){ d.open = true; })")
    m = browser.eval(LAYOUT)
    assert m["vw"] == PHONE[0]
    assert m["secRight"] <= m["vw"] + 0.5, "the section reaches past the phone's edge"
    assert m["secScroll"] <= m["secW"], "the section scrolls sideways"
    assert not m["wide"], f"elements reach past the section's edge: {m['wide']}"
    assert len({c["l"] for c in m["cards"]}) == 1, f"not one column: {m['cards']}"
    assert len({c["w"] for c in m["cards"]}) == 1, "the cards are not full width"
    assert m["buttons"] >= 6 and not m["small"], f"targets under 44 px: {m['small']}"
    assert m["bg"] == _rgb(THEME_TOKENS[scheme]["bg"]), "the section does not follow the shell's tokens"


def _card_js(ds: str) -> str:
    return f"document.querySelector('#newdata .nd-dataset[data-dataset=\"{ds}\"]')"


def _backfill_click(browser, ds: str, day: str) -> str:
    card = _card_js(ds)
    browser.eval(f"{card}.querySelector('input.nd-date').value = {day!r}")
    browser.eval(f"{card}.querySelector('button[data-nd-act=\"backfill\"]').click()")
    return browser.eval(f"{card}.querySelector('[data-nd-msg]').textContent")


def test_a_backfill_without_an_estimate_or_over_1000_calls_waits_for_a_second_click(dashboard, browser, market,
                                                                                    provider_routes):
    """Review should-fix 1: the confirm step is the page's only brake on a large backfill (the route queues any)."""
    before = len(provider_routes.requests())
    _open_board(browser, dashboard)
    # The market series have no call estimate: the page asks first and queues nothing.
    msg = _backfill_click(browser, "market_series", "2026-01-05")
    assert "not estimated" in msg and _requests(market) == []
    # Over 1,000 calls: the SPX slice made to miss 1,500 sessions from the chosen day on.
    browser.eval("ND.datasets['uw/spot_expiry_strike'].missing = "
                 "Array.from({length: 1500}, function(_, i){ return '2026-01-' + String(10 + (i % 18)); })")
    msg = _backfill_click(browser, "uw/spot_expiry_strike", "2026-01-05")
    assert "about 3,000 calls" in msg and _requests(market) == []       # two calls a session
    # The second click queues exactly the one backfill that was asked for.
    browser.eval(f"{_card_js('market_series')}.querySelector('button[data-nd-act=\"backfill-confirm\"]').click()")
    browser.wait_for("document.getElementById('nd-queue-active').textContent.indexOf('market_series') >= 0")
    rows = _requests(market)
    assert [(r["dataset"], r["params"]) for r in rows] == [
        ("market_series", '{"from": "2026-01-05", "mode": "backfill"}')]
    assert len(provider_routes.requests()) == before


def test_a_small_estimated_backfill_is_queued_on_the_first_click(dashboard, browser, market):
    _open_board(browser, dashboard)
    browser.eval("ND.datasets['uw/spot_expiry_strike'].missing = ['2026-10-20', '2026-10-21']")
    _backfill_click(browser, "uw/spot_expiry_strike", "2026-10-20")
    browser.wait_for("document.getElementById('nd-queue-active').textContent.indexOf('spot_expiry_strike') >= 0")
    msg = browser.eval(f"{_card_js('uw/spot_expiry_strike')}.querySelector('[data-nd-msg]').textContent")
    assert "about 4 calls" in msg
    assert [r["dataset"] for r in _requests(market)] == ["uw/spot_expiry_strike"]
