"""The Runs page's own JS, and the job overlay's, run under node (#26 PL5-b).

``test_runs_tab.py`` reads the source and the payload; this runs the functions. Three of the review's
findings were that a string assertion cannot see what the page does with a row: the trigger cell could grow
an ``|| (r.lane ? "dashboard" : "")`` fallback, the clip could stop clipping, and the overlay sentence — the
half of this slice the user sees — was only grep-asserted. So the functions are taken out of their IIFE
(``board_js.function_source``) and called against the shared DOM stub, with ``document.createElement``
recording its children so the cells can be read back.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from RevenueChaser.app import dashboard, runview
from RevenueChaser.core.tests.board_js import DOM_STUB, function_source, inline_scripts
from RevenueChaser.core.tests.open_issues_shell import strip_open_issues_data

# createElement returns elements that remember what was appended to them, which the stub's no-op does not.
RECORDING_DOM = r"""
document.createElement = function(tag){
  var el = _el("");
  el.tag = tag;
  el.kids = [];
  el.appendChild = function(c){ el.kids.push(c); return c; };
  el.classList = {add: function(c){ el.className = (el.className ? el.className + " " : "") + c; },
                  remove: function(){}, toggle: function(){}, contains: function(){ return false; }};
  return el;
};
function _cells(tr){
  return tr.kids.map(function(td){
    var inner = td.kids && td.kids.length ? td.kids[0] : null;
    return {cls: td.className, text: inner ? inner.textContent : td.textContent,
            title: td.title || "", pill: inner ? inner.className : ""};
  });
}
"""

ROW = {"run_id": "r1", "module": "rotation", "source": None, "trigger": None, "status": "done",
       "queued_at": 1_792_700_000.0, "started_at": 1_792_700_001.0, "finished_at": 1_792_700_061.0,
       "duration_s": 60.0, "error": None, "message": None, "params": {}}


def _node(script: str, tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    path = tmp_path / "page.js"
    path.write_text(script, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _page_js(client) -> str:
    html = client.get("/view/runs").get_data(as_text=True)
    return next(s for s in inline_scripts(html) if "function line" in s)


def _default_limit(script: str) -> str:
    """The page's own ``var DEFAULT_LIMIT`` line, as rendered from ``runview``."""
    return next(ln for ln in script.splitlines() if ln.strip().startswith("var DEFAULT_LIMIT"))


def _row_cells(client, tmp_path, row: dict) -> list[dict]:
    """The cells ``line(row)`` builds, as ``{cls, text, title, pill}`` in column order."""
    script = _page_js(client)
    funcs = "\n".join(function_source(script, name) for name in ("stamp", "took", "text", "cell", "line"))
    clip = next(ln for ln in script.splitlines() if ln.strip().startswith("var CLIP"))
    tail = f"console.log(JSON.stringify(_cells(line({json.dumps(row)}))));"
    return _node(DOM_STUB + RECORDING_DOM + clip + "\n" + funcs + "\n" + tail, tmp_path)


@pytest.fixture()
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


# ── the row ───────────────────────────────────────────────────────────────────

def test_the_row_shows_the_stored_values_in_column_order(client, tmp_path):
    row = dict(ROW, module="RC-Daily-Bars", trigger="scheduled", status="done", duration_s=2071.5)
    cells = _row_cells(client, tmp_path, row)
    # The stub's classList.add appends, so an empty cell reads "note-cell dash": compare the first class.
    assert [c["cls"].split(" ")[0] for c in cells] == ["when", "job", "trigger", "", "took",
                                                       "note-cell", "params"]
    assert cells[1]["text"] == "RC-Daily-Bars"
    assert cells[2]["text"] == "scheduled"
    assert cells[3]["text"] == "done" and cells[3]["pill"] == "pill s-done"
    assert cells[4]["text"] == "34m 32s"


def test_a_row_that_recorded_no_trigger_shows_a_dash_and_not_a_guess(client, tmp_path):
    """The reader keeps the column empty (test_runs_tab.py); the page must not fill it in either — a
    `r.trigger || (r.lane ? "dashboard" : "")` in line() passes every source-level assertion.

    The row carries the fields a guess would reach for (``lane``, ``pid``) even though the reader no longer
    ships them, so this fails on the inference itself and not merely because the payload got smaller.
    """
    cells = _row_cells(client, tmp_path, dict(ROW, trigger=None, source=None, lane="short", pid=4242))
    trigger = cells[2]
    assert trigger["text"] == "—", f"the trigger cell invented {trigger['text']!r}"
    assert "dash" in trigger["cls"]


def test_a_running_row_says_going_rather_than_a_duration(client, tmp_path):
    cells = _row_cells(client, tmp_path, dict(ROW, status="running", finished_at=None, duration_s=None))
    assert cells[4]["text"] == "going"
    assert cells[3]["pill"] == "pill s-running"


