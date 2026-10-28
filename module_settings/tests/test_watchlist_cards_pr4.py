"""PR 4 step 5: the two watch-list cards of Settings / "New data system" (D2, F7 A).

Each card lists its names (ticker, added on, by, note) with a Remove that posts the remove route, never the toggle;
the counts are in the section's summary line too, and the option card says which names wait for the next options
build. Stacked rows (no table), 44 px buttons, every string escaped, handlers read ``data-list`` / ``data-ticker``.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from RevenueChaser.core.tests.board_fixtures import hostile, newdata_fixture
from RevenueChaser.core.tests.test_board_html_guard import assert_inert
from RevenueChaser.core.tests.test_one_escaper_175 import check_page
from RevenueChaser.module_settings import newdata, report
from RevenueChaser.rcdata import ops


def _page(nd) -> str:
    return report.render_settings_dashboard({"newdata": nd})


def _card(html: str, kind: str) -> str:
    start = html.index(f'id="nd-watch-{kind}"')
    return html[start:html.index('data-nd-msg></div></div>', start)]


def test_each_card_lists_its_names_with_added_on_by_and_a_remove():
    html = _page(newdata_fixture())
    opt, stk = _card(html, "option"), _card(html, "stock")
    assert "<b>NVDA</b>" in opt and "added 2026-10-28T10:00:00+00:00 by leaps" in opt and " · note" in opt
    assert ('data-nd-act="watch-remove" data-list="option" data-ticker="NVDA"' in opt
            and 'aria-label="Remove NVDA from the option watch list"' in opt)
    assert "<b>MSFT</b>" in stk and "by sec_13f" in stk and 'data-list="stock" data-ticker="MSFT"' in stk
    assert "<table" not in opt + stk


def test_the_counts_are_in_the_summary_and_on_each_card():
    html = _page(newdata_fixture())
    summary = re.search(r'<div class="nd-sub" id="nd-watch-summary">(.*?)</div>', html).group(1)
    assert summary == ('Watch lists: option <span data-nd-watch-count="option">1</span> · '
                       'stock <span data-nd-watch-count="stock">1</span>')
    assert '<span class="nd-chip" data-nd-watch-count="option">1</span>' in html


def test_the_option_card_names_what_the_next_options_build_takes():
    opt = _card(_page(newdata_fixture()), "option")
    assert "Names queued for the next options build" in opt
    assert "SPX and 1 name from this list: NVDA" in opt and "step 11" in opt
    assert "options build" not in _card(_page(newdata_fixture()), "stock").split("nd-sub")[0]


def test_empty_lists_say_so_and_queue_only_spx():
    nd = {**newdata_fixture(), "watchlists": {"option": {"count": 0, "rows": []}, "stock": {"count": 0, "rows": []}},
          "options_build": {"always": ["SPX"], "queued": []}}
    html = _page(nd)
    assert _card(html, "option").count("No names yet.") == 1 and "SPX and 0 names from this list ·" in html
    assert "No names yet." in _card(html, "stock")


def test_hostile_rows_are_escaped_and_the_page_keeps_one_escaper():
    html = _page(hostile(newdata_fixture()))
    assert_inert(html)
    check_page(html)
    sec = html[html.index('<section id="newdata"'):]
    for b in re.findall(r'<button[^>]*data-nd-act="watch-remove"[^>]*>', sec):
        assert "<img" not in b and "onclick" not in b.lower()


def test_collect_reads_both_lists_from_ops_and_the_card_shows_them():
    ops.watch_add("option", "nvda", by="leaps")
    ops.watch_add("stock", "msft", by="sec_13f", note="13F breadth up")
    nd = newdata.collect(datetime(2026, 10, 28, 15, tzinfo=timezone.utc))
    html = _page(nd)
    assert "SPX and 1 name from this list: NVDA" in html
    assert "13F breadth up" in _card(html, "stock") and "by sec_13f" in _card(html, "stock")


def test_collect_on_no_ops_file_shows_empty_cards_and_does_not_create_it():
    nd = newdata.collect(datetime(2026, 10, 28, 15, tzinfo=timezone.utc))
    assert nd["watchlists"] == {"option": {"count": 0, "rows": []}, "stock": {"count": 0, "rows": []}}
    assert not ops.ops_path().exists()


def test_the_cards_css_is_phone_friendly():
    css = report.ND_CSS
    assert "#newdata .nd-watch-item{display:flex;flex-wrap:wrap" in css
    assert "min-height:44px" in css and "nowrap" not in css


_STUB = r"""
var calls = [];
function El(attrs, parent){ this.a = attrs || {}; this.parentNode = parent || null; this.disabled = false;
  this.innerHTML = ''; this.textContent = ''; this.kids = []; }
