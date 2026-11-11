"""Settings / Data health HTML renderer: table, Fetch buttons, help, empty state."""
from __future__ import annotations

import pytest

from RevenueChaser.module_settings import report


def _payload():
    return {
        "generated_at": "2026-09-25T20:00:00+00:00",
        "tickers": [
            {"ticker": "AAPL", "name": "Apple Inc."},
            {"ticker": "MU", "name": "Micron Technology"},
        ],
        "rows": [
            {
                "key": "universe", "label": "Equity universe",
                "tables": ["equity_universe"], "rows": 7403,
                "last_date": "—", "holes": 1710, "hole_label": "null market cap",
                "fetch": {"route": "/run/universe-stats", "label": "Fetch caps"},
                "fetch_alt": {"route": "/run/universe", "label": "Fetch universe"},
                "note": "provenance: __meta__",
            },
            {
                "key": "corpus", "label": "10-K / 20-F corpus",
                "tables": ["chain_filings", "edgar_documents"], "rows": 3278,
                "last_date": "2026-09-18", "holes": 42, "hole_label": "no filing",
                "fetch": {"route": "/run/edgar-sync", "label": "Fetch 10-K sync"},
                "note": "",
            },
        ],
    }








def test_still_has_no_filter_or_theme_chrome():
    html = report.render_settings_dashboard(_payload())
    assert "filter-panel" not in html
    assert "theme-coarse" not in html
    assert "mc-tabs" not in html


def test_renders_the_shared_ticker_combobox():
    """#383 C1b: the drill picker is the filter kit's combobox (RCCombobox); the constants-kit picker is gone."""
    html = report.render_settings_dashboard(_payload())
    assert 'id="ticker-combo"' in html
    assert 'role="combobox"' in html
    assert 'id="ticker-combo-list"' in html
    assert "RCCombobox.mount(" in html and 'class="rcf-combo"' in html
    assert "initTickerCombo" not in html and "tcRank" not in html
    assert 'class="rcf-bar"' not in html                 # an ops board: a picker, no filter bar


def test_picking_a_ticker_opens_the_drill_and_clearing_closes_it(tmp_path):
    from RevenueChaser.core.tests.board_js import run_board_js
    html = report.render_settings_dashboard(_payload())
    tail = """
var picked = [];
var _orig = onDrillPick; onDrillPick = function(t){ picked.push(t); };
DRILL_COMBO_OPTS.onPick({ticker: "AAPL", name: "Apple"});
DRILL_COMBO_OPTS.onClear();
console.log(JSON.stringify({picked: picked, rows: DRILL_COMBO_OPTS.rows === DATA, clear: DRILL_COMBO_OPTS.clear}));"""
    got = run_board_js(html, tail, tmp_path)
    assert got == {"picked": ["AAPL", None], "rows": True, "clear": True}