def test_a_long_value_is_clipped_in_the_cell_and_whole_in_the_tooltip(client, tmp_path):
    args = ["-m", "RevenueChaser.scripts.nightly_inputs", "earnings", "--max-uw-calls", "200",
            "--max-fred-calls", "10", "--max-yf-calls", "2000"]
    cells = _row_cells(client, tmp_path, dict(ROW, params={"args": args}))
    params = cells[6]
    assert params["text"].endswith("…") and len(params["text"]) == 65
    assert params["title"] == json.dumps({"args": args}, separators=(",", ":")).replace('","', '", "') \
        or params["title"].startswith('{"args"'), "the tooltip carries the whole value"
    assert len(params["title"]) > len(params["text"])


def test_an_error_is_shown_in_the_note_column(client, tmp_path):
    cells = _row_cells(client, tmp_path, dict(ROW, status="error", error="exit 1", duration_s=285.9))
    assert cells[5]["text"] == "exit 1"
    assert cells[3]["pill"] == "pill s-error"


# ── the meta line ─────────────────────────────────────────────────────────────

def _meta(client, tmp_path, payload: dict, paused: bool = False) -> str:
    script = _page_js(client)
    tail = (f"console.log(JSON.stringify(metaText({json.dumps(payload)}, "
            f"{json.dumps(paused)})));")
    return _node(DOM_STUB + function_source(script, "metaText") + "\n" + tail, tmp_path)


def test_the_meta_line_counts_the_matching_runs_not_the_page(client, tmp_path):
    """SHOULD 3's other half: with the filter in SQL, "0 of 0" is about the table, and a capped read says so."""
    assert _meta(client, tmp_path, {"shown": 37, "total": 37, "problems": True, "query": ""}) \
        == "37 of 37 problem runs"
    # shown < total can only mean the read hit its limit, so the line says which rows were looked at.
    assert _meta(client, tmp_path, {"shown": 12, "total": 37, "problems": True, "query": ""}) \
        == "12 of 37 problem runs · the newest 12 are read"
    assert _meta(client, tmp_path, {"shown": 200, "total": 5000, "problems": False, "query": ""}) \
        == "200 of 5000 recorded runs · the newest 200 are read"
    assert _meta(client, tmp_path, {"shown": 0, "total": 0, "problems": True, "query": ""}) \
        == "0 of 0 problem runs"
    assert _meta(client, tmp_path, {"shown": 3, "total": 3, "problems": False, "query": "daily"}) \
        == '3 of 3 recorded runs matching "daily"'
    assert _meta(client, tmp_path, {"shown": 1, "total": 1, "problems": False, "query": ""}, paused=True) \
        == "1 of 1 recorded runs · paused"


def test_the_page_asks_the_server_for_the_filter(client, tmp_path):
    """The controls build the request, so the rows and the count come from the same WHERE."""
    script = _page_js(client)
    tail = ("""
      var out = [];
      document.getElementById("runs-problems").checked = false;
      document.getElementById("runs-filter").value = "";
      out.push(query());
      document.getElementById("runs-problems").checked = true;
      out.push(query());
      document.getElementById("runs-filter").value = "  daily  ";
      out.push(query());
      console.log(JSON.stringify(out));
    """)
    urls = _node(DOM_STUB + "globalThis.URLSearchParams = require('url').URLSearchParams;\n"
                 + 'var POLL = "/api/runs";\n'
                 + "var problems = document.getElementById('runs-problems');"
                 + "var filter = document.getElementById('runs-filter');"
                 + "var limit = document.getElementById('runs-limit');\n"
                 + _default_limit(script) + "\n" + function_source(script, "query") + "\n" + tail, tmp_path)
    assert urls == ["/api/runs", "/api/runs?problems=1", "/api/runs?problems=1&q=daily"]
    assert runview.POLL_PATH == "/api/runs"


def test_the_page_can_ask_for_more_than_the_newest_page(client, tmp_path):
    """#131 item 2: the server takes ``limit`` up to MAX_LIMIT; the page's control is what sends it."""
    script = _page_js(client)
    tail = (f"""
      var out = [];
      limit.value = "{runview.DEFAULT_LIMIT}"; out.push(query());
      limit.value = "{runview.MAX_LIMIT}"; out.push(query());
      problems.checked = true; out.push(query());
      console.log(JSON.stringify(out));
    """)
    urls = _node(DOM_STUB + "globalThis.URLSearchParams = require('url').URLSearchParams;\n"
                 + 'var POLL = "/api/runs";\n'
                 + "var problems = document.getElementById('runs-problems');"
                 + "var filter = document.getElementById('runs-filter');"
                 + "var limit = document.getElementById('runs-limit');\n"
                 + _default_limit(script) + "\n" + function_source(script, "query") + "\n" + tail, tmp_path)
    # The default page is the URL it always was, so a poll on the default view did not change.
    assert urls == ["/api/runs", f"/api/runs?limit={runview.MAX_LIMIT}",
                    f"/api/runs?problems=1&limit={runview.MAX_LIMIT}"]


