"""The shared watch symbol and its dropdown (PR 4, D2; the named lists #774; ADR-X1): ``WATCHLIST_JS`` /
``WATCHLIST_CSS`` / ``watch_buttons_html``.

The server half is checked as markup; the client half runs under node against a small fake DOM, so the delegated
click handler is seen reading ``data-ticker`` and ``data-rc-watch-list``, posting to the per-list route and to
"+ New list", and refusing a ticker the dashboard would refuse.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from RevenueChaser.core import reporting_common as rc
from RevenueChaser.core.tests.test_one_escaper_175 import check_page
from RevenueChaser.rcdata import ops

HOSTILE = ["<script>alert(1)</script>", "\"><img src=x onerror=alert(1)>", "A'B", "A B", "", None, "ABCDEFGHIJK",
           "-AAA", "../etc", "A&B", "NVDA\n"]


def test_the_ticker_rule_is_the_dashboards():
    from RevenueChaser.app import dashboard
    assert rc.WATCH_TICKER_PATTERN == dashboard._TICKER_RE.pattern
    assert dashboard._TICKER_RE.pattern in rc.WATCHLIST_JS


def test_the_name_limit_the_input_offers_is_the_stores():
    assert f"RC_WATCH_NAME_MAX = {ops.WATCH_NAME_MAX};" in rc.WATCHLIST_JS


def test_the_groups_are_the_stores_kinds():
    assert tuple(rc.WATCH_GROUP_LABELS) == ops.WATCH_KINDS


@pytest.mark.parametrize("ticker", HOSTILE)
def test_a_ticker_the_dashboard_refuses_gets_no_symbol(ticker):
    assert rc.watch_buttons_html(ticker) == "" or ticker == "NVDA\n"


def test_a_trailing_newline_is_stripped_like_the_route_does():
    assert 'data-ticker="NVDA"' in rc.watch_buttons_html("NVDA\n")


def test_the_symbol_is_one_menu_button_with_data_ticker_and_no_inline_handler():
    out = rc.watch_buttons_html("brk.b")
    assert out.count("<button") == 1
    assert 'data-rc-watch-open="1" data-ticker="BRK.B"' in out
    assert 'aria-haspopup="true"' in out and 'aria-expanded="false"' in out
    assert 'aria-label="BRK.B watch lists"' in out and "data-rc-watch-on" not in out
    assert not re.search(r"\son\w+\s*=", out)
    for snippet in (rc.WATCHLIST_JS, rc.watchlist_html("leaps")):
        assert not re.search(r"""\son[a-z]+\s*=\s*["']""", snippet)


def test_a_board_that_knows_the_name_is_watched_renders_the_symbol_already_filled():
    assert 'data-rc-watch-on="1"' in rc.watch_buttons_html("NVDA", on=("stock",))
    assert "data-rc-watch-on" not in rc.watch_buttons_html("NVDA", on=())


def test_the_targets_are_44px_and_use_the_theme_tokens():
    css = rc.WATCHLIST_CSS
    assert "min-width:var(--tap,44px)" in css and "min-height:var(--tap,44px)" in css
    assert css.count("min-height:var(--tap,44px)") >= 3     # the symbol, every menu row, the new-name input
    for token in ("var(--card", "var(--border", "var(--fg", "var(--muted", "var(--sky", "var(--r-md",
                  "var(--e-float", "var(--focus"):
        assert token in css, token


def test_the_menu_is_a_phone_width_sheet_below_480px():
    sheet = rc.WATCHLIST_CSS[rc.WATCHLIST_CSS.index("@media (max-width:480px)"):]
    assert "position:fixed" in sheet and "max-width:none" in sheet


def test_the_mount_passes_the_board_as_json_and_defines_its_escaper():
    page = "<html><body>" + rc.watchlist_html("</script><b>x") + "</body></html>"
    assert "rcWatchInit(\"\\u003c/script\\u003e\\u003cb\\u003ex\");" in page
    check_page(page)


