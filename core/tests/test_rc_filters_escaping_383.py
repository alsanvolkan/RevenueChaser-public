"""The kit's sector tree escapes a hostile GICS label (ADR-X1, #383).

The tree is built in ``rc_filters.js`` and written to ``innerHTML``, so the AG page
runs under node (``board_js``) and the tree markup is read back.
"""
from __future__ import annotations

from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_asymmetric_growth.report import board_payload, render_asymmetric_growth_hunter_html
from RevenueChaser.module_asymmetric_growth.tests.test_agh_escaping import _result

_EVIL = "<img src=x onerror=alert(1)>"

_CAPTURE = r"""
globalThis.__sectorTrees = [];
function __arm(el){
  if (!el || el.__armed) return el;
  el.__armed = true;
  var qs = el.querySelector;
  if (typeof qs !== "function") return el;
  el.querySelector = function(sel){
    var found = qs.call(this, sel);
    __arm(found);
    if (String(sel).indexOf("rcf-tree") >= 0) {
      var cur = "";
      Object.defineProperty(found, "innerHTML", {
        configurable: true,
        get: function(){ return cur; },
        set: function(v){ cur = String(v); globalThis.__sectorTrees.push(cur); }
      });
    }
    return found;
  };
  return el;
}
var __docQS = document.querySelector;
document.querySelector = function(sel){ return __arm(__docQS.call(document, sel)); };
"""


def _page(monkeypatch):
    from RevenueChaser.module_asymmetric_growth import report as ag_report

    def fields(tickers):
        out = {}
        for t in tickers or []:
            row = {"gics": [_EVIL, "Semis"], "theme_ids": []}
            out[t] = row
            if t:
                out[str(t).upper()] = row
        return out

    monkeypatch.setattr(ag_report._universe, "filter_fields", fields)
    result = _result()
    card = result["cards"][0]
    card["gics"] = [_EVIL, "Semis"]
    card["sector"] = _EVIL
    card["industry"] = "Semis"
    html = render_asymmetric_growth_hunter_html(result)
    payload = board_payload(result)                 # the rows the page loads (S7, K10)
    assert payload["cards"][0]["gics"] == [_EVIL, "Semis"]
    return "<script>\n" + _CAPTURE + "\n</script>\n" + html, payload


def test_the_sector_tree_escapes_a_hostile_gics_label(tmp_path, rawstore_tmp_db, monkeypatch):
    html, payload = _page(monkeypatch)
    tree = run_board_js(html, "console.log(JSON.stringify((globalThis.__sectorTrees || []).join('\\n')));",
                        tmp_path, payload=payload)
    assert "&lt;img" in tree
    assert "<img" not in tree
