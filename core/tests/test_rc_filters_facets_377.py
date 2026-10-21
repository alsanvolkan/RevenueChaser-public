"""A facet a board omits never filters it (KL review, #377 Card E).

LEAPS is the first board that mounts the kit with ``meta.facets`` (no Earnings). The state is shared across boards
(``rc.filters.v1``), so an Earnings week picked on Co-movement used to reach LEAPS and hide scan rows by a control
the LEAPS bar does not show. The omitted facet's state is kept for the other boards; here it neither filters nor
counts as an active filter.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from RevenueChaser.core import reporting_common as rc

_NODE = r"""
var C = globalThis.RCFilters.core;
var now = new Date(2026, 9, 14);
function ts(d){ return Math.floor(d.getTime()/1000); }
var rows = [
  {ticker:"A", index:["S&P 500"], market_cap:5e10, gics:["Technology","Software"], theme_ids:[],
   earnings_date: ts(new Date(2026, 9, 15))},     // this week
  {ticker:"B", index:[], market_cap:null, gics:["Energy",""], theme_ids:[], earnings_date:null}
];
function run(meta, state){
  var core = C.makeCore(Object.assign({now: ts(now)}, meta));
  var F = Object.assign(C.defaults(), state);
  return {pass: rows.filter(function(r){ return core.pass(r, F); }).map(function(r){ return r.ticker; }),
          active: !!core.active(F)};
}
var four = {facets: ["index","mc","gics","theme"]};
console.log(JSON.stringify({
  earnOmitted: run(four, {earn:"0"}),
  earnOmittedPlusSector: run(four, {earn:"0", gics:{key:"Energy", level:1}}),
  earnAll: run({}, {earn:"0"}),
  indexOmitted: run({facets:["gics"]}, {index:"S&P 500"}),
  indexKept: run(four, {index:"S&P 500"})
}));
"""


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    p = tmp_path_factory.mktemp("rcf377") / "t.js"
    p.write_text(rc.FILTERS_JS + "\n" + _NODE, encoding="utf-8")
    res = subprocess.run(["node", str(p)], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_an_omitted_earnings_facet_neither_filters_nor_counts_as_active(out):
    assert out["earnOmitted"] == {"pass": ["A", "B"], "active": False}


def test_the_shown_facets_still_filter_beside_an_omitted_one(out):
    assert out["earnOmittedPlusSector"] == {"pass": ["B"], "active": True}
    assert out["indexKept"] == {"pass": ["A"], "active": True}


def test_any_omitted_facet_is_ignored_not_only_earnings(out):
    assert out["indexOmitted"] == {"pass": ["A", "B"], "active": False}


def test_a_board_without_meta_facets_keeps_every_facet(out):
    assert out["earnAll"] == {"pass": ["A"], "active": True}