# A fake DOM just wide enough for the delegated handler: attributes, ``closest`` by attribute or class, and one
# menu element whose ``innerHTML`` is scanned for the rows the handler looks up by selector.
_NODE_HARNESS = r"""
var calls = [];
var listeners = [];
function Btn(attrs, cls){ this.a = attrs || {}; this.cls = cls || ''; this.disabled = false; this.value = ''; }
Btn.prototype.getAttribute = function(k){ return this.a.hasOwnProperty(k) ? this.a[k] : null; };
Btn.prototype.setAttribute = function(k, v){ this.a[k] = String(v); };
Btn.prototype.removeAttribute = function(k){ delete this.a[k]; };
Btn.prototype.focus = function(){ this.focused = true; };
Btn.prototype.getBoundingClientRect = function(){ return {top: 10, bottom: 54, left: 20, right: 64,
                                                          width: 44, height: 44}; };
Btn.prototype.closest = function(sel){
  var m = /^\[([a-z-]+)\]$/.exec(sel);
  if(m) return this.a.hasOwnProperty(m[1]) ? this : null;
  m = /^\.([a-z-]+)$/.exec(sel);
  if(m) return this.cls.split(' ').indexOf(m[1]) >= 0 ? this : null;
  return null;
};
function Menu(){ this.a = {}; this.cls = 'rc-watch-menu'; this.hidden = true; this.innerHTML = '';
                 this.style = {}; }
Menu.prototype = Object.create(Btn.prototype);
Menu.prototype.querySelector = function(sel){
  // The three selectors the handler uses, read back out of the rendered menu.
  var attr = /^\[([a-z-]+)(?:="([^"]*)")?\]$/.exec(sel);
  if(!attr) return null;
  var want = attr[1], val = attr[2];
  var re = new RegExp(want + '="([^"]*)"', 'g'), m;
  while((m = re.exec(this.innerHTML))){
    if(val === undefined || m[1] === val){
      var row = {}; row[want] = m[1];
      // The ticker of that row, when the markup carries one.
      var tail = this.innerHTML.slice(m.index);
      var t = /data-ticker="([^"]*)"/.exec(tail.slice(0, 200));
      if(t) row['data-ticker'] = t[1];
      var btn = new Btn(row);
      btn.value = MENU_INPUT;
      FOUND.push(btn);
      return btn;
    }
  }
  return null;
};
var MENU_INPUT = '';
var FOUND = [];
var MENU = new Menu();
var BUTTONS = [];
var document = {
  addEventListener: function(type, fn, capture){ listeners.push({type: type, fn: fn, capture: capture}); },
  querySelectorAll: function(sel){ return sel.indexOf('aria-expanded') >= 0
      ? BUTTONS.filter(function(b){ return b.a['aria-expanded'] === 'true'; }) : BUTTONS; },
  createElement: function(){ return MENU; },
  body: {appendChild: function(){}}
};
var window = {pageYOffset: 0, pageXOffset: 0};
var ANSWERS = {};
function fetch(url, opts){
  calls.push({url: url, opts: opts || null});
  var body = ANSWERS[url] || {ok: false, error: 'no route'};
  return Promise.resolve({status: body.ok ? 200 : 400, json: function(){ return Promise.resolve(body); }});
}
function fire(type, target, extra){
  var ev = {type: type, target: target, stopped: false, prevented: false,
            stopPropagation: function(){ this.stopped = true; }, preventDefault: function(){ this.prevented = true; }};
  for(var k in (extra || {})) ev[k] = extra[k];
  listeners.forEach(function(l){ if(l.type === type) l.fn(ev); });
  return ev;
}
function click(target){ return fire('click', target); }
var LISTS = {ok: true, option: ['NVDA'], stock: [],
             groups: {option: [{id: 3, kind: 'option', name: 'Main', tickers: ['NVDA']}],
                      stock: [{id: 7, kind: 'stock', name: 'Core', tickers: []},
                              {id: 8, kind: 'stock', name: 'Biotech', tickers: ['CRSP']}]}};
"""