El.prototype.getAttribute = function(k){ return this.a.hasOwnProperty(k) ? this.a[k] : null; };
El.prototype.closest = function(sel){ var e = this; while(e){ if(e.match && e.match(sel)) return e; e = e.parentNode; }
  return null; };
El.prototype.removeChild = function(k){ this.kids = this.kids.filter(function(x){ return x !== k; }); k.parentNode = null; };
var counts = [new El(), new El()];
var ul = new El();
var card = new El({}, null);
card.match = function(s){ return s === '.nd-watch'; };
var queued = new El();
card.querySelector = function(sel){ return sel.indexOf('queued') >= 0 ? queued : msg; };
card.querySelectorAll = function(){ return ul.kids; };
var msg = new El();
ul.parentNode = card;
var li = new El({'data-ticker': 'NVDA'}, ul); li.match = function(s){ return s === 'li'; }; ul.kids.push(li);
var li2 = new El({'data-ticker': 'AMD'}, ul); li2.match = function(s){ return s === 'li'; }; ul.kids.push(li2);
var btn = new El({'data-list': 'option', 'data-ticker': 'NVDA'}, li);
var bad = new El({'data-list': 'option', 'data-ticker': '"><img src=x>'}, li2);
var document = {querySelectorAll: function(){ return counts; }, addEventListener: function(){},
  getElementById: function(){ return null; }};
var ND = %s;
function fetch(url, opts){ calls.push({url: url, opts: opts});
  return Promise.resolve({status: 200, json: function(){ return Promise.resolve({ok: true, removed: true}); }}); }
"""


def test_remove_posts_the_remove_route_with_the_settings_header_and_drops_the_row():
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    from RevenueChaser.core.html_safe import ESC_JS
    from RevenueChaser.core.tests.board_js import function_source
    js = (_STUB % json.dumps(report._nd_client(newdata_fixture())) + ESC_JS
          + function_source(report.ND_JS, "ndWatchRemove") + r"""
      ndWatchRemove(bad);
      ndWatchRemove(btn);
      setTimeout(function(){ console.log(JSON.stringify({calls: calls, left: ul.kids.length,
        counts: counts.map(function(c){ return c.textContent; }), msg: msg.innerHTML,
        queued: queued.textContent})); }, 20);
    """)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert [c["url"] for c in got["calls"]] == ["/api/watchlist/option/remove/NVDA"]     # the hostile one sent nothing
    assert got["calls"][0]["opts"] == {"method": "POST", "headers": {"X-RC-Panel": "settings"}}
    assert got["left"] == 1 and got["counts"] == ["1", "1"] and got["msg"] == "NVDA removed."
    # review nit 6: the queued line follows the list, in the words the server renders
    assert got["queued"] == "SPX and 1 name from this list: AMD · the options build comes later (build-plan step 11)"


def test_the_queued_line_is_one_span_the_remove_can_rewrite():
    opt = _card(_page(newdata_fixture()), "option")
    assert ('<span data-nd-queued>SPX and 1 name from this list: NVDA · the options build comes later '
            '(build-plan step 11)</span>') in opt
