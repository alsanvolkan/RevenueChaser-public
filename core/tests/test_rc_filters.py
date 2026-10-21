"""Shared filter bar (core/static/rc_filters.{js,css}) — plan filters S3.

The pure logic (``RCFilters.core``) runs under node without a DOM; the Python
helpers are checked for markup, escaping and ARIA wiring.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from RevenueChaser.core import reporting_common as rc


def test_the_component_ships_as_one_namespaced_module():
    js = rc.FILTERS_JS
    assert "root.RCFilters" in js and "root.RCCombobox" in js and "root.RCInfo" in js
    # No page globals from the old snippets (MC/EWEEK/INDEX/SECTOR state).
    for g in ("var MC ", "var MC=", "var EWEEK", "var INDEX ", "var SECTOR"):
        assert g not in js
    assert '"rc.filters.v1"' in js
    assert rc.FILTERS_CSS.startswith("<style>") and ".rcf-bar" in rc.FILTERS_CSS


def test_facet_order_is_index_cap_sector_theme_earnings():
    assert 'var FACETS = ["index", "mc", "gics", "theme", "earnings"]' in rc.FILTERS_JS


def test_filters_bar_html_is_the_mount_point():
    assert rc.filters_bar_html("x") == '<section class="rcf-bar" id="x"></section>'


def test_control_html_puts_the_explanation_beside_the_control_and_escapes():
    h = rc.control_html("hz", "Horizon <b>", "<div>CTRL</div>", 'Hor"izon',
                        "<b>body</b>", now_html="Now: 1d")
    assert 'aria-controls="rcb-i-hz"' in h and 'id="rcb-i-hz"' in h
    assert 'aria-expanded="false"' in h and "hidden" in h
    assert "Horizon &lt;b&gt;" in h                 # label escaped
    assert 'aria-label="Hor&quot;izon explained"' in h
    assert "<div>CTRL</div>" in h and "<b>body</b>" in h
    assert '<div class="rcf-now">Now: 1d</div>' in h


def test_combobox_html_has_the_aria_1_2_wiring():
    h = rc.combobox_html("find", "find-lbl", "Type a ticker")
    for part in ('role="combobox"', 'aria-autocomplete="list"', 'aria-expanded="false"',
                 'aria-controls="find-list"', 'role="listbox"', 'id="find-list"'):
        assert part in h


_NODE = r"""
var C = globalThis.RCFilters.core;
var now = new Date(2026, 8, 30);                    // Wed 30 Sep 2026 (ISO W40)
function ts(d){ return Math.floor(d.getTime()/1000); }
var rows = [
  {ticker:"AEM", name:"Agnico Eagle", index:["S&P 500"], market_cap:8.2e10,
   gics:["Basic Materials","Gold"], theme_ids:["r:c2","r:m3","r:f3"],
   earnings_date: ts(new Date(2026, 9, 1))},       // this week
  {ticker:"FITB", name:"Fifth Third", index:["S&P 500","Nasdaq Composite"], market_cap:2.8e10,
   gics:["Financial Services","Banks - Regional"], theme_ids:["r:c1","r:m1","r:f1"],
   earnings_date: ts(new Date(2026, 9, 20))},       // in 3 weeks
  {ticker:"TINY", name:"Tiny Co", index:[], market_cap:1.2e8,
   gics:["Unclassified",""], theme_ids:[], earnings_date:null}
];
function run(state, mode){
  var core = C.makeCore({now: ts(now), earnings_mode: mode || "filter"});
  var F = Object.assign(C.defaults(), state);
  return rows.filter(function(r){ return core.pass(r, F); }).map(function(r){ return r.ticker; });
}
var out = {
  all: run({}),
  sp: run({index:"S&P 500"}),
  micro: run({mc:{b:["micro"],min:null,max:null}}),
  large: run({mc:{b:["mid","large"],min:null,max:null}}),
  range: run({mc:{b:[],min:5e10,max:null}}),
  sector: run({gics:{key:"Financial Services",level:1}}),
  industry: run({gics:{key:"Basic Materials›Gold",level:2}}),
  unclassified: run({gics:{key:"Unclassified",level:1}}),
  theme: run({theme:{key:"r:m1",level:2}}),
  unthemed: run({theme:{key:"__none__",level:1}}),
  earnFilter: run({earn:"0"}),
  earnMarkMode: run({earn:"0"}, "mark"),
  earnMarkButFilter: run({earn:"0", earnFilter:true}, "mark"),
  wk1: C.earnKey(rows[1], now), wkU: C.earnKey(rows[2], now),
  iso_dec31: C.isoWeek(new Date(2026, 11, 31)), iso_jan1: C.isoWeek(new Date(2027, 0, 1)),
  iso_2020_dec31: C.isoWeek(new Date(2020, 11, 31)),
  gkeys: C.gicsKeys(rows[0]), gkeysUnclassified: C.gicsKeys(rows[2]),
  gkeysBare: C.gicsKeys({ticker:"BARE"}), gkeysBlankSector: C.gicsKeys({ticker:"B2", sector:""}),
  gkeysBlankPath: C.gicsKeys({ticker:"B3", gics:["",""]}),
  gkeysSectorOnly: C.gicsKeys({ticker:"S1", sector:"Energy"}),
  gkeysFromRowFields: C.gicsKeys({ticker:"S2", sector:"Energy", industry:"Oil & Gas E&P"}),
  legacy: C.sanitize({gics:{key:"Other", label:"Other", level:1}}, {themes:{}}, rows),
  legacyNested: C.sanitize({gics:{key:"Financial Services›Other", label:"Other", level:2}}, {themes:{}},
      [{ticker:"Z", gics:["Financial Services","Unclassified"]}]),
  oldGicsLevel4: C.sanitize({gics:{key:"Materials›Materials›Metals & Mining›Gold", label:"Gold", level:4}}, {themes:{}}, rows),
  cmp: ["Unclassified","Zeta","Other","Alpha","other thing","Mid"].sort(C.labelCmp),
  san: C.sanitize({index:"S&P 500", theme:{key:"gone",label:"Gone"}, gics:{key:"Nope"},
                   mc:{b:["mid","bogus"]}, earn:"W99"}, {themes:{}}, rows),
  rank: [C.rank({ticker:"AEM",name:"Agnico Eagle"},"aem"), C.rank({ticker:"AEM",name:"Agnico Eagle"},"eagle"),
         C.rank({ticker:"AEM",name:"Agnico Eagle"},"zzz")],
  free: [C.freeItem(" brk.b ", rows), C.freeItem("aem", rows), C.freeItem("APPLE INC", rows), C.freeItem("", rows)]
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def js_out(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    p = tmp_path_factory.mktemp("rcf") / "t.js"
    p.write_text(rc.FILTERS_JS + "\n" + _NODE, encoding="utf-8")
    out = subprocess.run(["node", str(p)], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_js_facets_combine_with_and(js_out):
    assert js_out["all"] == ["AEM", "FITB", "TINY"]
    assert js_out["sp"] == ["AEM", "FITB"]
    assert js_out["micro"] == ["TINY"]
    assert js_out["large"] == ["AEM", "FITB"]          # buckets are OR within the facet
    assert js_out["range"] == ["AEM"]                  # min $50B
    assert js_out["theme"] == ["FITB"]
    assert js_out["unthemed"] == ["TINY"]


def test_js_sector_filter_matches_at_either_of_the_two_levels(js_out):
    assert js_out["sector"] == ["FITB"]
    assert js_out["industry"] == ["AEM"]
    assert js_out["unclassified"] == ["TINY"]
    assert js_out["gkeys"] == ["Basic Materials", "Basic Materials›Gold"]
    assert js_out["gkeysUnclassified"] == ["Unclassified"]


def test_js_a_row_without_a_path_builds_its_keys_from_sector_and_industry(js_out):
    assert js_out["gkeysFromRowFields"] == ["Energy", "Energy›Oil & Gas E&P"]


def test_js_a_filter_saved_on_the_old_four_level_tree_is_cleared_with_a_note(js_out):
    s = js_out["oldGicsLevel4"]
    assert s["state"]["gics"] is None
    assert any("Gold" in n for n in s["notes"])


def test_the_sector_facet_is_two_levels_and_not_called_gics():
    js = rc.FILTERS_JS
    assert 'var GICS_LEVELS = ["sector", "industry"];' in js
    assert "Sector (GICS)" not in js and "industry group" not in js and "sub-industry" not in js
    assert 'trigger("gics", "Sector",' in js


def test_js_a_row_with_no_classification_reads_unclassified_never_other(js_out):
    """R3 (#86): the fallback key is the master's ``Unclassified`` (the legacy ``Other`` is gone)."""
    assert js_out["gkeysBare"] == ["Unclassified"]
    assert js_out["gkeysBlankSector"] == ["Unclassified"]
    assert js_out["gkeysBlankPath"] == ["Unclassified"]       # a row whose own path is empty
    assert js_out["gkeysSectorOnly"] == ["Energy"]


def test_js_a_saved_legacy_other_filter_is_carried_over_to_unclassified(js_out):
    """A sector filter saved before R3 (``Other``) keeps working instead of being dropped."""
    assert js_out["legacy"]["state"]["gics"]["key"] == "Unclassified"
    assert js_out["legacy"]["notes"] == []
    assert js_out["legacyNested"]["state"]["gics"]["key"] == "Financial Services›Unclassified"


def test_js_the_tree_sorts_the_unknown_bucket_and_other_last(js_out):
    assert js_out["cmp"] == ["Alpha", "Mid", "Zeta", "Other", "other thing", "Unclassified"]


def test_js_earnings_filters_on_screeners_and_marks_on_group_boards(js_out):
    assert js_out["earnFilter"] == ["AEM"]
    assert js_out["earnMarkMode"] == ["AEM", "FITB", "TINY"]     # marks, never removes
    assert js_out["earnMarkButFilter"] == ["AEM"]                # user ticked "Filter"
    assert js_out["wk1"] == "3" and js_out["wkU"] == "unknown"


def test_js_iso_week_wraps_at_the_year_end(js_out):
    assert js_out["iso_dec31"] == 53 and js_out["iso_jan1"] == 53
    assert js_out["iso_2020_dec31"] == 53
    assert max(js_out["iso_dec31"], js_out["iso_jan1"]) <= 53    # never "W54"


def test_js_sanitize_drops_state_that_does_not_exist_here(js_out):
    s = js_out["san"]
    assert s["state"]["index"] == "S&P 500"
    assert s["state"]["theme"] is None and s["state"]["gics"] is None
    assert s["state"]["mc"]["b"] == ["mid"]
    assert s["state"]["earn"] == "any"
    assert len(s["notes"]) == 2 and "no longer exists" in s["notes"][1] or \
        any("no longer exists" in n for n in s["notes"])


def test_js_combobox_ranks_ticker_before_name(js_out):
    assert js_out["rank"] == [0, 4, 9]


def test_js_combobox_offers_a_typed_ticker_outside_the_list_only_when_asked(js_out):
    """#383 C1b: Implied Probability opens a name outside the filtered pool (``allowFree``), as the old picker did."""
    assert js_out["free"][0] == {"ticker": "BRK.B", "name": "", "free": True}
    assert js_out["free"][1] is None            # already a row: the match itself is offered
    assert js_out["free"][2] is None            # not ticker-shaped
    assert js_out["free"][3] is None


def test_the_free_ticker_rule_is_the_servers_ticker_rule():
    from RevenueChaser.app import dashboard
    assert dashboard._TICKER_RE.pattern in rc.FILTERS_JS


_CLEAR_NODE = r"""
var handlers = {};
function el(id){ return {id: id, value: "MSFT — Microsoft", hidden: true, innerHTML: "", attrs: {},
  setAttribute: function(k, v){ this.attrs[k] = v; }, removeAttribute: function(k){ delete this.attrs[k]; },
  addEventListener: function(ev, fn){ (handlers[id + ":" + ev] = handlers[id + ":" + ev] || []).push(fn); },
  focus: function(){ handlers.focused = id; }, contains: function(){ return false; }}; }
var input = el("find"), list = el("find-list"), btn = el("clear-btn");
var asked = [];
globalThis.document = {addEventListener: function(){},
  querySelector: function(sel){ asked.push(sel); return sel === '[data-clear-for="find"]' ? btn : null; }};
var cleared = 0;
globalThis.RCCombobox.mount({input: input, list: list, rows: [], clear: true, onClear: function(){ cleared++; }});
(handlers["clear-btn:click"] || []).forEach(function(fn){ fn({}); });
var off = {asked: asked.length};
asked = [];
globalThis.RCCombobox.mount({input: el("other"), list: el("other-list"), rows: []});
console.log(JSON.stringify({value: input.value, cleared: cleared, focused: handlers.focused, wiredOnlyWhenAsked: asked.length === 0}));
"""


def test_js_the_combobox_wires_its_clear_button_when_asked(tmp_path):
    """#383 C1b: the boards that move to the kit get a working × (Co-movement keeps its own handler)."""
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    p = tmp_path / "clear.js"
    p.write_text(rc.FILTERS_JS + "\n" + _CLEAR_NODE, encoding="utf-8")
    out = subprocess.run(["node", str(p)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr[-1500:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got == {"value": "", "cleared": 1, "focused": "find", "wiredOnlyWhenAsked": True}


_FREE_FILTERED_NODE = r"""
var handlers = {};
function el(id){ return {id: id, value: "", hidden: true, innerHTML: "", attrs: {},
  setAttribute: function(k, v){ this.attrs[k] = v; }, removeAttribute: function(k){ delete this.attrs[k]; },
  addEventListener: function(ev, fn){ (handlers[id + ":" + ev] = handlers[id + ":" + ev] || []).push(fn); },
  querySelectorAll: function(){ return []; }, contains: function(){ return false; }}; }
var input = el("find"), list = el("find-list"), picked = [];
globalThis.document = {addEventListener: function(){}, querySelector: function(){ return null; },
  getElementById: function(){ return null; }};
var rows = [{ticker: "NVDA", name: "NVIDIA", sector: "Technology"}, {ticker: "PFE", name: "Pfizer", sector: "Healthcare"}];
globalThis.RCCombobox.mount({input: input, list: list, rows: rows, allowFree: true,
  filter: function(r){ return r.sector === "Healthcare"; }, onPick: function(r){ picked.push(r); }});
input.value = "nvda";
handlers["find:input"][0]({});
var html = list.innerHTML;
handlers["find:keydown"][0]({key: "Enter", preventDefault: function(){}});
console.log(JSON.stringify({html: html, picked: picked}));
"""


def test_js_a_free_ticker_hidden_by_the_filters_can_still_be_opened(tmp_path):
    """#383 C1b review (B3): with a filter on, a typed ticker that only a hidden row holds is still offered as
    "Open NVDA", as the old Implied Probability picker did; the hidden-match count stays."""
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    p = tmp_path / "free_filtered.js"
    p.write_text(rc.FILTERS_JS + "\n" + _FREE_FILTERED_NODE, encoding="utf-8")
    out = subprocess.run(["node", str(p)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr[-1500:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert "Open </span>" in got["html"] and ">NVDA</span>" in got["html"] and "not in the list" in got["html"]
    assert "1 more match but are hidden by your filters" in got["html"]
    assert got["picked"] == [{"ticker": "NVDA", "name": "", "free": True}]
