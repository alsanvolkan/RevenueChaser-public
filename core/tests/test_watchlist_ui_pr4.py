"""The shared "add to list" button and its menu (PR 4, D2; one menu since #786 step 5; ADR-X1):
``WATCHLIST_JS`` / ``WATCHLIST_CSS`` / ``watch_buttons_html``.

The server half is checked as markup; the client half runs under node against a small fake DOM, so the click handler
is seen reading ``data-ticker``, opening the menu of every list, and posting a tick to the menu's route, and refusing
a ticker the dashboard would refuse.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from RevenueChaser.core import reporting_common as rc
from RevenueChaser.core.tests.test_one_escaper_175 import check_page

HOSTILE = ["<script>alert(1)</script>", "\"><img src=x onerror=alert(1)>", "A'B", "A B", "", None, "ABCDEFGHIJK",
           "-AAA", "../etc", "A&B", "NVDA\n"]


def _buttons(html: str) -> list[str]:
    return re.findall(r"<button\b[^>]*>.*?</button>", html, re.S)


def test_the_ticker_rule_is_the_dashboards():
    from RevenueChaser.app import dashboard
    assert rc.WATCH_TICKER_PATTERN == dashboard._TICKER_RE.pattern
    assert dashboard._TICKER_RE.pattern in rc.WATCHLIST_JS


@pytest.mark.parametrize("ticker", HOSTILE)
def test_a_ticker_the_dashboard_refuses_gets_no_button(ticker):
    assert rc.watch_buttons_html(ticker) == "" or ticker == "NVDA\n"


def test_a_trailing_newline_is_stripped_like_the_route_does():
    assert 'data-ticker="NVDA"' in rc.watch_buttons_html("NVDA\n")


def test_one_labelled_menu_button_with_data_ticker_and_no_inline_handler():
    out = rc.watch_buttons_html("brk.b")
    assert len(_buttons(out)) == 1
    assert 'data-rc-watch-menu="" data-ticker="BRK.B"' in out
    assert 'aria-haspopup="menu" aria-expanded="false"' in out
    assert 'aria-label="Add BRK.B to a watch list"' in out and 'data-on="0"' in out
    assert not re.search(r"\son\w+\s*=", out)
    for snippet in (rc.WATCHLIST_JS, rc.watchlist_html("leaps")):
        assert not re.search(r"""\son[a-z]+\s*=\s*["']""", snippet)


def test_the_button_is_a_glyph_with_a_count_and_no_word():
    """The visible body is the bookmark glyph and the number of lists; the words are the accessible name."""
    (b,) = _buttons(rc.watch_buttons_html("NVDA", on=("stock", "index")))
    assert "<svg" in b and 'aria-label="NVDA is on 2 watch lists"' in b and 'data-on="2"' in b
    body = re.sub(r"<svg.*?</svg>", "", b, flags=re.S)
    assert re.sub(r"<[^>]+>", "", body).strip() == "2"
    (one,) = _buttons(rc.watch_buttons_html("NVDA", on=("stock",)))
    assert 'aria-label="NVDA is on 1 watch list"' in one


def test_the_targets_are_44px_the_rows_44px_on_a_phone_and_use_the_theme_tokens():
    css = rc.WATCHLIST_CSS
    assert "min-width:44px" in css and "min-height:44px" in css
    phone = css[css.index("@media (max-width:600px)"):]
    assert ".rc-wm-i{min-height:44px}" in phone and "bottom:0" in phone, "a bottom sheet with 44 px rows"
    assert "var(--input-bg" in css and "var(--sky" in css and "var(--card" in css


def test_the_mount_passes_the_board_as_json_and_defines_its_escaper():
    page = "<html><body>" + rc.watchlist_html("</script><b>x") + "</body></html>"
    assert "rcWatchInit(\"\\u003c/script\\u003e\\u003cb\\u003ex\");" in page
    check_page(page)