def _node(script: str) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    out = subprocess.run(["node", "-e", _NODE_HARNESS + rc.WATCHLIST_JS + script], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_client_symbols_escape_and_refuse_like_the_server():
    got = _node(r"""
      var out = {};
      out.hostile = %s.map(function(t){ return rcWatchButtons(t); });
      out.good = rcWatchButtons('brk.b');
      console.log(JSON.stringify(out));
    """ % json.dumps([t for t in HOSTILE if t is not None and t != "NVDA\n"]))
    assert set(got["hostile"]) == {""}
    assert got["good"] == rc.watch_buttons_html("BRK.B")


def test_init_marks_a_symbol_from_the_union_of_its_groups_lists():
    got = _node(r"""
      var a = new Btn({'data-rc-watch-open': '1', 'data-ticker': 'NVDA'});
      var b = new Btn({'data-rc-watch-open': '1', 'data-ticker': 'CRSP'});
      var c = new Btn({'data-rc-watch-open': '1', 'data-ticker': 'MSFT'});
      BUTTONS = [a, b, c];
      ANSWERS['/api/watchlists'] = LISTS;
      rcWatchInit('sec_13f').then(function(){
        console.log(JSON.stringify({on: [!!a.a['data-rc-watch-on'], !!b.a['data-rc-watch-on'],
                                         !!c.a['data-rc-watch-on']],
                                    read: calls[0].url}));
      });
    """)
    # NVDA from the option group, CRSP from the second stock list: the union, not one named list.
    assert got["on"] == [True, True, False]
    assert got["read"] == "/api/watchlists"


def test_the_menu_lists_both_groups_with_a_checkbox_each_and_a_new_list_row():
    got = _node(r"""
      var a = new Btn({'data-rc-watch-open': '1', 'data-ticker': 'CRSP'});
      BUTTONS = [a];
      ANSWERS['/api/watchlists'] = LISTS;
      rcWatchInit('sec_13f').then(function(){
        click(a);
        console.log(JSON.stringify({html: MENU.innerHTML, hidden: MENU.hidden, top: MENU.style.top,
                                    expanded: a.a['aria-expanded']}));
      });
    """)
    html = got["html"]
    assert got["hidden"] is False and got["expanded"] == "true" and got["top"] == "58px"
    assert "<h4>Options</h4>" in html and "<h4>Stocks</h4>" in html
    assert 'data-rc-watch-list="7" data-ticker="CRSP" aria-checked="false"' in html
    assert 'data-rc-watch-list="8" data-ticker="CRSP" aria-checked="true"' in html
    assert html.count('data-rc-watch-new=') == 2 and html.count("+ New list") == 2
    assert 'role="menuitemcheckbox"' in html


def test_a_tick_posts_to_that_one_list_with_the_board_and_leaves_the_others():
    got = _node(r"""
      var a = new Btn({'data-rc-watch-open': '1', 'data-ticker': 'CRSP'});
      BUTTONS = [a];
      ANSWERS['/api/watchlists'] = LISTS;
      ANSWERS['/api/watchlists/7/toggle/CRSP'] = {ok: true, list_id: 7, kind: 'stock', list: 'Core',
                                                  ticker: 'CRSP', on: true};
      rcWatchInit('sec_13f').then(function(){
        click(a);
        var row = MENU.querySelector('[data-rc-watch-list="7"]');
        var ev = click(row);
        setTimeout(function(){
          console.log(JSON.stringify({calls: calls, stopped: ev.stopped, capture: listeners[0].capture,
                                      html: MENU.innerHTML}));
        }, 20);
      });
    """)
    post = got["calls"][1]
    assert post["url"] == "/api/watchlists/7/toggle/CRSP"
    assert post["opts"]["method"] == "POST" and post["opts"]["headers"] == {"X-RC-Panel": "sec_13f"}
    assert got["stopped"] is True and got["capture"] is True
    assert 'data-rc-watch-list="7" data-ticker="CRSP" aria-checked="true"' in got["html"]
    assert 'data-rc-watch-list="8" data-ticker="CRSP" aria-checked="true"' in got["html"]


def test_new_list_creates_it_and_puts_the_open_ticker_on_it_at_once():
    got = _node(r"""
      var a = new Btn({'data-rc-watch-open': '1', 'data-ticker': 'MSFT'});
      BUTTONS = [a];
      ANSWERS['/api/watchlists'] = LISTS;
      ANSWERS['/api/watchlists/9/toggle/MSFT'] = {ok: true, list_id: 9, kind: 'stock', list: 'Energy',
                                                  ticker: 'MSFT', on: true};
      MENU_INPUT = '  Energy  ';
      rcWatchInit('sec_13f').then(function(){
        click(a);
        var add = new Btn({'data-rc-watch-new': 'stock', 'data-ticker': 'MSFT'});
        click(add);
        var shown = MENU.innerHTML;
        ANSWERS['/api/watchlists'] = {ok: true, created: true, list: {id: 9, kind: 'stock', name: 'Energy'}};
        var make = new Btn({'data-rc-watch-create': 'stock', 'data-ticker': 'MSFT'});
        click(make);
        setTimeout(function(){
          console.log(JSON.stringify({shown: shown, calls: calls, html: MENU.innerHTML,
                                      on: !!a.a['data-rc-watch-on']}));
        }, 30);
      });
    """)
    assert 'data-rc-watch-name="stock"' in got["shown"] and 'maxlength="40"' in got["shown"]
    made, added = got["calls"][1], got["calls"][2]
    assert made["url"] == "/api/watchlists" and made["opts"]["method"] == "POST"
    assert json.loads(made["opts"]["body"]) == {"kind": "stock", "name": "Energy"}   # trimmed, as the store stores it
    assert made["opts"]["headers"]["X-RC-Panel"] == "sec_13f"
    assert added["url"] == "/api/watchlists/9/toggle/MSFT"
    assert 'data-rc-watch-list="9" data-ticker="MSFT" aria-checked="true"' in got["html"]
    assert got["on"] is True


def test_a_new_list_with_no_name_sends_nothing_and_says_why():
    got = _node(r"""
      var a = new Btn({'data-rc-watch-open': '1', 'data-ticker': 'MSFT'});
      BUTTONS = [a];
      ANSWERS['/api/watchlists'] = LISTS;
      MENU_INPUT = '   ';
      rcWatchInit('sec_13f').then(function(){
        click(a);
        click(new Btn({'data-rc-watch-new': 'stock', 'data-ticker': 'MSFT'}));
        click(new Btn({'data-rc-watch-create': 'stock', 'data-ticker': 'MSFT'}));
        setTimeout(function(){ console.log(JSON.stringify({calls: calls.length, html: MENU.innerHTML})); }, 20);
      });
    """)
    assert got["calls"] == 1                              # the load only
    assert "Not changed: a list needs a name" in got["html"]


def test_a_click_whose_data_ticker_is_hostile_or_whose_list_is_not_a_number_sends_nothing():
    got = _node(r"""
      click(new Btn({'data-rc-watch-open': '1', 'data-ticker': '"><img src=x onerror=alert(1)>'}));
      click(new Btn({'data-rc-watch-list': '7', 'data-ticker': 'A B'}));
      click(new Btn({'data-rc-watch-list': 'x; drop', 'data-ticker': 'NVDA'}));
      click(new Btn({'data-rc-watch-create': 'bond', 'data-ticker': 'NVDA'}));
      setTimeout(function(){ console.log(JSON.stringify({calls: calls, open: RC_WATCH.open})); }, 20);
    """)
    assert got["calls"] == [] and got["open"] is None


def test_a_refused_toggle_leaves_the_state_and_says_why_in_the_menu():
    got = _node(r"""
      var a = new Btn({'data-rc-watch-open': '1', 'data-ticker': 'CRSP'});
      BUTTONS = [a];
      ANSWERS['/api/watchlists'] = LISTS;
      ANSWERS['/api/watchlists/7/toggle/CRSP'] = {ok: false, error: 'panel header required'};
      rcWatchInit('sec_13f').then(function(){
        click(a);
        var row = MENU.querySelector('[data-rc-watch-list="7"]');
        click(row);
        setTimeout(function(){ console.log(JSON.stringify({html: MENU.innerHTML, disabled: row.disabled,
                                                           on: !!a.a['data-rc-watch-on']})); }, 20);
      });
    """)
    assert "Not changed: panel header required" in got["html"]
    assert got["disabled"] is False and got["on"] is True          # CRSP is still on the other stock list
    assert 'data-rc-watch-list="7" data-ticker="CRSP" aria-checked="false"' in got["html"]


def test_escape_and_a_click_outside_close_the_menu_and_the_symbol_says_so():
    got = _node(r"""
      var a = new Btn({'data-rc-watch-open': '1', 'data-ticker': 'CRSP'});
      BUTTONS = [a];
      ANSWERS['/api/watchlists'] = LISTS;
      rcWatchInit('sec_13f').then(function(){
        click(a);
        var afterSecond = (click(a), MENU.hidden);                 // the same symbol again closes it
        click(a);
        fire('keydown', MENU, {key: 'Escape'});
        var afterEsc = [MENU.hidden, a.a['aria-expanded']];
        click(a);
        click(new Btn({'data-other': '1'}));
        console.log(JSON.stringify({afterSecond: afterSecond, afterEsc: afterEsc,
                                    afterOutside: [MENU.hidden, RC_WATCH.open]}));
      });
    """)
    assert got["afterSecond"] is True
    assert got["afterEsc"] == [True, "false"]
    assert got["afterOutside"] == [True, None]


def test_a_late_lists_read_does_not_undo_a_tick_that_landed_first():
    """Review nit 5 of PR 4, per list now: the load-time read answers after a tick, and the tick's state wins."""
    got = _node(r"""
      var a = new Btn({'data-rc-watch-open': '1', 'data-ticker': 'CRSP'});
      BUTTONS = [a];
      var release;
      var slow = new Promise(function(res){ release = res; });
      ANSWERS['/api/watchlists/7/toggle/CRSP'] = {ok: true, list_id: 7, ticker: 'CRSP', on: true};
      var realFetch = fetch;
      fetch = function(url, opts){
        if(url === '/api/watchlists'){ calls.push({url: url}); return slow.then(function(){
          return {status: 200, json: function(){ return Promise.resolve(LISTS); }}; }); }
        return realFetch(url, opts);
      };
      var init = rcWatchInit('leaps');
      click(new Btn({'data-rc-watch-list': '7', 'data-ticker': 'CRSP'}));
      setTimeout(function(){
        release();
        init.then(function(){
          console.log(JSON.stringify({core: !!RC_WATCH.lists[7]['CRSP'], bio: !!RC_WATCH.lists[8]['CRSP'],
                                      on: !!a.a['data-rc-watch-on']}));
        });
      }, 20);
    """)
    assert got == {"core": True, "bio": True, "on": True}