# ── the overlay, the half of this slice the user sees ─────────────────────────

def _overlay(client, tmp_path, jobs: list[dict]) -> list[str]:
    html = strip_open_issues_data(client.get("/").get_data(as_text=True))
    script = next(s for s in inline_scripts(html) if "function overlayText" in s)
    # A fixed clock, so the elapsed figures are the test's and not the day's.
    prelude = ('var _now = Date.parse("2026-10-23T10:00:00+00:00");'
               "Date.now = function(){ return _now; };\n")
    tail = (f"console.log(JSON.stringify({json.dumps(jobs)}.map(function(j)"
            "{ return overlayText(j === null ? [] : [j]); })));")
    return _node(DOM_STUB + prelude + function_source(script, "overlayText") + "\n" + tail, tmp_path)


def test_the_overlay_sentence_for_every_state(client, tmp_path):
    jobs = [
        None,                                                                            # nothing running
        {"status": "queued", "module": "rotation"},
        {"status": "running", "module": "rotation", "phase": "rotation",
         "started_at": "2026-10-23T09:59:30+00:00"},                                     # phase == module
        {"status": "running", "module": "universe_run", "phase": "prices",
         "started_at": "2026-10-23T09:00:00+00:00"},                                     # phase != module
        {"status": "running", "module": "module_leaps", "phase": None,
         "started_at": "2026-10-23T09:54:40+00:00"},                                     # elapsed >= 90 s
        {"status": "running", "module": "spxgamma", "phase": None,
         "started_at": "2026-10-23T09:59:58+00:00", "cancel_requested": True},
        {"status": "running", "module": "daily_bars", "phase": None, "started_at": None},  # no start stamp
    ]
    assert _overlay(client, tmp_path, jobs) == [
        "Running… this may take a few minutes",
        "Queued behind another job…",
        "Running rotation · 30s",
        "Running universe_run · prices · 60m 0s",
        "Running module_leaps · 5m 20s",
        "Running spxgamma · 2s · stopping at the next safe point",
        "Running daily_bars",
    ]


# ── a filter change while a poll is in flight (#131 item 1) ───────────────────

def _load_harness(client, tmp_path, steps: str) -> list:
    """Drive ``load()`` against a fetch whose answers the test releases one at a time."""
    script = _page_js(client)
    # The page's state line (``var paused = ..., busy = ...``) is taken as it stands, so a new flag the
    # function needs is declared where the page declares it and not only here.
    state = next(ln for ln in script.splitlines() if ln.strip().startswith("var paused"))
    prelude = ("globalThis.URLSearchParams = require('url').URLSearchParams;\n"
               'var POLL = "/api/runs";\n'
               "var problems = document.getElementById('runs-problems');\n"
               "var filter = document.getElementById('runs-filter');\n"
               "var limit = document.getElementById('runs-limit');\n"
               "var meta = document.getElementById('runs-meta');\n"
               "var sent = [], held = [];\n"
               "fetch = function(url){ sent.push(url); return new Promise(function(res){ held.push(res); }); };\n"
               "function answer(){ if (held.length) held.shift()({ok: true, json: function(){ return {rows: []}; }}); }\n"
               "function draw(){}\n"
               "function tick(){ return new Promise(function(r){ setTimeout(r, 0); }); }\n")
    funcs = _default_limit(script) + "\n" + "\n".join(function_source(script, name) for name in ("query", "load"))
    body = f"(async function(){{ {steps} console.log(JSON.stringify(sent)); }})();"
    return _node(DOM_STUB + prelude + state + "\n" + funcs + "\n" + body, tmp_path)


def test_a_filter_change_during_a_poll_is_sent_when_the_poll_ends(client, tmp_path):
    """``if (busy) return;`` dropped the click: the table sat unfiltered until the next 5 s poll."""
    sent = _load_harness(client, tmp_path, """
      load(true);                                   // the timer's poll is in flight
      problems.checked = true; load();              // the reader ticks Problems only meanwhile
      answer(); await tick();
    """)
    assert sent == ["/api/runs", "/api/runs?problems=1"], "the filter change was lost behind a poll"


def test_a_poll_during_a_poll_is_not_queued(client, tmp_path):
    """Only a reader's change is remembered; a timer tick that finds a read in flight is simply skipped."""
    sent = _load_harness(client, tmp_path, """
      load(true); load(true); answer(); await tick();
    """)
    assert sent == ["/api/runs"]


def test_many_changes_during_a_poll_send_one_read(client, tmp_path):
    sent = _load_harness(client, tmp_path, """
      load(true);
      problems.checked = true; load(); filter.value = "daily"; load(); load();
      answer(); await tick(); answer(); await tick();
    """)
    assert sent == ["/api/runs", "/api/runs?problems=1&q=daily"], "one extra request at most"
