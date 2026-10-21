"""The dashboard's tab badge for a job (#243): ``renderStatus()`` in ``templates/dashboard.html``, run under node.

A string search on the template cannot see a script that throws. This runs the real function against stub nav items
and a stubbed ``/api/jobs`` answer, then reads each badge back: its text, and whether a click can cancel.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "dashboard.html"

_HARNESS = r"""
const src = %(src)s;
function navItem(tab){
  const kids = [];
  return {tab: tab, kids: kids, getAttribute: function(){ return tab; },
    querySelector: function(){ return kids.find(function(k){ return k.className === 'job-badge'; }) || null; },
    appendChild: function(k){ k.remove = function(){ kids.splice(kids.indexOf(k), 1); }; kids.push(k); }};
}
const items = %(tabs)s.map(navItem);
const document = {querySelectorAll: function(){ return items; },
                  createElement: function(){ return {className: '', textContent: '', title: '', onclick: null}; }};
const cancelled = [];
const fetch = function(){ return Promise.resolve({json: function(){ return {jobs: %(jobs)s}; }}); };
const renderStatus = new Function('fetch', 'document', 'confirm', 'cancelJob',
  src.replace("fetch('/api/jobs').then", "return fetch('/api/jobs').then") + '\nreturn renderStatus;')(
    fetch, document, function(){ return true; }, function(id){ cancelled.push(id); });
renderStatus().then(function(){
  const out = {};
  items.forEach(function(i){
    const b = i.kids[0];
    if(!b){ out[i.tab] = null; return; }
    if(b.onclick){ b.onclick({stopPropagation: function(){}}); }
    out[i.tab] = {text: b.textContent, clickable: !!b.onclick};
  });
  console.log(JSON.stringify({badges: out, cancelled: cancelled}));
}, function(e){ console.error(String(e && e.stack || e)); process.exit(3); });
"""

_TABS = ["module_swing_potential", "module_sec_13f", "module_value_chain", "module_fundamentals", "module_leaps"]


def _render(jobs: list[dict], tmp_path) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    html = _TEMPLATE.read_text(encoding="utf-8")
    start = html.index("function renderStatus(){")
    end = html.index("\nfunction fmtStamp(")
    path = tmp_path / "badge.js"
    path.write_text(_HARNESS % {"src": json.dumps(html[start:end]), "tabs": json.dumps(_TABS),
                                "jobs": json.dumps(jobs)}, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _job(job_id, tab, status, cancellable=False, cancel_requested=False):
    return {"id": job_id, "status": status, "cancellable": cancellable, "cancel_requested": cancel_requested,
            "produces": [tab]}


def test_each_badge_says_what_the_job_is_doing_and_offers_cancel_only_where_it_works(tmp_path):
    got = _render([
        _job("swing", "module_swing_potential", "running", cancellable=True),
        _job("13f", "module_sec_13f", "running"),
        _job("vc", "module_value_chain", "queued"),
        _job("fund", "module_fundamentals", "running", cancellable=True, cancel_requested=True),
    ], tmp_path)
    assert got["badges"] == {
        "module_swing_potential": {"text": "running… ✕", "clickable": True},
        "module_sec_13f": {"text": "running…", "clickable": False},
        "module_value_chain": {"text": "queued ✕", "clickable": True},
        "module_fundamentals": {"text": "stopping…", "clickable": False},
        "module_leaps": None,
    }
    assert sorted(got["cancelled"]) == ["swing", "vc"]


def test_no_active_job_shows_no_badge(tmp_path):
    got = _render([_job("done", "module_swing_potential", "done")], tmp_path)
    assert set(got["badges"].values()) == {None}