_NODE_HARNESS = r"""
var calls = [];
var listeners = {click: [], keydown: []};
function El(attrs){ this.a = attrs || {}; this.disabled = false; this.hidden = false; this.style = {};
  this.innerHTML = ''; this.focused = false; this.children = []; }
El.prototype.getAttribute = function(k){ return this.a.hasOwnProperty(k) ? this.a[k] : null; };
El.prototype.setAttribute = function(k, v){ this.a[k] = String(v); };
El.prototype.removeAttribute = function(k){ delete this.a[k]; };
El.prototype.closest = function(sel){
  if(sel === '[data-rc-watch-menu]') return this.a.hasOwnProperty('data-rc-watch-menu') ? this : null;
  if(sel === '[data-rc-wm-list]') return this.a.hasOwnProperty('data-rc-wm-list') ? this : null;
  if(sel === '[data-rc-wm-close]') return this.a.hasOwnProperty('data-rc-wm-close') ? this : null;
  if(sel === '#rc-watch-menu') return this.inMenu ? this : null;
  return null;
};
El.prototype.querySelector = function(){ return null; };
El.prototype.querySelectorAll = function(){ return []; };
El.prototype.getBoundingClientRect = function(){ return {left: 0, right: 44, top: 0, bottom: 44}; };
El.prototype.focus = function(){ document.activeElement = this; };
El.prototype.appendChild = function(c){ this.children.push(c); };
var BUTTONS = [], BYID = {};
var document = {
  activeElement: null,
  addEventListener: function(type, fn){ (listeners[type] = listeners[type] || []).push(fn); },
  querySelectorAll: function(){ return BUTTONS; },
  getElementById: function(id){ return BYID[id] || null; },
  createElement: function(){ return new El(); },
  body: new El(),
  documentElement: {clientWidth: 1200}
};
document.body.appendChild = function(c){ if(c.id) BYID[c.id] = c; };
var window = {innerWidth: 1200, innerHeight: 800, addEventListener: function(){}};
var ANSWERS = {};
function fetch(url, opts){
  calls.push({url: url, opts: opts || null});
  var body = ANSWERS[url] || {ok: false, error: 'no route'};
  return Promise.resolve({status: body.ok ? 200 : 400, json: function(){ return Promise.resolve(body); }});
}
function click(el){
  var ev = {target: el, stopped: false, prevented: false,
            stopPropagation: function(){ this.stopped = true; }, preventDefault: function(){ this.prevented = true; }};
  listeners.click.forEach(function(fn){ fn(ev); });
  return ev;
}
function key(k){ var ev = {key: k, preventDefault: function(){}}; listeners.keydown.forEach(function(fn){ fn(ev); }); }
var LISTS = {ok: true, option: ['NVDA'], stock: [], lists: [
  {id: 'index', name: 'index', group: 'Your lists', count: 78, tickers: ['MSFT']},
  {id: 'jobs.option', name: 'Option chains', group: 'Read by the jobs', count: 1, tickers: ['NVDA']},
  {id: 'jobs.stock', name: 'Stock picks', group: 'Read by the jobs', count: 0, tickers: []}]};
"""