def test_the_drill_picker_is_mounted_with_its_options(tmp_path):
    """#383 C1b review (N3): the page mounts the kit's combobox with ``DRILL_COMBO_OPTS`` (no browser e2e covers the
    drill). ``RCCombobox.mount`` is stubbed between the kit's script and the board's."""
    import json
    import shutil
    import subprocess
    import pytest
    from RevenueChaser.core.tests.board_js import DOM_STUB, inline_scripts
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    scripts = inline_scripts(report.render_settings_dashboard(_payload()))
    board = next(i for i, b in enumerate(scripts) if "var DRILL_COMBO_OPTS" in b)
    assert any("root.RCCombobox" in b for b in scripts[:board]), "the kit loads before the board script"
    stub = "var mounted = []; RCCombobox.mount = function(o){ mounted.push(o); return {}; };"
    js = "\n;\n".join(scripts[:board] + [stub] + scripts[board:])
    js += "\nconsole.log(JSON.stringify({n: mounted.length, same: mounted[0] === DRILL_COMBO_OPTS}));"
    path = tmp_path / "drill.js"
    path.write_text(DOM_STUB + js, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {"n": 1, "same": True}


def test_embedded_data_escapes_script_close():
    payload = _payload()
    payload["tickers"] = [{"ticker": "XSS", "name": "Bad </script><script>alert(1)</script>"}]
    html = report.render_settings_dashboard(payload)
    assert "</script><script>alert(1)" not in html
    assert "\\u003c/script" in html




def _run_verdicts(js: str, cases: list) -> list:
    """Extract ``settingsJobVerdict`` from ``js`` and run it under node."""
    import json
    import re
    import shutil
    import subprocess
    import pytest
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    m = re.search(r"function settingsJobVerdict\(.*?\n}\n", js, re.S)
    assert m, "settingsJobVerdict not found in the Settings JS"
    script = m.group(0) + "\nvar cases = %s;\nconsole.log(JSON.stringify(cases.map(settingsJobVerdict)));\n" % json.dumps(cases)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


class TestJobVerdict:
    def test_every_status_maps_truthfully(self):
        cases = [
            {"ok": True, "job": {"status": "queued"}},
            {"ok": True, "job": {"status": "running"}},
            {"ok": True, "job": {"status": "done", "message": "3 caps filled"}},
            {"ok": True, "job": {"status": "error", "error": "boom"}},
            {"ok": True, "job": {"status": "cancelled"}},
            {"ok": True, "job": {"status": "weird"}},
            {"ok": False, "error": "unknown job"},
            {},
        ]
        got = _run_verdicts(report.JS, cases)
        states = [g["state"] for g in got]
        assert states == ["poll", "poll", "done", "error", "error", "error",
                          "error", "error"], got
        assert got[0]["text"] == "Queued…"
        assert got[1]["text"] == "Running…"
        assert got[2]["text"] == "3 caps filled"
        assert got[3]["text"] == "boom"
        assert got[4]["text"] == "Cancelled"
        assert got[5]["text"] == "Unknown job status: weird"
        assert "Job not found" in got[6]["text"]
        assert "Job not found" in got[7]["text"]

    def test_done_and_error_fall_back_to_defaults(self):
        got = _run_verdicts(report.JS, [
            {"ok": True, "job": {"status": "done"}},
            {"ok": True, "job": {"status": "error"}},
        ])
        assert got == [{"state": "done", "text": "Done"},
                       {"state": "error", "text": "Error"}]

    def test_poller_uses_the_verdict(self):
        assert "settingsJobVerdict(d)" in report.JS


# ── Board inputs section + visible errors (slice 2) ──────────────────────────
def _card(**over):
    card = {
        "key": "module_comovement", "label": "Co-movement",
        "tables": ["equity_universe", "swing_ohlc"],
        "price_basis": "daily price bars, split- and dividend-adjusted closes; correct for returns",
        "params": ["Window 252 sessions", "Min observations 126"],
        "as_of": "2026-09-25T20:00:00+00:00",
        "window": {"window_start": "2025-09-25", "prices_through": "2026-09-25"},
        "funnel_steps": [
            {"key": "scanned", "label": "Scanned universe", "count": 100,
             "kind": "start", "fix": None},
            {"key": "below_floor", "label": "Below the $300M classification floor",
             "count": 30, "kind": "drop", "by_design": True, "fix": None},
            {"key": "no_market_cap", "label": "No market cap", "count": 10,
             "kind": "drop", "by_design": False,
             "fix": {"route": "/run/universe-stats", "label": "Fetch caps"}},
            {"key": "clustered", "label": "Clustered", "count": 60, "kind": "result",
             "fix": None},
        ],
        "state": "ok", "error": None,
    }
    card.update(over)
    return card


def _with(**payload_over):
    p = _payload()
    p.update(payload_over)
    return report.render_settings_dashboard(p)








def test_board_data_is_escaped():
    html = _with(boards=[_card(label="<i>x</i>", params=["<b>p</b>"],
                               price_basis="<u>z</u>")])
    assert "<i>x</i>" not in html and "<b>p</b>" not in html and "<u>z</u>" not in html


def test_no_boards_no_section():
    assert "Board inputs" not in report.render_settings_dashboard(_payload())


# ── G6a (#124): the board inputs in the Data Flow artifact's language ────────
# One line per store table the board reads: provider → dataset → store file, with the dataset's
# coverage beside it. The facts come from the registry and from the rows ``collect()`` already
# built, so rendering asks the store nothing.
def _boards_html(rows=None, **over):
    """The Board inputs section alone, so a count cannot pick up the page's help text or legend."""
    return report._boards_section([_card(**over)], rows)


def test_a_board_card_shows_one_source_to_store_line_per_table():
    html = _boards_html(tables=["oipd_chain", "oipd_meta"])
    assert "What this board reads" in html
    assert html.count('class="bflow-row"') == 2
    assert html.count("Unusual Whales") == 2, "the provider is written in full, never as a code"
    assert html.count("Implied Probability chain") == 2
    assert html.count('class="db db-board"') == 2, "the OIPD tables live in boards/oipd.duckdb (S9)"


def test_every_provider_the_registry_can_name_is_written_in_words():
    """The copy rule: a source code such as ``finra/cftc`` never reaches the page."""
    from RevenueChaser.core import datasets

    for source in datasets.Source:
        words = report._provider_name(source)
        assert words and words != source.value, (source, words)
        assert words == report.source_words(source.value), (
            "the board rows, the dataset table and the diagram read one provider-name list")






def test_a_table_no_dataset_claims_says_so_in_words():
    html = _boards_html(tables=["telemetry_notes"])
    assert "not claimed by any dataset" in html
    assert 'class="db db-' not in html, "an unclaimed table has no store file to name"


def test_a_table_two_datasets_claim_does_not_read_as_unclaimed(monkeypatch):
    """A duplicate claim and no claim are different facts; the line must not confuse them."""
    from RevenueChaser.core import datasets

    monkeypatch.setitem(datasets._OWNERS, "shared_table", ("a", "b"))
    html = _boards_html(tables=["shared_table"])
    assert "claimed by more than one dataset" in html
    assert "not claimed by any dataset" not in html


def test_a_hostile_table_name_is_escaped_in_the_source_to_store_line():
    html = _boards_html(tables=["<script>x</script>"])
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html








def test_no_errors_no_error_list():
    html = _with(errors=[])
    assert 'id="errors"' not in html and 'class="row-err"' not in html


# ── editable Co-movement inputs (#40 Part B) ─────────────────────────────────
import re  # noqa: E402


def _field(key, label, value, default, kind="int", lo=None, hi=None, rng="",
           overridden=False, choices=None):
    return {"key": key, "label": label, "value": value, "default": default,
            "kind": kind, "lo": lo, "hi": hi, "range": rng,
            "overridden": overridden, "choices": choices}


def _settings_block(custom=False, **over):
    s = {
        "board": "module_comovement", "custom": custom,
        "inputs": [
            _field("WINDOW", "Default window (sessions)", 252, 252,
                   choices=[[21, "1m (21)"], [126, "6m (126)"], [252, "1y (252)"]],
                   rng="1m, 2m, 6m, 9m, 1y, 1.5y"),
            _field("MIN_OBS", "Minimum observations", 126, 126, lo=60,
                   rng="60–WINDOW"),
            _field("OOS_HOLDOUT", "Out-of-sample holdout (sessions)", 63, 63,
                   lo=42, hi=126, rng="42–126"),
        ],
        "fine": [
            _field("WINSOR_K", "Winsorize at (sigma)", 5.0, 5.0, kind="float",
                   lo=4.0, hi=8.0, rng="4–8"),
            _field("MIN_PERIODS", "Pairwise minimum overlap", 60, 60, lo=20,
                   rng="20–MIN_OBS"),
            _field("BENCH_STALE_SESSIONS", "Benchmark lag (sessions)", 2, 2,
                   lo=0, hi=5, rng="0–5"),
        ],
        "derived": {"key": "OOS_MIN_OBS",
                    "label": "Held-out observations needed (derived)", "value": 40},
    }
    s.update(over)
    return s


_NODE_STUB = r"""
var _els = {}, FETCHES = [], RESP = [];
function mk(id, extra){
  var a = {}, o = {id: id, textContent: "", innerHTML: "", disabled: false, value: "",
    open: false, hidden: false, style: {}, classList: {add(){}, remove(){}},
    setAttribute(k, v){ a[k] = String(v); }, getAttribute(k){ return k in a ? a[k] : null; },
    closest(){ return o._details || null; }, addEventListener(){}};
  return Object.assign(o, extra || {});
}
globalThis.document = {
  getElementById(id){ return _els[id] || (_els[id] = mk(id)); },
  querySelectorAll(sel){ return sel.indexOf("[data-setting]") >= 0 ? (globalThis.INPUTS || []) : []; },
  querySelector(){ return null; }
};
globalThis.location = {search: "", origin: "http://x"};
globalThis.parent = {postMessage(){}};
globalThis.fetch = function(url, opts){
  FETCHES.push({url: url, body: opts && opts.body ? JSON.parse(opts.body) : null});
  var r = RESP.shift() || {status: 200, body: {ok: true, job: {status: "running"}}};
  return Promise.resolve({status: r.status, ok: r.status < 400,
                          json(){ return Promise.resolve(r.body); }});
};
function input(key, orig, value){
  var a = {"data-setting": key, "data-orig": String(orig)};
  return {value: String(value), getAttribute(k){ return a[k] == null ? null : a[k]; }};
}
function saveBtn(){
  var b = mk("b"); _els["msg-set-module_comovement"] = mk("m");
  b.getAttribute = function(k){ return k === "data-board" ? "module_comovement" : null; };
  return b;
}
"""


def _run_settings_js(driver: str) -> dict:
    """Run the Settings board's own JS under node with a small DOM/fetch stub."""
    import json
    import shutil
    import subprocess
    import pytest
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    js = report.JS.replace("document.querySelectorAll('.fetch-btn').forEach", "[].forEach")
    js = js.replace("document.querySelectorAll('.set-save').forEach", "[].forEach")
    js = js.replace("document.querySelectorAll('.set-reset').forEach", "[].forEach")
    script = _NODE_STUB + js + "\n" + driver
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


class TestSaveHandler:
    def test_a_400_shows_the_messages_next_to_the_fields_and_does_not_reload(self):
        got = _run_settings_js(r"""
globalThis.INPUTS = [input("WINDOW", 252, 252), input("MIN_OBS", 126, 10),
                     input("OOS_HOLDOUT", 63, 63)];
RESP.push({status: 400, body: {ok: false, error: "x", messages: [
  "MIN_OBS must be between 60 and WINDOW (252); got 10",
  "WINDOW + OOS_HOLDOUT must not exceed the 500 stored sessions; got 252 + 126",
  "<img src=x onerror=alert(1)> is odd"]}});
var btn = saveBtn();
settingsSave(btn);
setTimeout(function(){
  console.log(JSON.stringify({
    reloaded: location.search !== "",
    posted: FETCHES.map(function(f){ return f.url; }),
    body: FETCHES[0].body,
    minobs: _els["err-MIN_OBS"].textContent,
    holdout: _els["err-OOS_HOLDOUT"].textContent,
    window_: (_els["err-WINDOW"] || {}).textContent || "",
    general: _els["seterr-module_comovement"].textContent,
    inner: _els["seterr-module_comovement"].innerHTML + _els["err-MIN_OBS"].innerHTML,
    btnDisabled: btn.disabled}));
  process.exit(0);
}, 30);
""")
        assert got["reloaded"] is False, "a rejected save must not reload the page"
        assert got["posted"] == ["/api/settings/board/module_comovement"]
        assert got["body"] == {"changes": {"MIN_OBS": 10}}, "only changed fields are sent"
        assert "between 60 and WINDOW" in got["minobs"]
        assert "WINDOW + OOS_HOLDOUT must not exceed" in got["holdout"]
        assert "WINDOW + OOS_HOLDOUT" in got["window_"]
        assert "onerror" in got["general"]           # unmatched message -> general area
        assert got["inner"] == "", "messages must be set as text, never as HTML"
        assert got["btnDisabled"] is False

    def test_a_202_starts_the_poll_and_does_not_reload_while_it_runs(self):
        got = _run_settings_js(r"""
globalThis.INPUTS = [input("MIN_OBS", 126, 200)];
RESP.push({status: 202, body: {ok: true, job_id: "J1", values: {}}});
RESP.push({status: 200, body: {ok: true, job: {status: "running"}}});
var btn = saveBtn();
settingsSave(btn);
setTimeout(function(){
  console.log(JSON.stringify({
    urls: FETCHES.map(function(f){ return f.url; }),
    msg: _els["msg-set-module_comovement"].textContent,
    reloaded: location.search !== "", btnDisabled: btn.disabled}));
  process.exit(0);
}, 60);
""")
        assert got["urls"] == ["/api/settings/board/module_comovement", "/api/job/J1"]
        assert got["msg"] == "Running…"
        assert got["reloaded"] is False and got["btnDisabled"] is True

    def test_a_finished_job_reloads_with_refresh(self):
        got = _run_settings_js(r"""
globalThis.INPUTS = [input("MIN_OBS", 126, 200)];
RESP.push({status: 202, body: {ok: true, job_id: "J2", values: {}}});
RESP.push({status: 200, body: {ok: true, job: {status: "done", message: "ok"}}});
settingsSave(saveBtn());
setTimeout(function(){ console.log(JSON.stringify({search: location.search})); process.exit(0); }, 60);
""")
        assert got["search"] == "?refresh=1"

    def test_nothing_changed_sends_nothing(self):
        got = _run_settings_js(r"""
globalThis.INPUTS = [input("MIN_OBS", 126, 126)];
settingsSave(saveBtn());
setTimeout(function(){
  console.log(JSON.stringify({n: FETCHES.length,
    msg: _els["msg-set-module_comovement"].textContent}));
  process.exit(0); }, 20);
""")
        assert got["n"] == 0 and "No changes" in got["msg"]

    def test_reset_posts_reset_true_and_polls(self):
        got = _run_settings_js(r"""
RESP.push({status: 202, body: {ok: true, job_id: "J3", values: {}}});
settingsReset(saveBtn());
setTimeout(function(){ console.log(JSON.stringify({f: FETCHES})); process.exit(0); }, 40);
""")
        assert got["f"][0]["body"] == {"reset": True}
        assert got["f"][1]["url"] == "/api/job/J3"

    def test_one_shared_verdict_function(self):
        from RevenueChaser.core import reporting_common as rc
        assert "function settingsJobVerdict(" in rc.JOB_JS
        assert "function settingsJobVerdict(" in report.JS
        assert report.JS.count("function settingsJobVerdict(") == 1


# ── funnel note + type rows on the Board inputs card (#87) ───────────────────
def _typed_card(note_label="Unknown security type — kept", **over):
    steps = [
        {"key": "scanned", "label": "Scanned universe", "count": 100, "kind": "start",
         "fix": None},
        {"key": "funds_etfs", "label": "Funds / ETFs", "count": 12, "kind": "drop",
         "by_design": True, "fix": None},
        {"key": "unknown_type", "label": note_label, "count": 7, "kind": "note",
         "fix": None, "link": {"tab": "module_settings", "anchor": "identity",
                               "label": "Settings → Identity"}},
        {"key": "clustered", "label": "Clustered", "count": 88, "kind": "result",
         "fix": None},
    ]
    return _card(funnel_steps=steps, **over)








def test_the_note_row_has_its_own_style():
    assert ".frow2.note" in report.CSS


# ── Identity health card (issue #89) ─────────────────────────────────────────
IDENTITY_IDS = ("identity-health", "identity-run", "identity-spdr", "identity-types",
                "identity-sources", "identity-conflicts", "identity-stale")


def _identity_row():
    return {"key": "identity", "label": "Identity spine",
            "tables": ["security_master", "identity_conflicts", "identity_source_cache", "identity_runs"],
            "rows": 7366, "last_date": "2026-09-30", "holes": 12, "hole_label": "unclassified ≥$300M",
            "fetch": {"route": "/run/identity", "label": "Fetch identity"}, "note": "last run ok"}


def _src(status="ok", rows=0, fetched=0, error=None, selected=True):
    return {"status": status, "selected": selected, "rows": rows, "fetched": fetched, "error": error}


def _identity(**over):
    ident = {
        "last_run": {
            "run_id": "r2", "status": "partial", "started_at": "2026-09-30T00:00:00Z",
            "finished_at": "2026-09-30T00:06:00Z",
            "sources": {"sec_exchange": _src(rows=10300), "nasdaq": _src("error", error="HTTP 503 <b>down</b>"),
                        "openfigi": _src(rows=120, fetched=120), "fdic": _src("skipped", selected=False)},
            "counts": {"tickers": 7366, "conflicts": 266, "spdr_checked": 480, "spdr_agree": 478}},
        "floor": 300_000_000.0,
        "type_mix": [{"type": "common", "all": 5200, "floor": 2900, "excluded": False},
                     {"type": "etp", "all": 213, "floor": 40, "excluded": True},
                     {"type": "unknown", "all": 3, "floor": 0, "excluded": False}],
        "source_mix": [{"bucket": "official", "all": 1501, "floor": 1501},
                       {"bucket": "provider", "all": 5100, "floor": 1200},
                       {"bucket": "SIC", "all": 325, "floor": 20},
                       {"bucket": "type rule", "all": 400, "floor": 60},
                       {"bucket": "unclassified", "all": 64, "floor": 12}],
        "conflicts": {
            "as_of": "2026-09-30T00:00:00Z",
            "by_kind": [{"kind": "sector", "count": 200}, {"kind": "spdr", "count": 3}],
            "top": [{"ticker": "AAPL", "kind": "spdr", "field": "sector", "side_a": "Technology",
                     "side_b": "Communication Services", "note": "SPDR XLC holdings",
                     "last_seen": "2026-09-30T00:00:00Z", "market_cap": 3.0e12},
                    {"ticker": "*", "kind": "source", "field": "uw_snapshot", "side_a": "previous snapshot",
                     "side_b": "new snapshot (1500 rows)", "note": "gated", "last_seen": "2026-09-30T00:00:00Z",
                     "market_cap": None}],
            "older": {"count": 12, "since": "2026-09-20T00:00:00Z"}},
        "stale": [{"source": "sec_exchange", "newest": "2026-09-30T00:00:00Z", "rows": 7000, "past_window": None,
                   "window": "every run"},
                  {"source": "sec_submissions", "newest": "2026-09-25T00:00:00Z", "rows": 6900, "past_window": 17,
                   "window": "30 days"},
                  {"source": "fdic", "newest": None, "rows": 0, "past_window": 0, "window": "90 days"}],
        "spdr": {"state": "ok", "checked": 480, "agree": 478, "date": "2026-09-30T00:00:00Z"},
    }
    ident.update(over)
    return ident


def _card_html(**over):
    return _with(rows=_payload()["rows"] + [_identity_row()], identity=_identity(**over))








def test_a_payload_without_identity_renders_no_card():
    assert "identity-health" not in report.render_settings_dashboard(_payload())




























# --- Automations panel (#130) ------------------------------------------------------

def _autos():
    from RevenueChaser.core import automations
    return automations.static_rows()




def test_automations_section_is_absent_without_rows():
    html = report.render_settings_dashboard(_payload())
    assert 'id="auto-body"' not in html




def test_an_empty_automations_list_says_so():
    html = _render_rows([])
    assert "No scheduled tasks are registered." in html
    assert "<details" not in html


def test_the_panel_uses_rcesc_and_never_the_drill_escaper():
    start = report.AUTOMATIONS_JS
    assert "rcEsc(" in start and "tcEsc" not in start
    assert "innerHTML" in start


def test_confirm_names_the_task_for_install_remove_and_run():
    js = report.AUTOMATIONS_JS
    for verb in ("install", "remove", "run"):
        assert f"'{verb}'" in js
    assert "confirm(" in js


def _render_rows(rows: list, available: bool = True) -> str:
    import json
    import shutil
    import subprocess
    import pytest
    from RevenueChaser.core import html_safe
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    script = (html_safe.ESC_JS + report.AUTOMATIONS_JS +
              "\nconsole.log(JSON.stringify(autoTableHtml(%s, %s)));\n"
              % (json.dumps(rows), json.dumps(available)))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_renderer_escapes_every_field_of_a_hostile_row():
    bad = "<img src=x onerror=__x(1)>"
    row = {k: bad for k in ("name", "label", "schedule", "command", "settings", "log_path",
                            "description", "state", "result_text", "result_kind",
                            "last_run", "next_run", "last_success_age")}
    row.update(tables_written=[bad], log_tail=[bad], missed_runs=bad)
    html = _render_rows([row])
    assert "<img" not in html and "&lt;img" in html


def test_renderer_buttons_follow_the_state():
    rows = [dict(_autos()[0], state=s) for s in
            ("not_installed", "enabled", "disabled", "running", "unavailable", "weird")]
    out = [_render_rows([r]) for r in rows]
    acts = lambda h: sorted(set(__import__("re").findall(r'data-act="(\w+)"', h)))  # noqa: E731
    assert acts(out[0]) == ["install"]
    assert acts(out[1]) == ["disable", "remove", "run"]
    assert acts(out[2]) == ["enable", "remove"]
    assert acts(out[3]) == [] and acts(out[4]) == [] and acts(out[5]) == []


def test_renderer_shows_neutral_not_red_for_a_task_that_never_ran():
    row = dict(_autos()[0], state="enabled", result_kind="neutral", result_text="not run yet")
    html = _render_rows([row])
    assert "not run yet" in html and "st-bad" not in html
    red = _render_rows([dict(row, result_kind="red", result_text="failed (0x1)")])
    assert "st-bad" in red


def test_rows_without_live_state_show_checking():
    assert "checking" in _render_rows(_autos()).lower()



def test_renderer_escapes_a_hostile_name_inside_data_name():
    bad = 'x" onmouseover="__x(1)" y="<img src=x onerror=__x(2)>'
    html = _render_rows([dict(_autos()[0], name=bad, state="enabled")])
    assert 'data-name="x&quot; onmouseover=&quot;__x(1)&quot;' in html
    assert "<img" not in html


def test_error_state_is_red_and_offers_no_button():
    row = dict(_autos()[0], state="error", result_kind="red",
               result_text="unreadable scheduler answer")
    html = _render_rows([row])
    assert "st-bad" in html and "Scheduler error" in html
    assert "data-act" not in html


# --- G10 (#124): one line per automation, the rest behind a click -------------------

def _one_line(html: str) -> str:
    """The closed line of the first automation: the whole ``<summary>`` element."""
    start = html.index("<summary>")
    return html[start:html.index("</summary>", start) + len("</summary>")]


def _behind_the_click(html: str) -> str:
    """Everything after the first automation's closed line: what opening it shows."""
    return html[html.index("</summary>") + len("</summary>"):]


def _live_row(**fields) -> dict:
    """The first registry row as the poll returns it: installed, last run ok, next run known."""
    row = dict(_autos()[0], state="enabled", result_kind="ok", result_text="ok",
               last_run="2026-10-23T04:00:05", next_run="2026-10-24T04:00:00",
               missed_runs=0, last_success_age="8 h ago", log_tail=[])
    row.update(fields)
    return row


def test_the_automation_line_carries_its_name_schedule_state_result_and_next_run():
    line = _one_line(_render_rows([_live_row()]))
    first = _autos()[0]
    assert first["label"] in line and first["name"] in line
    assert first["schedule"] in line
    assert "Enabled" in line
    assert "last result: ok" in line
    assert "next run: 2026-10-24 04:00" in line


def test_a_task_that_is_not_installed_shows_its_state_and_no_dash_result():
    row = dict(_autos()[0], state="not_installed", result_kind="none", result_text="—",
               last_run=None, next_run=None, missed_runs=0, last_success_age="never")
    line = _one_line(_render_rows([row]))
    assert "Not installed" in line
    assert "last result" not in line and "next run" not in line


def test_the_automation_line_leaves_the_long_text_behind_the_click():
    auto = _autos()[0]
    line = _one_line(_render_rows([_live_row()]))
    assert auto["description"][:60] not in line
    assert auto["command"] not in line
    assert auto["settings"] not in line
    assert auto["log_path"] not in line


def test_the_line_shows_the_schedule_time_and_the_chain_condition_waits_behind_the_click():
    auto = next(a for a in _autos() if a["name"] == "RC-Screeners-Daily")
    row = dict(auto, state="enabled", result_kind="ok", result_text="ok",
               last_run="2026-10-22T23:30:00", next_run="2026-10-23T23:30:00", missed_runs=0,
               last_success_age="18 h ago", log_tail=[])
    html = _render_rows([row])
    line = _one_line(html)
    assert "23:30 (local)" in line and "after RC-Daily-Bars" not in line
    assert "weekly 23:30 (local)" in line and "Wednesday" not in line
    opened = _behind_the_click(html)
    assert "after RC-Daily-Bars" in opened and "Wednesday" in opened


def test_the_expanded_part_keeps_the_description_command_and_run_conditions_word_for_word():
    auto = _autos()[0]
    opened = _behind_the_click(_render_rows([_live_row()]))
    esc_js = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")  # noqa: E731
    assert esc_js(auto["description"]) in opened
    assert esc_js(auto["command"]) in opened
    assert esc_js(auto["settings"]) in opened
    assert esc_js(auto["schedule"]) in opened
    assert "What it does" in opened and "Command" in opened and "Run conditions" in opened


def test_every_automation_is_a_closed_details_row_with_its_own_id():
    html = _render_rows(_autos())
    for auto in _autos():
        assert f'<details class="auto-item" id="auto-{auto["name"]}">' in html
    assert html.count("<details") == html.count("</details>") == len(_autos())


def test_the_line_opens_from_the_keyboard_without_a_click_handler():
    line = _one_line(_render_rows([_live_row()]))
    assert line.startswith("<summary>") and "onclick" not in line and "onkeydown" not in line


def test_the_buttons_are_behind_the_click():
    html = _render_rows([_live_row()])
    assert "data-act" not in _one_line(html)
    assert 'data-act="run"' in _behind_the_click(html)


def test_the_expanded_part_keeps_the_store_tables_the_log_and_its_tail():
    auto = next(a for a in _autos() if a["name"] == "RC-Daily-Bars")
    row = dict(auto, state="enabled", result_kind="ok", result_text="ok",
               last_run="2026-10-23T22:35:00", next_run="2026-10-24T22:35:00", missed_runs=0,
               last_success_age="7 h ago",
               log_tail=["=== 2026-10-23T22:35:00 exit 0", "wrote 3,582 rows"])
    opened = _behind_the_click(_render_rows([row]))
    for table in auto["tables_written"]:
        assert table in opened
    assert auto["log_path"] in opened and "wrote 3,582 rows" in opened
    assert "Store tables it writes" in opened


def test_the_automation_line_escapes_hostile_schedule_result_and_next_run():
    bad = "<img src=x onerror=__x(1)>"
    line = _one_line(_render_rows([_live_row(schedule=bad, result_text=bad, next_run=bad)]))
    assert "<img" not in line and line.count("&lt;img") == 3


def _auto_poll(open_ids: list, focused_id: str | None) -> dict:
    """``autoRender`` against a fake DOM: which rows it left open, and whether it put focus back."""
    import json
    import shutil
    import subprocess
    import pytest
    from RevenueChaser.core import html_safe
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    script = (html_safe.ESC_JS + report.AUTOMATIONS_JS
              + "\nvar openIds = " + json.dumps(open_ids) + ";"
              + "\nvar focusedId = " + json.dumps(focused_id) + ";"
              + "\nvar focused = false;"
              + "\nvar head = {focus: function(){ focused = true; }};"
              + "\nvar made = {};"
              + "\nvar body = {innerHTML: '', querySelectorAll: function(){"
              + " return openIds.map(function(id){ return {id: id}; }); }};"
              + "\nglobalThis.document = {"
              + " activeElement: focusedId ? {closest: function(){ return {id: focusedId}; }} : null,"
              + " getElementById: function(id){"
              + " if(id === 'auto-body') return body;"
              + " if(!made[id]) made[id] = {id: id, open: false,"
              + " querySelector: function(){ return head; }};"
              + " return made[id]; }};"
              + "\nautoRender(" + json.dumps(_autos()) + ", true);"
              + "\nconsole.log(JSON.stringify({rendered: body.innerHTML.length, focused: focused,"
              + " open: openIds.map(function(id){ return made[id] ? made[id].open : null; })}));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_row_the_reader_opened_stays_open_across_a_poll():
    """The panel re-renders on every poll while a task runs; the row being watched must not snap shut."""
    seen = _auto_poll(open_ids=["auto-RC-Daily-Bars"], focused_id=None)
    assert seen["rendered"] > 0 and seen["open"] == [True]


def test_the_focused_automation_keeps_focus_across_a_poll():
    """A keyboard reader inside an opened row must not be dropped back to the page by the 5 s poll."""
    seen = _auto_poll(open_ids=["auto-RC-Daily-Bars"], focused_id="auto-RC-Daily-Bars")
    assert seen["open"] == [True] and seen["focused"] is True


def _drill_rows(detail: dict) -> str:
    """``drillFill(payload)`` run in node: the helpers of ``DRILL_JS`` without the DOM wiring at its end."""
    import json
    import shutil
    import subprocess
    import pytest
    from RevenueChaser.core import html_safe
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    helpers = report.DRILL_JS[:report.DRILL_JS.index("function onDrillPick")]
    script = (html_safe.ESC_JS + helpers +
              "\nconsole.log(JSON.stringify(drillFill(%s)));\n" % json.dumps(detail))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_lookup_shows_both_ends_of_each_range():
    """avo, 11 Nov 2026: "ne zamandan kaldigini" — the first day, not only the newest."""
    html = _drill_rows({"ticker": "MU", "name": "Micron", "files": [
        {"kind": "prices", "name": "prices.duckdb", "title": "Prices and shares", "looked_at": 4,
         "tables": [{"table": "price_bar_daily", "rows": 2, "first": "2021-01-04", "last": "2026-11-10"}]}]})
    assert '<span class="dlbl">price_bar_daily</span>' in html
    assert "2 rows · 2021-01-04 → 2026-11-10" in html
    assert "Prices and shares" in html, "the group is headed by what the file is for"


def test_a_table_with_no_date_shows_its_row_count_alone():
    html = _drill_rows({"ticker": "MU", "files": [
        {"kind": "ownership", "name": "ownership.duckdb", "title": "Who owns it", "looked_at": 2,
         "tables": [{"table": "ibkr_shortable", "rows": 1, "first": None, "last": None}]}]})
    assert "1 row · no date kept" in html


def test_a_file_that_holds_nothing_for_the_name_says_how_many_tables_were_asked():
    """#715, compact: the files holding nothing share one short group instead of a group each."""
    html = _drill_rows({"ticker": "MU", "files": [
        {"kind": "options", "name": "options.duckdb", "title": "Options", "looked_at": 7, "tables": []}]})
    assert 'id="drill-empty"' in html and "Options" in html
    assert "nothing stored · 7 tables asked" in html


def test_a_missing_file_is_a_line_not_an_empty_group():
    html = _drill_rows({"ticker": "MU", "files": [
        {"kind": "options", "name": "options.duckdb", "title": "Options", "looked_at": 0, "tables": [],
         "error": "no options.duckdb yet"}]})
    assert "no options.duckdb yet" in html


def test_a_table_that_could_not_be_read_says_so_by_name():
    html = _drill_rows({"ticker": "MU", "files": [
        {"kind": "prices", "name": "prices.duckdb", "title": "Prices", "looked_at": 1,
         "tables": [{"table": "price_bar_daily", "rows": None, "error": "IOError: locked"}]}]})
    assert "could not read — IOError: locked" in html


def test_the_lookup_shows_the_identifiers_on_one_line_and_skips_the_empty_ones():
    """avo, 11 Nov 2026 ("daha compact"): one wrapped line of chips, and no row for a field we do not have."""
    html = _drill_rows({"ticker": "MU", "cik": "0000723125", "figi": "BBG000BLNNH6",
                        "security_id": "sec:7", "cusip": None, "isin": None, "files": []})
    assert '<div class="dident">' in html
    assert '<span class="dlbl">CIK</span> 0000723125' in html
    assert '<span class="dlbl">FIGI</span> BBG000BLNNH6' in html
    assert '<span class="dlbl">id</span> sec:7' in html
    assert "CUSIP" not in html and "ISIN" not in html, "a field with no value takes no room"
    rich = _drill_rows({"name": "Micron", "sector": "Technology", "industry": "Semiconductors", "files": []})
    assert "Technology" in rich and "Semiconductors" in rich and "Micron" in rich


def test_the_lookup_escapes_a_hostile_identifier_and_table_name():
    bad = "<img src=x onerror=__x(1)>"
    html = _drill_rows({"ticker": "MU", "cik": bad, "figi": bad, "cusip": bad, "isin": bad, "files": [
        {"kind": "prices", "name": bad, "title": bad, "looked_at": 1,
         "tables": [{"table": bad, "rows": 1, "first": None, "last": None}]}]})
    assert "<img" not in html and html.count("&lt;img") >= 5


def test_the_lookup_errors_are_listed_once_and_only_when_there_are_any():
    assert 'id="drill-errors"' not in _drill_rows({"ticker": "MU", "files": [], "errors": []})
    html = _drill_rows({"ticker": "MU", "files": [], "errors": ["options: RuntimeError: disk gone"]})
    assert 'id="drill-errors"' in html and "options: RuntimeError: disk gone" in html


def test_panel_posts_send_the_panel_header():
    assert "'X-RC-Panel': 'automations'" in report.AUTOMATIONS_JS


def test_null_cap_text_does_not_claim_it_is_outside_the_universe():
    """DF99: a NULL cap also occurs before the caps backfill has resolved it.

    #715: the market cap left the page with the old drill, so the page must claim nothing about it at all.
    """
    html = report.render_settings_dashboard(_payload())
    assert "not in the $300M classification universe" not in html
    assert "no cap stored" not in html


# ── S5 of the Data Health plan (#124): Source / Coverage / Fresh and a detail row ────────────
def _s5_payload():
    """The two rows above, with the registry facts ``_attach_registry`` adds."""
    p = _payload()
    p["rows"][0].update(
        source="yfinance", writer="RC-Screeners-Daily",
        writer_note="caps-only fill is POST /run/universe-stats",
        coverage={"covered": 812, "expected": 905, "fraction": 812 / 905,
                  "label": "812 / 905 (90%)", "newest": "2026-09-20", "problems": []},
        fresh={"state": "date", "date": "2026-09-20", "text": "2026-09-20"})
    return p


def _thead_cells(html: str) -> list[str]:
    """The table's header cells as plain text (a header may hold a button, e.g. Source)."""
    import re
    # The inventory's own table, not the control board's tables above it.
    if 'table class="inv"' in html:
        html = html[html.index('table class="inv"'):]
    thead = re.search(r"<thead><tr>(.*?)</tr></thead>", html, re.S).group(1)
    return [h.split("<")[0].strip()
            for h in re.findall(r"<th[^>]*>(.*?)</th>", thead, re.S)]


def _main_row(html: str, key: str) -> str:
    start = html.index(f'id="row-{key}"')
    return html[start:html.index("</tr>", start)]


def _detail_row(html: str, key: str) -> str:
    start = html.index(f'id="det-{key}"')
    return html[start:html.index("</tr>", start)]


def _cells(row: str) -> list[str]:
    import re
    return [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]


















def test_the_detail_toggle_and_the_grouping_run_under_node(tmp_path):
    """The two handlers: a detail row opens and closes from its button, and grouping keeps a
    detail row with its dataset row and puts the worst-coverage order back."""
    import json
    import shutil
    import subprocess
    import pytest
    from RevenueChaser.core.tests.board_js import function_source
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    js = (function_source(report.JS, "settingsDetail")
          + function_source(report.JS, "settingsRowPairs")
          + function_source(report.JS, "settingsGroupRows")
          + r"""
var els = {det: {hidden: true}};
globalThis.document = {getElementById: function(id){ return els[id] || null; }};
function button(){
  var a = {"aria-controls": "det", "aria-expanded": "false"};
  return {getAttribute: function(k){ return a[k] == null ? null : a[k]; },
          setAttribute: function(k, v){ a[k] = String(v); },
          state: function(){ return a["aria-expanded"]; }};
}
var b = button();
settingsDetail(b);
var opened = {expanded: b.state(), hidden: els.det.hidden};
settingsDetail(b);
var closed = {expanded: b.state(), hidden: els.det.hidden};
function tr(kind, source, order){
  return {className: kind, attrs: {"data-source": source, "data-order": String(order)},
          getAttribute: function(k){ return this.attrs[k] == null ? null : this.attrs[k]; }};
}
function body(children){
  return {children: children, out: [],
          appendChild: function(el){ this.out.push(el.attrs["data-order"] +
                                                    (el.className === "detail" ? "d" : "r")); }};
}
var tbody = body([tr("row", "uw", 0), tr("detail", "", 0), tr("row", "sec", 1), tr("detail", "", 1),
                  tr("row", "derived", 2), tr("detail", "", 2)]);
settingsGroupRows(tbody, true);
var grouped = tbody.out.slice();
tbody.out = [];
settingsGroupRows(tbody, false);
console.log(JSON.stringify({opened: opened, closed: closed,
                            grouped: grouped, restored: tbody.out}));
""")
    path = tmp_path / "settings_handlers.js"
    path.write_text(js, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["opened"] == {"expanded": "true", "hidden": False}
    assert got["closed"] == {"expanded": "false", "hidden": True}
    assert got["grouped"] == ["2r", "2d", "1r", "1d", "0r", "0d"], "source order, detail with its row"
    assert got["restored"] == ["0r", "0d", "1r", "1d", "2r", "2d"], "back to worst coverage first"


# ── G2 of the GUI redesign (#124): coverage bar, store chip, legend ──────────────────────────
def _cov_table(coverage: dict, **over) -> str:
    """One dataset rendered through ``_table`` with the coverage record ``_attach_registry`` builds."""
    row = {"key": "quotes", "label": "Quotes", "tables": ["quote_snapshot"], "rows": 10,
           "last_date": "—", "holes": None, "hole_label": "", "fetch": None, "note": "",
           "source": "uw", "coverage": coverage, "fresh": None}
    row.update(over)
    return report._table([row])


def test_the_coverage_cell_draws_a_bar_before_its_honest_label():
    """A thin bar with its percentage beside it: the number stays text, so colour is not the signal."""
    html = _cov_table({"fraction": 0.9, "label": "812 / 905 (90%)"})
    bar = ('<span class="covbar st-warn" role="img" aria-label="coverage 90 percent">'
           '<span style="width:90%"></span></span>')
    assert bar in html
    assert html.index(bar) < html.index("812 / 905 (90%)"), "the bar leads, the label follows"
    assert _cells(_main_row(html, "quotes"))[2] == "812 / 905 (90%)"


@pytest.mark.parametrize("label", ["n/a", "unknown", "error"])
def test_a_coverage_with_no_fraction_draws_no_bar_and_no_zero_percent_look(label):
    html = _cov_table({"fraction": None, "label": label})
    assert "covbar" not in html
    assert "width:0%" not in html
    assert _cells(_main_row(html, "quotes"))[2].startswith(label)


@pytest.mark.parametrize("label", ["n/a", "unknown"])
def test_a_coverage_with_no_population_reads_muted(label):
    """No population is a hole in the denominator, not a red failure: muted text, no bar."""
    html = _cov_table({"fraction": None, "label": label})
    assert f'<span class="muted">{label}</span>' in html


def test_a_coverage_error_stays_red():
    html = _cov_table({"fraction": None, "label": "error", "problems": []})
    assert '<span class="warnv">error</span>' in html and "covbar" not in html


def test_over_100_percent_draws_a_full_bar_and_keeps_the_real_number():
    html = _cov_table({"fraction": 1.24, "label": "1,122 / 905 (124%)"})
    assert '<span style="width:100%"></span>' in html
    assert "width:124%" not in html
    assert "1,122 / 905 (124%)" in html, "the text stays honest above 100%"
    assert 'aria-label="coverage 100 percent"' in html


@pytest.mark.parametrize("fraction, band", [(0.69, "st-bad"), (0.70, "st-warn"),
                                            (0.94, "st-warn"), (0.95, "st-ok")])
def test_the_bar_colour_follows_the_coverage_band(fraction, band):
    html = _cov_table({"fraction": fraction, "label": "x"})
    assert f'<span class="covbar {band}"' in html


@pytest.mark.parametrize("db", ["raw", "edgar", "history", "prices"])
def test_each_store_file_gets_a_pill_whose_text_names_it(db):
    html = _cov_table({"fraction": None, "label": "n/a"}, db=db)
    pill = f'<span class="db db-{db}" title="stored in {db}.duckdb under .store/">{db}</span>'
    main = _main_row(html, "quotes")
    assert pill in main
    assert _cells(main)[0] == f"Quotes {db}", "the pill's text sits beside the name"
    assert html.count(pill) >= 2, "the legend under the table explains the pill once"


def test_a_row_without_a_store_file_draws_no_pill():
    html = _cov_table({"fraction": None, "label": "n/a"})           # no "db" key at all
    assert 'class="db ' not in _main_row(html, "quotes")


def test_a_store_file_outside_the_palette_draws_no_pill_and_never_takes_a_class_from_it():
    bad = "<img src=x onerror=__x(1)>"
    html = _cov_table({"fraction": None, "label": "n/a"}, db=bad)
    assert 'class="db ' not in _main_row(html, "quotes")
    assert "<img" not in html


def test_a_legend_under_the_table_explains_each_pill_once():
    html = report._table([{"key": "quotes", "label": "Quotes", "tables": [], "note": ""}])
    assert html.index("</tbody></table>") < html.index('id="db-legend"'), "under the table"
    legend = html[html.index('id="db-legend"'):]
    assert "Store file" in legend
    for db in ("raw", "edgar", "history", "prices"):
        assert f'class="db db-{db}"' in legend, db


# ── S5b / G3 of the GUI brief (#124): the Fresh cell's named states ─────────────────────────
# The cells used to print a bare dash for four different situations. Each state now carries its
# own words, and a failed query is red and says it is not an empty dataset.

def _fresh_row(record, **over) -> dict:
    """One dataset row carrying the Fresh record ``inventory._fresh_record`` builds."""
    row = {"key": "quotes", "label": "Quotes", "tables": ["quote_snapshot"], "rows": 10,
           "last_date": "2026-09-20", "holes": None, "hole_label": "", "fetch": None, "note": "",
           "source": "uw", "coverage": {"fraction": 1.0, "label": "905 / 905 (100%)"},
           "fresh": record}
    row.update(over)
    return row


def _fresh_text(html: str, key: str = "quotes") -> str:
    """The Fresh cell's text (the fourth cell) of one row."""
    return _cells(_main_row(html, key))[3]


def _last_date_text(html: str, key: str = "quotes") -> str:
    """The Last data date cell's text (the fifth cell) of one row."""
    return _cells(_main_row(html, key))[4]


@pytest.mark.parametrize("state, text", [
    ("date", "2026-09-20"),
    ("no_time_column", "no time column - see the provenance rows"),
    ("run_log", "taken from the run log, 2026-10-20"),
    ("empty", "no date stored yet"),
    ("retired", "retired - no freshness claim made"),
    ("bookkeeping", "store bookkeeping - no freshness claim"),
    ("failed", "query failed - this is not an empty dataset"),
])
def test_each_fresh_state_prints_its_own_words_and_never_a_dash(state, text):
    html = report._table([_fresh_row({"state": state, "date": None, "text": text})])
    assert _fresh_text(html) == text
    assert _fresh_text(html) != "—", "the Fresh cell stopped printing a bare dash"


def test_a_failed_freshness_query_is_called_out_in_colour():
    html = report._table([_fresh_row({"state": "failed", "date": None,
                                      "text": "query failed - this is not an empty dataset"})])
    assert ('<span class="warnv">query failed - this is not an empty dataset</span>'
            in _main_row(html, "quotes"))


def test_a_failed_query_never_renders_like_a_table_that_holds_no_rows():
    """The failure this slice exists for: the broken read and the empty table must not look alike."""
    failed = report._table([_fresh_row({"state": "failed", "date": None,
                                        "text": "query failed - this is not an empty dataset"})])
    empty = report._table([_fresh_row({"state": "empty", "date": None,
                                       "text": "no date stored yet"})])
    assert _fresh_text(failed) != _fresh_text(empty)
    assert 'class="warnv"' in _main_row(failed, "quotes"), "a failure is called out in colour"
    assert 'class="warnv"' not in _main_row(empty, "quotes"), "an empty table is not a failure"


@pytest.mark.parametrize("state, text", [
    ("date", "2026-09-20"),
    ("no_time_column", "no time column - see the provenance rows"),
    ("empty", "no date stored yet"),
    ("run_log", "taken from the run log, 2026-10-20"),
    ("retired", "retired - no freshness claim made"),
    ("bookkeeping", "store bookkeeping - no freshness claim"),
])
def test_a_failed_query_never_renders_like_any_other_state(state, text):
    """A broken read is its own state, in colour: it must not borrow the words of any other."""
    failed = report._table([_fresh_row({"state": "failed", "date": None,
                                        "text": "query failed - this is not an empty dataset"})])
    other = report._table([_fresh_row({"state": state, "date": None, "text": text})])
    assert _fresh_text(failed) != _fresh_text(other)
    assert 'class="warnv"' in _main_row(failed, "quotes")
    assert 'class="warnv"' not in _main_row(other, "quotes")


def test_the_fresh_words_are_escaped_and_no_state_name_becomes_a_class():
    bad = "<img src=x onerror=__x(1)>"
    html = report._table([_fresh_row({"state": bad, "date": None, "text": bad})])
    row = _main_row(html, "quotes")
    assert "<img" not in html
    assert "&lt;img src=x onerror=__x(1)&gt;" in row
    assert 'class="warnv"' not in row, "colour comes from the state name, never from the text"


def test_a_row_with_no_fresh_record_keeps_the_dash():
    """Only a row the registry does not know reaches the table without a record."""
    html = report._table([_fresh_row(None)])
    assert _fresh_text(html) == "—"


def test_the_last_data_date_stops_printing_a_bare_dash():
    """A row with no date of its own says so in words; the Freshness cell beside it says why."""
    html = report._table([_fresh_row({"state": "no_time_column", "date": None,
                                      "text": "no time column - see the provenance rows"},
                                     last_date="—")])
    assert _last_date_text(html) == "no date of its own"
    assert _last_date_text(report._table([_fresh_row({"state": "date",
                                                      "date": "2026-09-20",
                                                      "text": "2026-09-20"})])) == "2026-09-20"


def test_the_commitments_of_traders_reads_fresh_neither_stale_nor_failed():
    """It is four weeks thin by design (the first write wins, it grows forward): a recent week is
    fresh, and a small row count is not a failure."""
    html = report._table([_fresh_row({"state": "date", "date": "2026-10-20", "text": "2026-10-20"},
                                     key="cftc", label="E-mini Commitments of Traders",
                                     tables=["cftc_cot"], rows=4, last_date="2026-10-20")])
    row = _main_row(html, "cftc")
    assert _fresh_text(html, "cftc") == "2026-10-20"
    assert "stale" not in row.lower() and "failed" not in row.lower()
    assert 'class="warnv"' not in row


def test_the_run_log_caveat_sits_in_the_detail_row_beside_the_words():
    """The registry's #82/#83 caveat is not retyped in the cell: it is shown where the notes are."""
    html = report._table([_fresh_row({"state": "run_log", "date": "2026-10-20",
                                      "text": "taken from the run log, 2026-10-20"},
                                     fresh_note="from the run log; not trustworthy until #82 "
                                                "and #83 are fixed")])
    assert "not trustworthy until #82" in _detail_row(html, "quotes")
    assert "not trustworthy until #82" not in _main_row(html, "quotes"), "the cell stays short"


def _sec_entry(**over) -> dict:
    """One ``sec_files.health_summary`` entry: stored rows, the file's figures and the last pull."""
    entry = {"present": 5, "members": 5, "floor": 3, "file_rows": 5,
             "last_modified": "Mon, 19 Oct 2026 20:30:00 GMT", "appended": 0, "absent": 0,
             "good_session": "2026-10-19", "session": "2026-10-19",
             "last_pull": "ok", "error": None}
    entry.update(over)
    return entry


@pytest.mark.parametrize("exchange, mf, said", [
    ({"last_pull": "unknown"}, {"last_pull": "unknown"}, "no pull recorded yet"),
    ({}, {}, "pulled 2026-10-19, unchanged"),
    ({"last_pull": "failed", "session": "2026-10-20", "error": "SEC 500"}, {},
     "pull FAILED 2026-10-20 (SEC 500)"),
])
def test_the_sec_files_pull_tri_state_reaches_the_table_verbatim(
        monkeypatch, rawstore_tmp_db, exchange, mf, said):
    """avo 2026-10-22: the note moved into the detail row; every sentence survives word for word."""
    from RevenueChaser.core import sec_files
    from RevenueChaser.module_settings import inventory
    summary = {"exchange": _sec_entry(**exchange), "mf": _sec_entry(**mf)}
    monkeypatch.setattr(sec_files, "health_summary", lambda marker_dir=None: summary)
    row = inventory._sec_files_row()
    html = report._table([row])
    assert said in row["note"], "the row builder still writes the sentence"
    assert said in html, "and the renderer shows it, not a reworded copy"

# ── G4 of the GUI brief (#124): the filters above the table ──────────────────────────────────
def _g4_payload():
    """Five datasets, one per thing the filters can say.

    ``universe`` (raw file, 90% coverage, a last data date) answers no state at all;
    ``corpus`` (edgar file) has no denominator; ``quotes`` (raw file) is only 20% covered and
    nothing else is wrong with it; ``cftc`` (raw file, 4% coverage) has no time column and a
    failed query; ``mystery`` is a dataset ``core.datasets`` does not know.
    """
    p = _payload()
    p["rows"] = [
        {"key": "universe", "label": "Equity universe",
         "tables": ["equity_universe", "equity_universe_changes"], "rows": 7403,
         "last_date": "2026-09-20", "holes": 93, "hole_label": "null market cap",
         "source": "yfinance",
         "coverage": {"covered": 812, "expected": 905, "fraction": 812 / 905,
                      "label": "812 / 905 (90%)", "newest": "2026-09-20", "problems": []},
         "fetch": {"route": "/run/universe-stats", "label": "Fetch caps"}, "note": ""},
        {"key": "corpus", "label": "10-K / 20-F corpus",
         "tables": ["chain_filings", "edgar_documents"], "rows": 3278,
         "last_date": "2026-09-18", "holes": 42, "hole_label": "no filing",
         "source": "sec",
         "coverage": {"covered": None, "expected": None, "fraction": None,
                      "label": "n/a", "newest": None, "problems": []},
         "fetch": {"route": "/run/edgar-sync", "label": "Fetch 10-K sync"}, "note": ""},
        {"key": "quotes", "label": "Quotes and market cap", "tables": ["quote_snapshot"],
         "rows": 180, "last_date": "2026-09-20", "holes": 0, "hole_label": "",
         "source": "yfinance",
         "coverage": {"covered": 181, "expected": 905, "fraction": 0.2,
                      "label": "181 / 905 (20%)", "newest": "2026-09-20", "problems": []},
         "fetch": {"route": "/run/screeners", "label": "Fetch screeners"}, "note": ""},
        {"key": "cftc", "label": "CFTC E-mini Commitments of Traders", "tables": ["cftc_cot"], "rows": 2,
         "last_date": "—", "holes": 0, "hole_label": "",
         "source": "finra/cftc",
         "coverage": {"covered": 2, "expected": 50, "fraction": 0.04,
                      "label": "2 / 50 (4%)", "newest": None, "problems": []},
         "fetch": {"route": "/run/earnings-inputs", "label": "Fetch inputs"}, "note": ""},
        {"key": "mystery", "label": "Unregistered dataset", "tables": ["mystery_table"],
         "rows": 1, "last_date": "—", "holes": 0, "hole_label": "",
         "fetch": {"route": "/run/mystery", "label": "Fetch"}, "note": ""},
    ]
    p["errors"] = [{"table": "cftc_cot", "where": "count", "error": "the query failed"}]
    return p


def _filter_bar(html: str) -> str:
    """The filter bar alone: everything between it and the table it filters."""
    return html[html.index('id="inv-filters"'):html.index('id="inv-table"')]


def _chip_texts(bar: str, kind: str) -> list:
    import re
    return re.findall(r'data-chips="%s"[^>]*>(.*?)</button>' % kind, bar, re.S)






def test_the_attention_line_is_seventy_percent_and_a_failed_query_also_counts():
    """The threshold from both sides: 69% is called out, 70% is not, and a failed query is a
    failure of its own even at full coverage."""
    def row(fraction, label="905 / 905 (100%)", tables=()):
        return {"coverage": {"fraction": fraction, "label": label}, "last_date": "2026-09-20",
                "tables": list(tables)}
    assert "needs attention" in report._states_of(row(0.69), [])
    assert "needs attention" in report._states_of(row(0.0), [])
    assert "needs attention" not in report._states_of(row(0.70), [])
    assert "needs attention" not in report._states_of(row(1.0), [])
    failed = [{"table": "quote_snapshot", "where": "count", "error": "the query failed"}]
    assert "needs attention" in report._states_of(row(1.0, tables=["quote_snapshot"]), failed)
    assert "needs attention" not in report._states_of(row(1.0, tables=["other"]), failed)
    assert "no denominator" in report._states_of(row(None, "n/a"), [])
    assert "no denominator" not in report._states_of(row("n/a", "error"), [])










def test_the_filters_run_under_node(tmp_path):
    """``settingsFilter`` against a DOM stub: the find box, the chips, the count line and the
    detail row that must collapse with its dataset. Nothing here is asserted from the source text."""
    import json as _json
    import shutil
    import subprocess
    import pytest
    from RevenueChaser.core.tests.board_js import function_source
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    js = (function_source(report.JS, "settingsRowPairs")
          + function_source(report.JS, "settingsFilter")
          + function_source(report.JS, "settingsFilterClear")
          + r"""
function el(attrs, cls){
  var o = {attrs: attrs || {}, hidden: false, className: cls || "", textContent: "",
           disabled: false, querySelector: function(){ return null; }};
  o.getAttribute = function(k){ return o.attrs[k] == null ? null : o.attrs[k]; };
  o.setAttribute = function(k, v){ o.attrs[k] = String(v); };
  return o;
}
function dataset(name, tables, db, source, state){
  var tr = el({"data-db": db, "data-source": source, "data-state": state}, "row");
  tr.button = el({"aria-expanded": "false"});
  tr.button.textContent = name;
  tr.querySelector = function(sel){ return sel === ".det-btn" ? tr.button : null; };
  tr.detail = el({}, "detail");
  tr.detail.hidden = true;
  tr.tbl = el({});
  tr.tbl.textContent = tables;
  tr.detail.querySelector = function(sel){ return sel === ".tbl" ? tr.tbl : null; };
  return tr;
}
var rows = [dataset("Equity universe", "equity_universe equity_universe_changes", "raw", "yfinance", ""),
            dataset("10-K / 20-F corpus", "chain_filings edgar_documents", "edgar", "sec", "no denominator"),
            dataset("CFTC E-mini Commitments of Traders", "cftc_cot", "raw", "finra/cftc",
                    "needs attention|no time column")];
var tbody = {children: []};
rows.forEach(function(tr){ tbody.children.push(tr, tr.detail); });
function chip(kind, value){
  var b = el({"aria-pressed": "false", "data-chips": kind, "data-value": value});
  return b;
}
var groups = {db: [chip("db", "raw"), chip("db", "edgar")],
              source: [chip("source", "yfinance"), chip("source", "sec"), chip("source", "finra/cftc")],
              state: [chip("state", "needs attention"), chip("state", "no time column"),
                      chip("state", "no denominator")]};
var box = el({}); box.value = "";
var count = el({}); var clear = el({}); clear.disabled = true;
globalThis.document = {
  getElementById: function(id){
    return id === "inv-find" ? box : id === "inv-count" ? count : id === "inv-clear" ? clear : null; },
  querySelector: function(sel){ return sel === "#inv-table tbody" ? tbody : null; },
  querySelectorAll: function(sel){
    if(sel === '#inv-filters button[aria-pressed="true"]'){
      var on = [];
      Object.keys(groups).forEach(function(k){ groups[k].forEach(function(b){
        if(b.attrs["aria-pressed"] === "true") on.push(b); }); });
      return on;
    }
    return [];
  }
};
function toggle(kind, value){
  groups[kind].forEach(function(b){
    if(b.attrs["data-value"] === value){
      b.attrs["aria-pressed"] = b.attrs["aria-pressed"] === "true" ? "false" : "true";
    }
  });
}
function reset(){
  Object.keys(groups).forEach(function(k){ groups[k].forEach(function(b){ b.attrs["aria-pressed"] = "false"; }); });
  box.value = "";
}
function visible(){ return rows.filter(function(tr){ return !tr.hidden; })
                                 .map(function(tr){ return tr.button.textContent; }); }
function state(){ return {shown: visible(), count: count.textContent, clear: !clear.disabled,
                         details: rows.map(function(tr){ return tr.detail.hidden; })}; }
var out = {};
settingsFilter();
out.none = state();
box.value = "COT";
settingsFilter();
out.byName = state();
box.value = "chain_filings";
settingsFilter();
out.byTable = state();
reset(); toggle("db", "raw"); toggle("source", "sec");
settingsFilter();
out.and = state();
reset(); toggle("db", "raw");
groups.db[1].attrs["aria-pressed"] = "true";       // two chips in one group: raw or edgar
settingsFilter();
out.or = state();
reset(); toggle("state", "needs attention");
settingsFilter();
out.stateChip = state();
reset(); toggle("state", "needs attention");
rows[2].button.setAttribute("aria-expanded", "true");
rows[2].detail.hidden = false;
box.value = "universe";
settingsFilter();
out.collapsed = {details: rows.map(function(tr){ return tr.detail.hidden; }),
                 expanded: rows[2].button.attrs["aria-expanded"]};
toggle("db", "raw");                               // a filter is on, then Clear filters is pressed
settingsFilterClear();
out.cleared = state();
console.log(JSON.stringify(out));
""")
    path = tmp_path / "settings_filters.js"
    path.write_text(js, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr[-2000:]
    got = _json.loads(out.stdout.strip().splitlines()[-1])

    all_three = ["Equity universe", "10-K / 20-F corpus", "CFTC E-mini Commitments of Traders"]
    assert got["none"] == {"shown": all_three, "count": "Showing 3 of 3 datasets",
                           "clear": False, "details": [True, True, True]}
    assert got["byName"]["shown"] == ["CFTC E-mini Commitments of Traders"], "the dataset name matches"
    assert got["byName"]["count"] == 'Showing 1 of 3 datasets · filtered by search "COT"'
    assert got["byName"]["clear"] is True
    assert got["byTable"]["shown"] == ["10-K / 20-F corpus"], "a store table name matches"
    assert got["and"]["shown"] == [], "raw AND sec is no row at all"
    assert got["and"]["count"] == ("Showing 0 of 3 datasets · filtered by store file raw · "
                                  "source sec")
    assert got["or"]["shown"] == all_three, "two chips in one group widen the result"
    assert got["stateChip"]["shown"] == ["CFTC E-mini Commitments of Traders"]
    assert got["stateChip"]["count"] == "Showing 1 of 3 datasets · filtered by state needs attention"
    assert got["collapsed"] == {"details": [True, True, True], "expanded": "false"}, \
        "a hidden dataset takes its detail row down and leaves it collapsed"
    assert got["cleared"] == {"shown": all_three, "count": "Showing 3 of 3 datasets",
                              "clear": False, "details": [True, True, True]}

# ── G5 of the GUI redesign (#124): the summary tiles above the table ────────────────────────────
def _tile_value(html: str, label: str) -> str:
    """The number the summary tile labelled ``label`` shows; fails when the tile is absent."""
    import re
    found = re.search(r'<div class="stile-n">([^<]*)</div><div class="stile-l" id="[^"]*">'
                      + re.escape(label) + "</div>", html)
    assert found, f"no summary tile labelled {label}"
    return found.group(1)


def _tile_row(key: str, **over) -> dict:
    """One row shaped the way ``inventory._collect`` builds it: healthy unless a field is replaced."""
    row = {"key": key, "label": key, "tables": [f"{key}_table"], "rows": 10,
           "last_date": "2026-09-18", "holes": None,
           "coverage": {"fraction": 1.0, "label": "10 / 10 (100%)"}}
    row.update(over)
    return row


def _tile_html(rows: list[dict], timing=None, errors=None) -> str:
    payload = {"generated_at": "2026-09-25T20:00:00+00:00", "rows": rows}
    if timing is not None:
        payload["timing"] = timing
    if errors is not None:
        payload["errors"] = errors
    return report.render_settings_dashboard(payload)













# ── S6 of the Data Health plan (#124): "Other store tables" ─────────────────────────────────
# Every MANIFEST table no dataset claims, with its row count and newest date only. Normally
# empty (S3 claims them all), so the section is the honest intermediate state and the backstop.
_OTHER_SENTENCE = ("These store tables are not claimed by any dataset yet, so they carry no "
                   "source and no coverage.")


def _other_section(html: str) -> str:
    start = html.index('id="other-tables"')
    return html[start:].split("</section>")[0]


def test_no_other_store_tables_section_when_nothing_is_unclaimed():
    for payload in (_payload(), dict(_payload(), other_tables=[])):
        html = report.render_settings_dashboard(payload)
        assert "Other store tables" not in html, "an empty list renders no heading"
        assert "not claimed by any dataset" not in html






def test_every_registry_source_is_spelled_out_in_words():
    """avo's copy rule: no 'uw' or 'sec' shorthand on the board. Every provider value the registry can carry has
    plain words, and none of them is the raw short form."""
    from RevenueChaser.core import datasets
    for source in datasets.Source:
        words = report.source_words(source.value)
        assert source.value in report.SOURCE_WORDS, source.value
        assert words != source.value
        assert len(words) > 3
    assert report.source_words("something-new") == "something-new"


# ── G11 of the GUI brief (#124): what is missing, and how it improves ───────────────────────
def _detail_text(html: str, key: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _detail_row(html, key))).strip()


def test_what_is_missing_is_computed_from_the_coverage_counts():
    """The number is measured, never written in the registry: expected minus covered."""
    html = _cov_table({"covered": 812, "expected": 905, "fraction": 812 / 905,
                       "label": "812 / 905 (90%)", "newest": None, "problems": []},
                      gap_kind="needs_decision", next_step="Decide the population.")
    detail = _detail_text(html, "quotes")
    assert "What is missing" in detail
    assert "93 of 905 names have no row" in detail


def test_what_is_missing_at_full_coverage_is_zero_of_the_population():
    html = _cov_table({"covered": 905, "expected": 905, "fraction": 1.0,
                       "label": "905 / 905 (100%)", "newest": None, "problems": []},
                      gap_kind="none")
    detail = _detail_text(html, "quotes")
    assert "0 of 905 names have no row" in detail


def test_what_is_missing_says_no_population_is_defined_when_expected_is_none():
    html = _cov_table({"covered": 6084, "expected": None, "fraction": None,
                       "label": "n/a", "newest": None, "problems": []},
                      gap_kind="fixable", next_step="Join the universe count.")
    detail = _detail_text(html, "quotes")
    assert "no population is defined" in detail
    assert "names have no row" not in detail


def test_what_is_missing_says_the_coverage_query_failed():
    html = _cov_table({"label": "error", "fraction": None, "covered": None, "expected": None,
                       "newest": None, "problems": ["the summary could not read the store"]},
                      gap_kind="fixable", next_step="Fix the summary.")
    detail = _detail_text(html, "quotes")
    assert "the coverage query failed" in detail
    assert "no population is defined" not in detail


def test_a_retired_row_says_nothing_is_missing_by_design():
    html = _cov_table({"label": "n/a", "fraction": None, "covered": None, "expected": None,
                       "newest": None, "problems": []}, retired=True)
    detail = _detail_text(html, "quotes")
    assert "nothing, by design" in detail


def test_how_it_improves_speaks_the_kind_and_carries_the_next_step():
    html = _cov_table({"covered": 812, "expected": 905, "fraction": 812 / 905,
                       "label": "812 / 905 (90%)", "newest": None, "problems": []},
                      gap_kind="fixable", next_step="Re-run the join after the next pull.")
    detail = _detail_text(html, "quotes")
    assert "How it improves" in detail
    assert "Can be fixed now. Re-run the join after the next pull." in detail


def test_a_ceiling_states_its_reason_and_never_a_schedule():
    reason = "OpenFIGI maps from a CUSIP, so a name with no CUSIP cannot get one on this route."
    html = _cov_table({"label": "n/a", "fraction": None, "covered": None, "expected": None,
                       "newest": None, "problems": []},
                      gap_kind="ceiling", next_step=reason)
    detail = _detail_text(html, "quotes")
    assert f"Cannot be improved: {reason}" in detail


def test_a_row_with_no_gap_kind_draws_no_chip_and_no_complete_claim():
    """No kind means nothing is written down, not that the dataset is complete."""
    html = _cov_table({"label": "n/a", "fraction": None, "covered": None, "expected": None,
                       "newest": None, "problems": []})
    assert 'class="gap ' not in _main_row(html, "quotes")
    detail = _detail_text(html, "quotes")
    assert "How it improves" in detail and "Complete" not in detail


@pytest.mark.parametrize("kind, said", [
    ("fixable", "Can be fixed now"),
    ("in_build", "Being built"),
    ("needs_decision", "Waiting for a decision from you"),
    ("self_heals", "Fills in by itself over time"),
    ("none", "Complete"),
])
def test_the_name_cell_carries_a_tag_chip_with_the_same_words(kind, said):
    html = _cov_table({"fraction": None, "label": "n/a"}, gap_kind=kind)
    main = _main_row(html, "quotes")
    assert f'<span class="gap gap-{kind}">{said}</span>' in main
    assert _cells(main)[0] == f"Quotes {said}", "the chip's words sit beside the name"


def test_a_ceiling_chip_stays_short_while_the_detail_carries_the_reason():
    reason = "The source maps from a CUSIP, so a name with no CUSIP cannot get one."
    html = _cov_table({"fraction": None, "label": "n/a"}, gap_kind="ceiling", next_step=reason)
    main = _main_row(html, "quotes")
    assert '<span class="gap gap-ceiling">Cannot be improved</span>' in main
    assert reason in _detail_text(html, "quotes")


def test_a_hostile_next_step_is_escaped_and_a_hostile_kind_never_sets_the_class():
    bad = "<img src=x onerror=__x(1)>"
    html = _cov_table({"fraction": None, "label": "n/a"}, gap_kind="fixable", next_step=bad)
    assert "<img" not in html
    assert "&lt;img src=x onerror=__x(1)&gt;" in html
    # A kind outside the six renders no chip and no claim: its class can never come from data.
    html = _cov_table({"fraction": None, "label": "n/a"}, gap_kind=bad, next_step=bad)
    assert "<img" not in html
    assert "gap-" + bad not in html
    assert 'class="gap ' not in _main_row(html, "quotes")


def test_the_cftc_row_says_fresh_but_shallow_and_names_119_without_calling_it_a_failure():
    """Read through the real registry and ``_attach_registry``, the way the board builds it."""
    from RevenueChaser.module_settings import inventory
    row = {"key": "cftc", "label": "CFTC E-mini Commitments of Traders",
           "tables": ["cftc_cot"], "rows": 2, "last_date": "2026-10-13", "holes": 0,
           "hole_label": "", "fetch": None, "note": ""}
    inventory._begin_call()
    try:
        inventory._attach_registry([row])
    finally:
        inventory._end_call()
    main = _main_row(report._table([row]), "cftc")
    assert "Waiting for a decision from you" in main, "the chip carries the kind in words"
    detail = _detail_text(report._table([row]), "cftc")
    assert "#119" in detail
    assert "fresh but shallow" in detail
    assert "failure" not in detail.lower() and "failed" not in detail.lower()
# ── G6b of the GUI redesign (#124): the Data Flow diagram ─────────────────────────────────────
# Provider → job → store file, built from the rows the table already collected. Pure: no store
# read, no second pass over the registry, so the diagram costs page weight and never query time.
def _flow_rows():
    return [
        {"key": "quotes", "label": "Quotes", "source": "uw", "writer": "RC-Daily-Bars", "db": "raw"},
        {"key": "bars", "label": "Daily bars", "source": "uw", "writer": "RC-Daily-Bars", "db": "prices"},
        {"key": "bars_again", "label": "Daily bars, second table", "source": "uw",
         "writer": "RC-Daily-Bars", "db": "prices"},
        {"key": "filings", "label": "10-K / 20-F corpus", "source": "sec",
         "writer": "RC-ValueChain-Sync", "db": "edgar"},
        {"key": "pins", "label": "LEAPS pins", "source": "uw", "writer": None,
         "writer_note": "the user enters it", "db": "raw"},
    ]


def _flow_section(rows) -> str:
    """The page's own section, through the renderer's function."""
    return report._flow_section(rows)


def test_the_flow_graph_de_duplicates_nodes_and_counts_the_datasets_per_edge():
    graph = report.flow_graph(_flow_rows())
    assert graph["providers"] == [{"name": "SEC EDGAR filings", "count": 1},
                                  {"name": "Unusual Whales", "count": 4}]
    assert graph["jobs"] == [{"name": "RC-Daily-Bars", "count": 3},
                             {"name": "RC-ValueChain-Sync", "count": 1},
                             {"name": "started by hand from the dashboard", "count": 1}]
    assert graph["stores"] == [{"name": "raw", "count": 2}, {"name": "edgar", "count": 1},
                               {"name": "prices", "count": 2}]
    edges = {(e["provider"], e["job"], e["store"]): e["count"] for e in graph["edges"]}
    assert edges == {
        ("Unusual Whales", "RC-Daily-Bars", "raw"): 1,
        ("Unusual Whales", "RC-Daily-Bars", "prices"): 2,
        ("SEC EDGAR filings", "RC-ValueChain-Sync", "edgar"): 1,
        ("Unusual Whales", "started by hand from the dashboard", "raw"): 1,
    }
    assert len(graph["edges"]) == 4, "five datasets, four edges: two share one"


def test_a_dataset_without_a_writer_joins_the_started_by_hand_node():
    graph = report.flow_graph([{"key": "x", "source": "sec", "writer": None, "db": "raw"}])
    assert [j["name"] for j in graph["jobs"]] == ["started by hand from the dashboard"]
    assert graph["providers"] == [{"name": "SEC EDGAR filings", "count": 1}]


@pytest.mark.parametrize("row,words", [
    # the registry does not know it: no source key, or an empty one
    ({"key": "x", "db": "raw"}, "unknown provider"),
    ({"key": "x", "source": "", "db": "raw"}, "unknown provider"),
    # the registry's own "no single provider" value keeps the words the dataset table uses for it
    ({"key": "x", "source": "unknown", "db": "raw"}, "more than one provider, or not known"),
])
def test_a_dataset_without_a_provider_says_unknown_provider_in_words(row, words):
    graph = report.flow_graph([row])
    assert [p["name"] for p in graph["providers"]] == [words]


def test_a_row_without_a_store_file_takes_the_registry_file_without_reading_the_store():
    from RevenueChaser.core import datasets
    spec = next(s for s in datasets.REGISTRY if s.db == "history")
    graph = report.flow_graph([{"key": spec.key, "source": spec.source.value, "writer": spec.writer}])
    assert [s["name"] for s in graph["stores"]] == ["history"]






def test_clicking_a_job_highlights_its_edges_and_clicking_again_clears_them(tmp_path):
    """The real script, run under node against the rendered diagram read back as a small DOM."""
    import json
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    svg = report._flow_svg(report.flow_graph(_flow_rows()))
    nodes = re.findall(r'data-node="([^"]+)"', svg)
    edges = re.findall(r'data-a="([^"]*)" data-b="([^"]*)"', svg)
    assert nodes and edges
    harness = """
var NODES = %s, EDGES = %s;
function el(attrs){
  var cls = {};
  return {cls: cls,
    getAttribute: function(k){ return k in attrs ? attrs[k] : null; },
    setAttribute: function(k, v){ attrs[k] = v; },
    classList: {toggle: function(n, on){ if(on){ cls[n] = 1; } else { delete cls[n]; } }}};
}
var nodeEls = NODES.map(function(id){
  var attrs = {'data-node': id};
  if(id.indexOf('job:') === 0){ attrs['data-job'] = id.slice(4); attrs['aria-pressed'] = 'false'; }
  if(id.indexOf('provider:') === 0){ attrs['data-provider'] = id.slice(9); attrs['aria-pressed'] = 'false'; }
  return el(attrs);
});
var edgeEls = EDGES.map(function(p){ return el({'data-a': p[0], 'data-b': p[1]}); });
var handlers = {};
var svg = {querySelectorAll: function(sel){
    return sel === '.flow-edge' ? edgeEls : sel === '[data-node]' ? nodeEls : []; },
  addEventListener: function(t, f){ handlers[t] = f; }};
globalThis.document = {getElementById: function(id){ return id === 'flow-svg' ? svg : null; }};
""" % (json.dumps(nodes), json.dumps(edges))
    tail = """
function state(){
  var dim = [], on = [];
  edgeEls.forEach(function(e, i){ if(e.cls['flow-dim']){ dim.push(i); } if(e.cls['flow-on']){ on.push(i); } });
  return {on: on, dim: dim,
    dimNodes: nodeEls.filter(function(n){ return n.cls['flow-dim']; }).length};
}
function press(id){
  var node = nodeEls.filter(function(n){ return n.getAttribute('data-node') === id; })[0];
  handlers.click({target: {closest: function(){ return node; }}});
}
var out = {};
press('store:raw'); out.store = state();
out.storePressed = nodeEls.filter(function(n){ return n.getAttribute('aria-pressed') === 'true'; }).length;
press('job:RC-Daily-Bars'); out.job = state();
out.jobPressed = nodeEls.filter(function(n){ return n.getAttribute('aria-pressed') === 'true'; })
  .map(function(n){ return n.getAttribute('data-node'); });
press('job:RC-Daily-Bars'); out.cleared = state();
out.clearedPressed = nodeEls.filter(function(n){ return n.getAttribute('aria-pressed') === 'true'; }).length;
press('provider:Unusual Whales'); out.provider = state();
out.providerPressed = nodeEls.filter(function(n){ return n.getAttribute('aria-pressed') === 'true'; })
  .map(function(n){ return n.getAttribute('data-node'); });
console.log(JSON.stringify(out));
"""
    path = tmp_path / "flow.js"
    path.write_text(harness + report.FLOW_JS + tail, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    prices = next(i for i, (a, b) in enumerate(edges)
                  if (a, b) == ("job:RC-Daily-Bars", "store:prices"))
    filings = next(i for i, (a, b) in enumerate(edges)
                   if (a, b) == ("job:RC-ValueChain-Sync", "store:edgar"))
    assert got["store"]["dim"] == [] and got["store"]["dimNodes"] == 0, "a store file is not a button"
    assert got["job"]["on"], "the job's own lines light up"
    assert prices in got["job"]["on"] and filings not in got["job"]["on"]
    assert filings in got["job"]["dim"]
    assert len(got["job"]["dim"]) == len(edges) - len(got["job"]["on"])
    assert got["job"]["dimNodes"] == len(nodes) - 4, (
        "the job, the provider that feeds it and its two store files stay lit")
    assert got["cleared"]["on"] == [] and got["cleared"]["dim"] == [] and got["cleared"]["dimNodes"] == 0
    assert got["provider"]["dimNodes"] == len(nodes) - 3, (
        "a provider's own lines light up, and the jobs at their far end stay lit")
    assert got["storePressed"] == 0, "a store file is not a toggle"
    assert got["jobPressed"] == ["job:RC-Daily-Bars"], "the pressed job says so to a screen reader"
    assert got["clearedPressed"] == 0, "pressing it again clears the state"
    assert got["providerPressed"] == ["provider:Unusual Whales"]


def test_a_hostile_provider_or_job_name_is_escaped_in_text_and_in_an_attribute():
    bad = """<img src=x onerror=__x(1)>"&'"""
    section = report._flow_section([{"key": "x", "source": bad, "writer": bad, "db": "raw"}])
    assert "<img" not in section
    assert "&lt;img src=x onerror=__x(1)&gt;" in section
    assert 'data-provider="' in section and 'data-job="' in section
    assert bad not in section, "never raw, in text or inside a quoted attribute"


def test_a_name_too_long_for_its_column_is_cut_and_its_hover_title_keeps_all_of_it():
    """A layout guard, not a wording rule: the real providers and jobs all fit, so only a value
    that could not fit is cut, and the node's own title still carries every word of it."""
    long_name = "RC-" + "VeryLongTaskName" * 4
    section = _flow_section([{"key": "x", "source": "sec", "writer": long_name, "db": "raw"}])
    shown = long_name[:report.FLOW_TEXT_MAX - 1] + "…"
    assert f">{shown}</text>" in section
    assert f">{long_name}</text>" not in section, "the column cannot hold it"
    assert f"<title>{long_name} — 1 dataset</title>" in section, "the whole name is still there"


def test_each_edge_is_coloured_by_the_store_file_it_lands_in():
    svg = report._flow_svg(report.flow_graph(_flow_rows()))
    for colour in ("#4f8cff", "#e06088", "#e0a030"):          # raw, prices, edgar
        assert f'stroke="{colour}"' in svg
    assert 'stroke="#3cb89a"' not in svg, "no dataset lands in history here"


def test_two_flows_sharing_one_pair_draw_side_by_side_so_neither_colour_hides_the_other():
    """Two datasets from one provider, written by one job, landing in different store files take the
    same provider-to-job box pair. Drawn on one exact line, the second file's colour would cover the
    first and the picture would silently lose a flow."""
    graph = report.flow_graph([{"key": "a", "source": "sec", "writer": "W", "db": "raw"},
                               {"key": "b", "source": "sec", "writer": "W", "db": "edgar"}])
    svg = report._flow_svg(graph)
    shared = [tag for tag in re.findall(r'<path class="flow-edge"[^>]*>', svg)
              if 'data-a="provider:SEC EDGAR filings"' in tag and 'data-b="job:W"' in tag]
    assert len(shared) == 2
    assert len({re.search(r' d="([^"]+)"', tag).group(1) for tag in shared}) == 2, (
        "the two lines are the same line, so one store file's colour is hidden")
    assert any('stroke="#4f8cff"' in tag for tag in shared), "raw's line is still its own"
    assert any('stroke="#e0a030"' in tag for tag in shared), "edgar's line is still its own"


def test_a_store_file_outside_the_four_files_draws_in_muted_ink_and_says_so():
    svg = report._flow_svg(report.flow_graph([{"key": "x", "source": "sec", "writer": "W"}]))
    assert report.FLOW_NO_STORE in svg
    assert 'stroke="#8a93a6"' in svg
    section = _flow_section([{"key": "x", "source": "sec", "writer": "W"}])
    assert "names no store file" in section, "the legend explains the grey line"
    assert len(report.DB_PILLS) == 4 and "outside the four" in section, "the legend counts the files it lists"


def test_the_flow_legend_names_the_four_store_files():
    section = _flow_section(_flow_rows())
    legend = section[section.index('id="flow-legend"'):]
    for db in ("raw", "edgar", "history", "prices"):
        assert f'id="flow-store-{db}"' in legend, db
        assert f"{db}.duckdb" in legend, db
    assert "Line colour is the store file" in legend




def test_every_job_node_is_a_button_and_the_store_files_are_not():
    svg = report._flow_svg(report.flow_graph(_flow_rows()))
    jobs = re.findall(r'<g class="flow-node" tabindex="0" role="button" data-job="([^"]+)"', svg)
    assert jobs == ["RC-Daily-Bars", "RC-ValueChain-Sync", "started by hand from the dashboard"]
    providers = re.findall(r'<g class="flow-node" tabindex="0" role="button" data-provider="([^"]+)"', svg)
    assert providers == ["SEC EDGAR filings", "Unusual Whales"]
    assert 'data-provider="raw"' not in svg and 'data-job="raw"' not in svg


def test_the_diagram_collapses_past_the_page_weight_budget(monkeypatch):
    monkeypatch.setattr(report, "FLOW_MAX_BYTES", 10)
    section = _flow_section(_flow_rows())
    assert '<details class="flow">' in section and " open>" not in section


def test_the_real_registry_diagram_stays_under_the_budget():
    """The ~60 datasets of the real registry, built from the registry alone (no store read)."""
    from RevenueChaser.core import datasets
    rows = [{"key": s.key, "source": s.source.value, "writer": s.writer, "db": s.db}
            for s in datasets.REGISTRY]
    svg = report._flow_svg(report.flow_graph(rows))
    assert len(svg.encode("utf-8")) < report.FLOW_MAX_BYTES, f"{len(svg)} bytes"
    assert '<details class="flow" open>' in _flow_section(rows)
# ── S7 of the Data Health plan (#124): "Identity coverage" ───────────────────────────────────
# Not on the page since PR 5 (D9): the spine's identity funnel replaced it
# (module_settings/tests/test_newdata_spine.py). The payload still carries the section until the old inventory goes.
def test_the_hand_built_identity_coverage_card_is_not_rendered_any_more():
    cov = {"names_kept": 1, "funnel": [], "funnel_note": "x", "inputs": [], "identifiers": []}
    html = report.render_settings_dashboard(dict(_payload(), identity_coverage=cov))
    assert 'id="identity-coverage"' not in html and "Identity coverage" not in html


# ── G7 of the GUI brief (#124): the drill below the table, and which boards can score the name ──
def _drill_section(html: str) -> str:
    """The drill's own block: its heading and search box, up to the closed detail panel."""
    return html[html.index('id="ticker-drill"'):html.index('id="drill"')]


def _panel(html: str, ident: str) -> str:
    start = html.index(f'id="{ident}"')
    nxt = html.find('role="tabpanel"', html.index(">", start))
    return html[start:nxt if nxt > 0 else len(html)]


def test_the_health_panel_holds_its_parts_in_order():
    """#492 (i), reordered by #527 step 9, B4 and #715: the head, the link to the live parts, the backups and the catalog, then
    the request log and the routes folded."""
    from RevenueChaser.module_settings.tests.test_control_view import control_nd
    html = report.render_settings_dashboard(dict(_payload(), newdata=control_nd()))
    assert "<title>Data/Health</title>" in html
    health = _panel(html, "panel-health")
    order = ['id="live-frame"', "<h1>Data/Health · Fetch/Health</h1>", 'id="sec-health"', 'class="fgrps"',
             "<h1>Needs, boards and automations</h1>", "What am I looking at?",
             'id="needs-catalog"', 'id="needs-log"', 'id="needs-routes"']
    at = [health.index(x) for x in order]
    assert at == sorted(at), order
    # avo, 11 Nov 2026: the Ticker lookup is on the Database tab now, not under the Grafana frame.
    data = _panel(html, "panel-data")
    assert 'id="drill"' in data and 'id="ticker-combo"' in data
    assert 'id="ticker-drill"' not in health and 'id="backups"' not in data
    assert 'id="ctl-fetch"' not in html, "the Fetch panel is no longer drawn here"


def test_the_old_settings_fold_and_its_sections_are_gone():
    html = report.render_settings_dashboard(dict(_payload(), automations=[{"key": "x"}]))
    for gone in ('id="old-settings"', "oldfold", 'id="inv-table"', 'id="identity"', 'id="other-tables"',
                 'id="data-flow"', "Old Settings sections"):
        assert gone not in html, gone
    assert not hasattr(report, "OLD_FOLD_CSS")


def test_the_tabs_map_data_and_health_and_old_choices_land_on_them():
    assert report.SETTINGS_TABS["tabs"] == {"data": "panel-data", "health": "panel-health"}
    assert report.SETTINGS_TABS["aliases"] == {"fetch": "data", "needs": "health", "live": "health"}
    assert "CFG.aliases[name]" in report.TABS_JS


def test_the_lookup_keeps_its_own_search_box():
    block = _drill_section(report.render_settings_dashboard(_payload()))
    assert 'id="ticker-combo"' in block and 'id="ticker-combo-list"' in block, \
        "the lookup keeps its own search box"
    assert "Ticker lookup" in block


def test_the_four_parts_that_live_nowhere_else_sit_in_health_above_backups():
    """avo, 6 Nov 2026 ("A"): the board settings, Automations, the watch lists and the IBKR cards move to Health."""
    from RevenueChaser.module_settings.tests.test_control_view import control_nd
    html = report.render_settings_dashboard(dict(_payload(), automations=_autos(), newdata=control_nd(),
                                                 ibkr_login_url="https://gateway.example/",
                                                 errors=[{"table": "ibkr", "where": "x", "error": "read failed"}]))
    health = _panel(html, "panel-health")
    # #527 step 9: the reads that failed and the backups lead; the login link is in the top bar (Data tab's copy)
    marks = ['id="errors"', 'id="auto-body"', 'id="nd-watch-option"', 'id="nd-watch-stock"']
    at = [health.index(m) for m in marks]
    assert at == sorted(at), marks
    assert html.count('<details class="help"><summary>What am I looking at?</summary>') == 1
    assert "function settingsSave" in html and "function autoInit" in html and "function ndWatchRemove" in html
