"""The shared watch-list button pair (PR 4, D2; ADR-X1): ``WATCHLIST_JS`` / ``WATCHLIST_CSS`` / ``watch_buttons_html``.

The server half is checked as markup; the client half runs under node against a small fake DOM, so the click handler
is seen reading ``data-ticker`` and posting to the toggle route, and refusing a ticker the dashboard would refuse.
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


def test_the_ticker_rule_is_the_dashboards():
    from RevenueChaser.app import dashboard
    assert rc.WATCH_TICKER_PATTERN == dashboard._TICKER_RE.pattern
    assert dashboard._TICKER_RE.pattern in rc.WATCHLIST_JS


@pytest.mark.parametrize("ticker", HOSTILE)
def test_a_ticker_the_dashboard_refuses_gets_no_buttons(ticker):
    assert rc.watch_buttons_html(ticker) == "" or ticker == "NVDA\n"


def test_a_trailing_newline_is_stripped_like_the_route_does():
    assert 'data-ticker="NVDA"' in rc.watch_buttons_html("NVDA\n")


def test_the_pair_is_two_labelled_toggles_with_data_ticker_and_no_inline_handler():
    out = rc.watch_buttons_html("brk.b")
    assert out.count("<button") == 2
    assert 'data-rc-watch="option" data-ticker="BRK.B"' in out and 'data-rc-watch="stock" data-ticker="BRK.B"' in out
    assert out.count('aria-pressed="false"') == 2 and 'aria-label="BRK.B on the option watch list"' in out
    assert not re.search(r"\son\w+\s*=", out)
    for snippet in (rc.WATCHLIST_JS, rc.watchlist_html("leaps")):
        assert not re.search(r"""\son[a-z]+\s*=\s*["']""", snippet)


def test_the_targets_are_44px_and_use_the_theme_tokens():
    css = rc.WATCHLIST_CSS
    assert "min-width:44px" in css and "min-height:44px" in css
    assert "var(--input-bg" in css and "var(--sky" in css and "var(--primary-fg" in css


def test_the_mount_passes_the_board_as_json_and_defines_its_escaper():
    page = "<html><body>" + rc.watchlist_html("</script><b>x") + "</body></html>"
    assert "rcWatchInit(\"\\u003c/script\\u003e\\u003cb\\u003ex\");" in page
    check_page(page)


_NODE_HARNESS = r"""
var calls = [];
var listeners = [];
function Btn(attrs){ this.a = attrs; this.disabled = false; }
Btn.prototype.getAttribute = function(k){ return this.a.hasOwnProperty(k) ? this.a[k] : null; };
Btn.prototype.setAttribute = function(k, v){ this.a[k] = String(v); };
Btn.prototype.removeAttribute = function(k){ delete this.a[k]; };
Btn.prototype.closest = function(){ return this; };
var BUTTONS = [];
var document = {
  addEventListener: function(type, fn, capture){ listeners.push({type: type, fn: fn, capture: capture}); },
  querySelectorAll: function(){ return BUTTONS; }
};
var ANSWERS = {};
function fetch(url, opts){
  calls.push({url: url, opts: opts || null});
  var body = ANSWERS[url] || {ok: false, error: 'no route'};
  return Promise.resolve({status: body.ok ? 200 : 400, json: function(){ return Promise.resolve(body); }});
}
function click(btn){
  var ev = {target: btn, stopped: false, prevented: false,
            stopPropagation: function(){ this.stopped = true; }, preventDefault: function(){ this.prevented = true; }};
  listeners.forEach(function(l){ if(l.type === 'click') l.fn(ev); });
  return ev;
}
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


def test_init_reads_the_lists_and_marks_every_pair_then_a_click_posts_with_the_board():
    got = _node(r"""
      var a = new Btn({'data-rc-watch': 'option', 'data-ticker': 'NVDA', 'aria-pressed': 'false'});
      var b = new Btn({'data-rc-watch': 'stock', 'data-ticker': 'NVDA', 'aria-pressed': 'false'});
      var c = new Btn({'data-rc-watch': 'option', 'data-ticker': 'MSFT', 'aria-pressed': 'false'});
      BUTTONS = [a, b, c];
      ANSWERS['/api/watchlists'] = {ok: true, option: ['NVDA'], stock: []};
      ANSWERS['/api/watchlist/stock/toggle/NVDA'] = {ok: true, list: 'stock', ticker: 'NVDA', on: true};
      rcWatchInit('sec_13f').then(function(){
        var after_init = [a.a['aria-pressed'], b.a['aria-pressed'], c.a['aria-pressed']];
        var ev = click(b);
        setTimeout(function(){
          console.log(JSON.stringify({after_init: after_init, after_click: b.a['aria-pressed'],
            stopped: ev.stopped, capture: listeners[0].capture, calls: calls, rendered: rcWatchButtons('NVDA')}));
        }, 20);
      });
    """)
    assert got["after_init"] == ["true", "false", "false"]
    assert got["after_click"] == "true" and got["stopped"] is True and got["capture"] is True
    post = got["calls"][1]
    assert post["url"] == "/api/watchlist/stock/toggle/NVDA"
    assert post["opts"]["method"] == "POST" and post["opts"]["headers"] == {"X-RC-Panel": "sec_13f"}
    assert got["rendered"].count('aria-pressed="true"') == 2


def test_a_click_on_a_button_whose_data_ticker_is_hostile_sends_nothing():
    got = _node(r"""
      var bad = new Btn({'data-rc-watch': 'option', 'data-ticker': '"><img src=x onerror=alert(1)>'});
      var odd = new Btn({'data-rc-watch': 'bond', 'data-ticker': 'NVDA'});
      click(bad); click(odd);
      setTimeout(function(){ console.log(JSON.stringify({calls: calls})); }, 20);
    """)
    assert got["calls"] == []


def test_a_refused_toggle_leaves_the_state_and_says_why():
    got = _node(r"""
      var b = new Btn({'data-rc-watch': 'option', 'data-ticker': 'NVDA', 'aria-pressed': 'false'});
      BUTTONS = [b];
      ANSWERS['/api/watchlist/option/toggle/NVDA'] = {ok: false, error: 'panel header required'};
      click(b);
      setTimeout(function(){ console.log(JSON.stringify({p: b.a['aria-pressed'], t: b.a['title'],
        e: b.a['data-rc-watch-error'], d: b.disabled})); }, 20);
    """)
    assert got == {"p": "false", "t": "Not changed: panel header required", "e": "1", "d": False}