def _node(script: str) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    out = subprocess.run(["node", "-e", _NODE_HARNESS + rc.WATCHLIST_JS + script], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_client_buttons_escape_and_refuse_like_the_server():
    got = _node(r"""
      var out = {};
      out.hostile = %s.map(function(t){ return rcWatchButtons(t); });
      out.good = rcWatchButtons('brk.b');
      console.log(JSON.stringify(out));
    """ % json.dumps([t for t in HOSTILE if t is not None and t != "NVDA\n"]))
    assert set(got["hostile"]) == {""}
    assert got["good"] == rc.watch_buttons_html("BRK.B")


def test_init_counts_the_lists_of_every_button():
    got = _node(r"""
      var a = new El({'data-rc-watch-menu': '', 'data-ticker': 'NVDA'});
      var b = new El({'data-rc-watch-menu': '', 'data-ticker': 'AAPL'});
      BUTTONS = [a, b];
      ANSWERS['/api/watchlists'] = LISTS;
      rcWatchInit('sec_13f').then(function(){
        console.log(JSON.stringify({a: [a.a['data-on'], a.a['aria-label']], b: [b.a['data-on'], b.a['aria-label']],
                                    rendered: rcWatchButtons('MSFT')}));
      });
    """)
    assert got["a"] == ["1", "NVDA is on 1 watch list"]
    assert got["b"] == ["0", "Add AAPL to a watch list"]
    assert 'data-on="1"' in got["rendered"]


def test_a_click_opens_the_menu_of_every_list_and_a_tick_posts_with_the_board():
    got = _node(r"""
      var a = new El({'data-rc-watch-menu': '', 'data-ticker': 'NVDA'});
      BUTTONS = [a];
      ANSWERS['/api/watchlists'] = LISTS;
      ANSWERS['/api/watchlists/jobs.stock/tick/NVDA'] = {ok: true, list_id: 'jobs.stock', ticker: 'NVDA', on: true};
      rcWatchInit('leaps').then(function(){
        var ev = click(a);
        var menu = BYID['rc-watch-menu'];
        var opened = {hidden: menu.hidden, expanded: a.a['aria-expanded'], html: menu.innerHTML, stopped: ev.stopped};
        var item = new El({'data-rc-wm-list': 'jobs.stock'}); item.inMenu = true;
        click(item);
        setTimeout(function(){
          console.log(JSON.stringify({opened: opened, calls: calls, after: menu.innerHTML, on: a.a['data-on']}));
        }, 20);
      });
    """)
    o = got["opened"]
    assert o["hidden"] is False and o["expanded"] == "true" and o["stopped"] is True
    html = o["html"]
    assert html.index("Your lists") < html.index("Read by the jobs")
    assert html.count('role="menuitemcheckbox"') == 3
    assert 'aria-checked="true" data-rc-wm-list="jobs.option"' in html
    assert 'aria-checked="false" data-rc-wm-list="jobs.stock"' in html
    post = got["calls"][1]
    assert post["url"] == "/api/watchlists/jobs.stock/tick/NVDA"
    assert post["opts"]["method"] == "POST" and post["opts"]["headers"]["X-RC-Panel"] == "leaps"
    assert json.loads(post["opts"]["body"]) == {"on": True}
    assert 'aria-checked="true" data-rc-wm-list="jobs.stock"' in got["after"]
    assert got["on"] == "2"


def test_an_untick_sends_false_and_escape_closes_and_returns_focus():
    got = _node(r"""
      var a = new El({'data-rc-watch-menu': '', 'data-ticker': 'NVDA'});
      BUTTONS = [a];
      ANSWERS['/api/watchlists'] = LISTS;
      ANSWERS['/api/watchlists/jobs.option/tick/NVDA'] = {ok: true, on: false};
      rcWatchInit('leaps').then(function(){
        click(a);
        var item = new El({'data-rc-wm-list': 'jobs.option'}); item.inMenu = true;
        click(item);
        setTimeout(function(){
          key('Escape');
          console.log(JSON.stringify({body: calls[1].opts.body, hidden: BYID['rc-watch-menu'].hidden,
            expanded: a.a['aria-expanded'], focus: document.activeElement === a, on: a.a['data-on']}));
        }, 20);
      });
    """)
    assert json.loads(got["body"]) == {"on": False}
    assert got["hidden"] is True and got["expanded"] == "false" and got["focus"] is True and got["on"] == "0"


def test_a_click_on_a_button_whose_data_ticker_is_hostile_sends_nothing():
    got = _node(r"""
      var bad = new El({'data-rc-watch-menu': '', 'data-ticker': '"><img src=x onerror=alert(1)>'});
      click(bad);
      setTimeout(function(){ console.log(JSON.stringify({calls: calls, menu: !!BYID['rc-watch-menu']})); }, 20);
    """)
    assert got == {"calls": [], "menu": False}


def test_a_refused_tick_leaves_the_state_and_says_why():
    got = _node(r"""
      var a = new El({'data-rc-watch-menu': '', 'data-ticker': 'ZZZZ'});
      BUTTONS = [a];
      ANSWERS['/api/watchlists'] = LISTS;
      ANSWERS['/api/watchlists/index/tick/ZZZZ'] = {ok: false, error: 'ZZZZ is not a US listing the spine knows'};
      rcWatchInit('leaps').then(function(){
        click(a);
        var item = new El({'data-rc-wm-list': 'index'}); item.inMenu = true;
        click(item);
        setTimeout(function(){ console.log(JSON.stringify({html: BYID['rc-watch-menu'].innerHTML,
                                                           on: a.a['data-on']})); }, 20);
      });
    """)
    assert 'aria-checked="false" data-rc-wm-list="index"' in got["html"]
    assert "Not changed: ZZZZ is not a US listing the spine knows" in got["html"] and got["on"] == "0"


def test_a_late_lists_read_does_not_undo_a_tick_that_landed_first():
    """The load-time read answers after a tick: the ticked list keeps the tick's state for that ticker."""
    got = _node(r"""
      var a = new El({'data-rc-watch-menu': '', 'data-ticker': 'NVDA'});
      BUTTONS = [a];
      RC_WATCH.lists = [{id: 'jobs.stock', name: 'Stock picks', group: 'Read by the jobs', count: 0, has: {}}];
      ANSWERS['/api/watchlists/jobs.stock/tick/NVDA'] = {ok: true, on: true};
      var release;
      var slow = new Promise(function(res){ release = res; });
      var realFetch = fetch;
      fetch = function(url, opts){
        if(url === '/api/watchlists'){ calls.push({url: url}); return slow.then(function(){
          return {status: 200, json: function(){ return Promise.resolve(LISTS); }}; }); }
        return realFetch(url, opts);
      };
      var init = rcWatchInit('leaps');
      click(a);
      var item = new El({'data-rc-wm-list': 'jobs.stock'}); item.inMenu = true;
      click(item);
      setTimeout(function(){
        release();
        init.then(function(){ console.log(JSON.stringify({on: a.a['data-on']})); });
      }, 20);
    """)
    assert got == {"on": "2"}            # jobs.option from the read, jobs.stock from the tick
