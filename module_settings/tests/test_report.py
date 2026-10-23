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


def test_renders_the_inventory_table_headers_and_rows():
    """S5 (#124): the table leads with Source / Coverage / Fresh; the store tables, writer,
    action and notes moved into a detail row (asserted in the S5 tests below)."""
    html = report.render_settings_dashboard(_payload())
    for header in ("Dataset", "Source", "Coverage", "Freshness", "Last data date", "Rows", "Holes"):
        assert header in html
    assert "Store table(s)" in html and "Action" in html and "Notes" in html
    assert "Equity universe" in html
    assert "equity_universe" in html
    assert "2026-09-18" in html


def test_renders_the_fetch_buttons_including_alt():
    html = report.render_settings_dashboard(_payload())
    assert 'data-fetch="/run/universe-stats"' in html
    assert 'data-row="universe"' in html
    assert 'data-fetch="/run/universe"' in html
    assert 'data-row="universe-alt"' in html
    assert 'id="msg-universe-alt"' in html
    assert 'data-fetch="/run/edgar-sync"' in html
    assert 'data-row="corpus"' in html
    assert 'class="fetch-btn"' in html


def test_has_help_block_and_a_friendly_last_sync():
    html = report.render_settings_dashboard(_payload())
    assert 'class="help"' in html
    assert "<summary>" in html
    assert "Refresh" in html and "Fetch" in html
    assert "Last sync:" in html
    assert "2026-09-25T20:00:00" not in html


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


def test_empty_payload_renders_the_empty_state():
    html = report.render_settings_dashboard({"rows": []})
    assert "empty" in html.lower()
    assert "store is empty" in html.lower()
    assert "<table" not in html


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
        "price_basis": "dividend-adjusted closes (yfinance auto_adjust); correct for returns",
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


def test_board_inputs_card_renders_funnel_and_fetch_buttons():
    html = _with(boards=[_card()])
    assert "Board inputs" in html
    assert "Co-movement" in html
    assert "as of Friday, September 25, 2026 EOD" in html
    assert "equity_universe" in html and "swing_ohlc" in html
    assert "dividend-adjusted closes" in html
    assert "Window 252 sessions" in html
    assert "−30" in html and "−10" in html
    assert "(by design)" in html
    # only the fixable drop gets a button, wired to the shared poller
    assert 'data-fetch="/run/universe-stats" data-row="module_comovement-no_market_cap"' in html
    assert 'id="msg-module_comovement-no_market_cap"' in html
    assert html.count("(by design)") == 1
    assert "Clustered" in html and "60" in html


def test_board_cards_are_collapsed_by_default():
    """S9: a board card is a closed <details>, not an always-open panel."""
    html = _with(boards=[_card(settings=_settings_block())])
    m = re.search(r'<div class="bcard" id="board-module_comovement">'
                  r'<details(?![^>]*\bopen\b)>', html)
    assert m, "board card is not collapsed by default"
    assert "<summary>" in html
    # The Co-movement e2e parser keys on this div and the frow2 rows inside it.
    parsed = re.search(
        r'<div class="bcard" id="board-module_comovement">(.*?)</div></div>'
        r'(?=<div class="bcard"|\s*</div>|\s*<script)', html, re.S)
    assert parsed and 'class="frow2' in parsed.group(1)


def test_board_card_states():
    nr = _with(boards=[_card(state="not_rendered", funnel_steps=[], as_of=None)])
    assert ("Co-movement has not been rendered yet. "
            "Open or Refresh that tab first.") in nr
    nf = _with(boards=[_card(state="no_funnel", funnel_steps=[])])
    assert ("Rendered before funnel reporting; Refresh the Co-movement tab.") in nf
    bad = _with(boards=[_card(state="invalid", error="start 1 <script>x</script>")])
    assert 'class="board-err"' in bad
    assert "<script>x</script>" not in bad and "&lt;script&gt;" in bad
    err = _with(boards=[_card(state="error", error="JSONDecodeError: bad", funnel_steps=[])])
    assert "JSONDecodeError: bad" in err and 'class="board-err"' in err


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
    assert html.count("OIPD chain") == 2
    assert html.count('class="db db-raw"') == 2


def test_every_provider_the_registry_can_name_is_written_in_words():
    """The copy rule: a source code such as ``finra/cftc`` never reaches the page."""
    from RevenueChaser.core import datasets

    for source in datasets.Source:
        words = report._provider_name(source)
        assert words and words != source.value and words[0].isupper(), (source, words)


def test_a_board_card_draws_the_datasets_coverage_bar_with_its_number():
    html = _with(boards=[_card(tables=["price_bars_daily"])],
                 rows=[{"key": "price_bars",
                        "coverage": {"fraction": 0.9, "label": "812 / 905 (90%)"}}])
    assert 'class="covbar st-warn"' in html
    assert "812 / 905 (90%)" in html, "the bar always carries its number"


def test_a_coverage_with_no_population_draws_no_bar_on_the_board_card():
    html = _with(boards=[_card(tables=["price_bars_daily"])],
                 rows=[{"key": "price_bars", "coverage": {"fraction": None, "label": "n/a"}}])
    flow = re.search(r'<li class="bflow-row">.*?price_bars_daily.*?</li>', html, re.S)
    assert flow, "the table has no source to store line"
    assert 'class="covbar' not in flow.group(0), "a percentage of nothing is not 0%"
    assert "n/a" in flow.group(0)


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


def test_the_source_to_store_lines_sit_between_the_header_and_the_funnel():
    html = _with(boards=[_card()])
    assert html.index('class="bflow-row"') < html.index('class="funnel2"')


def test_rendering_the_board_cards_asks_the_store_nothing(monkeypatch):
    """The Refresh budget (#124): the cards are built from the payload, never from a new read."""
    from RevenueChaser.core import rawstore

    def boom(*args, **kwargs):
        raise AssertionError("a render must not query the store")

    for name in ("query", "get", "get_all", "count"):
        monkeypatch.setattr(rawstore, name, boom)
    html = _with(boards=[_card(tables=["price_bars_daily"])],
                 rows=[{"key": "price_bars",
                        "coverage": {"fraction": 1.0, "label": "905 / 905 (100%)"}}])
    assert "Daily price bars" in html and "905 / 905 (100%)" in html


def test_errors_are_shown_on_the_affected_row_and_escaped():
    html = _with(errors=[{"table": "equity_universe", "where": "_count",
                          "error": "disk <b>on</b> fire"}])
    assert 'class="row-err"' in html
    assert "disk &lt;b&gt;on&lt;/b&gt; fire" in html
    assert "disk <b>on</b> fire" not in html
    assert 'id="errors"' in html


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


class TestEditableInputs:
    def test_inputs_render_with_ranges_and_the_fine_tuning_details(self):
        html = _with(boards=[_card(settings=_settings_block())])
        assert 'data-setting="MIN_OBS"' in html
        assert 'data-setting="OOS_HOLDOUT"' in html
        assert '<select' in html and 'data-setting="WINDOW"' in html
        assert "60–WINDOW" in html and "42–126" in html
        # fine tuning is a collapsed <details>, closed by default, holding 3 inputs
        m = re.search(r'<details class="fine"(?![^>]*\bopen\b)[^>]*>(.*?)</details>',
                      html, re.S)
        assert m, "no collapsed Fine tuning <details>"
        assert "Fine tuning" in m.group(1)
        for key in ("WINSOR_K", "MIN_PERIODS", "BENCH_STALE_SESSIONS"):
            assert f'data-setting="{key}"' in m.group(1)
        assert 'data-setting="MIN_OBS"' not in m.group(1)
        # the derived value is read-only text, not an input
        assert "Held-out observations needed (derived)" in html
        assert 'data-setting="OOS_MIN_OBS"' not in html

    def test_error_area_and_per_field_error_slots_exist(self):
        html = _with(boards=[_card(settings=_settings_block())])
        assert 'id="seterr-module_comovement"' in html
        assert 'role="alert"' in html
        for key in ("WINDOW", "MIN_OBS", "OOS_HOLDOUT", "WINSOR_K",
                    "MIN_PERIODS", "BENCH_STALE_SESSIONS"):
            assert f'id="err-{key}"' in html

    def test_overridden_fields_are_marked_and_the_badge_shows_only_when_custom(self):
        plain = _with(boards=[_card(settings=_settings_block())])
        assert "Custom settings" not in plain
        assert "Reset to defaults" in plain       # the button is always there
        assert 'class="ovr"' not in plain
        s = _settings_block(custom=True)
        s["inputs"][1] = _field("MIN_OBS", "Minimum observations", 200, 126, lo=60,
                                rng="60–WINDOW", overridden=True)
        html = _with(boards=[_card(settings=s)])
        assert "Custom settings" in html
        assert html.count('class="ovr"') == 1
        assert "default 126" in html
        assert 'value="200"' in html

    def test_everything_data_derived_is_escaped(self):
        s = _settings_block()
        s["inputs"][1] = _field("MIN_OBS", '<img src=x onerror=alert(1)>', 126, 126,
                                lo=60, rng='<script>alert(2)</script>')
        html = _with(boards=[_card(settings=s)])
        assert "<img src=x" not in html and "<script>alert(2)" not in html
        assert "&lt;img src=x" in html

    def test_a_board_without_settings_renders_no_form(self):
        html = _with(boards=[_card()])
        assert "data-setting=" not in html and "Save settings" not in html


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


def test_the_card_renders_a_note_row_with_an_identity_anchor_and_no_button():
    html = _with(boards=[_typed_card()])
    row = re.search(r'<div class="frow2 note">.*?</div>', html, re.S).group(0)
    assert row == ('<div class="frow2 note"><span>⚠ Unknown security type — kept '
                   '<a href="#identity">Settings → Identity</a></span>'
                   '<span class="num">7</span></div>')
    assert "<button" not in row and "−7" not in html       # a note: no action, positive count
    assert "−12" in html and "Funds / ETFs" in html        # the type row is a normal by-design drop
    assert html.count("(by design)") == 1


def test_a_note_link_elsewhere_opens_that_tab_and_no_link_renders_none():
    card = _typed_card()
    card["funnel_steps"][2]["link"] = {"tab": "module_value_chain", "label": "VC"}
    row = re.search(r'<div class="frow2 note">.*?</div>', _with(boards=[card]), re.S).group(0)
    assert '<a href="/#module_value_chain" target="_top">VC</a>' in row
    del card["funnel_steps"][2]["link"]
    row = re.search(r'<div class="frow2 note">.*?</div>', _with(boards=[card]), re.S).group(0)
    assert "<a " not in row and "⚠ Unknown security type — kept</span>" in row


def test_the_note_label_is_escaped_on_the_card():
    html = _with(boards=[_typed_card(note_label="<i>x</i> & y")])
    assert "&lt;i&gt;x&lt;/i&gt; &amp; y" in html and "<i>x</i>" not in html


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


def test_the_identity_row_gets_a_fetch_identity_button_on_the_shared_poller():
    html = _with(rows=_payload()["rows"] + [_identity_row()])
    assert 'data-fetch="/run/identity" data-row="identity"' in html
    assert ">Fetch identity</button>" in html and 'id="msg-identity"' in html
    assert "Identity spine" in html and "unclassified ≥$300M" in html


def test_the_card_renders_every_section_with_its_id():
    html = _card_html()
    for ident in IDENTITY_IDS:
        assert f'id="{ident}"' in html, ident
    assert '<div class="bcard" id="identity-health"><h3>Identity health</h3>' in html


def test_the_card_sits_directly_below_the_inventory_table_and_above_the_errors_and_boards():
    html = _with(rows=_payload()["rows"], identity=_identity(), boards=[_card()],
                 errors=[{"table": "equity_universe", "where": "_count", "error": "x"}])
    assert (html.index('class="panel"') < html.index('id="identity-health"')
            < html.index('id="errors"') < html.index("Board inputs"))


def test_a_payload_without_identity_renders_no_card():
    assert "identity-health" not in report.render_settings_dashboard(_payload())


def test_the_last_run_line_shows_status_dates_counts_and_each_selected_source():
    html = _card_html()
    run = html[html.index('id="identity-run"'):html.index('id="identity-spdr"')]
    assert "partial" in run and "2026-09-30 00:00 UTC" in run and "2026-09-30 00:06 UTC" in run
    assert "7,366 tickers" in run and "266 conflicts" in run
    assert "sec_exchange" in run and "10,300 rows" in run
    assert "openfigi" in run and "120 fetched" in run
    assert "nasdaq" in run and "error" in run and "HTTP 503 &lt;b&gt;down&lt;/b&gt;" in run
    assert "fdic" not in run, "a source that was not selected in the run is not listed"


def test_the_spdr_line_in_its_three_states():
    ok = _card_html()
    assert "SPDR check: 478/480 agree (2026-09-30)" in ok
    failed = _card_html(spdr={"state": "error", "error": "SPDR xle: HTTP 500", "date": "2026-10-01T00:00:00Z",
                              "last_good": {"date": "2026-09-30T00:00:00Z", "checked": 480, "agree": 478}})
    assert ("SPDR check failed: SPDR xle: HTTP 500 (last good: 2026-09-30, 478/480 agree)") in failed
    first = _card_html(spdr={"state": "error", "error": "boom", "date": "2026-10-01T00:00:00Z", "last_good": None})
    assert "SPDR check failed: boom (no good run yet)" in first
    never = _card_html(spdr={"state": "never"})
    assert "SPDR check: not run yet" in never


def test_the_type_mix_table_flags_excluded_types_and_labels_the_floor():
    html = _card_html()
    types = html[html.index('id="identity-types"'):html.index('id="identity-sources"')]
    assert "<th>Type</th>" in types and "<th>All</th>" in types and "<th>≥$300M</th>" in types
    assert "common" in types and "5,200" in types and "2,900" in types and "unknown" in types
    assert types.count("(excluded)") == 1
    assert types.index("etp") < types.index("(excluded)") < types.index("unknown")


def test_the_sector_source_mix_table():
    html = _card_html()
    src = html[html.index('id="identity-sources"'):html.index('id="identity-conflicts"')]
    assert "Sector source mix" in src and "GICS" not in src
    assert "<th>Bucket</th>" in src and "<th>All</th>" in src and "<th>≥$300M</th>" in src
    for bucket in ("official", "provider", "SIC", "type rule", "unclassified"):
        assert bucket in src
    assert "1,501" in src and "5,100" in src and "1,200" in src and "325" in src


def test_the_conflicts_section_shows_as_of_the_kinds_the_older_line_and_a_top_table():
    html = _card_html()
    c = html[html.index('id="identity-conflicts"'):html.index('id="identity-stale"')]
    assert "as of 2026-09-30" in c
    assert "sector" in c and "200" in c and "spdr" in c
    assert "12 older, not seen since 2026-09-20" in c
    assert "<details" in c and "Top 2 by market cap" in c
    for head in ("Ticker", "Kind", "Field", "Side A", "Side B", "Note", "Last seen"):
        assert f"<th>{head}</th>" in c
    assert "AAPL" in c and "Communication Services" in c and "SPDR XLC holdings" in c
    assert c.index("AAPL") < c.index("uw_snapshot")


def test_no_older_line_when_nothing_is_older():
    html = _card_html(conflicts={"as_of": "2026-09-30T00:00:00Z", "by_kind": [], "top": [],
                                 "older": {"count": 0, "since": None}})
    assert "not seen since" not in html
    assert "no current conflicts" in html


def test_the_stale_table_shows_counts_every_run_and_the_window():
    html = _card_html()
    s = html[html.index('id="identity-stale"'):]
    for head in ("Source", "Newest", "Rows", "Past window", "Window"):
        assert f"<th>{head}</th>" in s
    assert "sec_exchange" in s and "every run" in s
    assert "sec_submissions" in s and "6,900" in s and ">17<" in s and "30 days" in s
    assert "2026-09-25" in s and "90 days" in s


def test_the_empty_state_when_there_is_no_run_and_no_master():
    html = _with(identity=_identity(last_run=None, type_mix=[], source_mix=[], stale=[],
                                    conflicts={"as_of": None, "by_kind": [], "top": [],
                                               "older": {"count": 0, "since": None}},
                                    spdr={"state": "never"}))
    assert "No identity run yet. Use Fetch identity in the table above." in html
    assert 'id="identity-health"' in html
    assert 'id="identity-types"' not in html


def test_the_card_escapes_every_data_derived_string():
    evil = "<script>alert(1)</script>"
    html = _card_html(
        type_mix=[{"type": evil, "all": 1, "floor": 1, "excluded": True}],
        source_mix=[{"bucket": evil, "all": 1, "floor": 1}],
        conflicts={"as_of": "2026-09-30T00:00:00Z", "by_kind": [{"kind": evil, "count": 1}],
                   "top": [{"ticker": evil, "kind": evil, "field": evil, "side_a": evil, "side_b": evil,
                            "note": evil, "last_seen": evil, "market_cap": 1.0}],
                   "older": {"count": 1, "since": evil}},
        stale=[{"source": evil, "newest": evil, "rows": 1, "past_window": 1, "window": evil}],
        spdr={"state": "error", "error": evil, "date": evil, "last_good": {"date": evil, "checked": 1, "agree": 1}})
    assert evil not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_the_help_text_says_the_card_is_store_only():
    html = _card_html()
    help_ = html[html.index('class="help"'):html.index("</details>", html.index('class="help"'))]
    assert "Identity health" in help_ and "Fetch identity" in help_


def test_the_help_and_the_card_warn_that_fetch_identity_writes_the_universe_classification():
    """Since R3 (#86) a successful identity run applies the master to equity_universe; since #265 that is the
    sector and industry plus UW's earnings date, no longer an industry group or a sub-industry."""
    html = _card_html()
    help_ = html[html.index('class="help"'):html.index("</details>", html.index('class="help"'))]
    assert "equity_universe" in help_ and "every board" in help_
    assert "sector and industry" in help_ and "GICS" not in help_
    card = html[html.index('id="identity-health"'):]
    assert 'id="identity-writes"' in card and "equity_universe" in card
    writes = card[card.index('id="identity-writes"'):card.index("</div>", card.index('id="identity-writes"'))]
    assert "sector and industry" in writes and "next earnings date" in writes
    assert "industry group" not in writes and "sub-industry" not in writes
    empty = report.render_settings_dashboard({"rows": [_identity_row()], "identity": {
        "last_run": None, "type_mix": [], "source_mix": [], "conflicts": {}, "stale": [], "spdr": {"state": "never"}}})
    assert 'id="identity-writes"' in empty[empty.index('id="identity-health"'):]


def test_a_card_from_a_seeded_store_end_to_end(rawstore_tmp_db):
    from RevenueChaser.module_settings import inventory
    from RevenueChaser.module_settings.tests.test_inventory import FIXED, _seed_identity
    _seed_identity()
    html = report.render_settings_dashboard(inventory.collect(now=FIXED))
    assert 'data-fetch="/run/identity"' in html
    assert "SPDR check: 2/3 agree (2026-09-30)" in html
    types = html[html.index('id="identity-types"'):html.index('id="identity-sources"')]
    assert "common" in types and "(excluded)" in types
    c = html[html.index('id="identity-conflicts"'):html.index('id="identity-stale"')]
    assert "as of 2026-09-30" in c and "2 older, not seen since 2026-09-20" in c
    assert "Communication Services" in c
    s = html[html.index('id="identity-stale"'):]
    assert "sec_submissions" in s and "every run" in s


def test_the_card_is_reachable_at_the_identity_anchor_and_comes_after_the_table():
    """R4's funnel note links to ``#identity``: the whole card sits in ``<section id="identity">``."""
    html = _card_html()
    assert '<section id="identity"><div class="bcard" id="identity-health">' in html
    assert html.count('id="identity"') == 1, "the anchor must be unique"
    assert html.index('class="panel"') < html.index('id="identity"') < html.index('id="identity-health"')
    assert html.index('id="identity-stale"') < html.index("</section>"), "the section wraps every card section"
    empty = _with(identity=_identity(last_run=None, type_mix=[], source_mix=[], stale=[],
                                     conflicts={"as_of": None, "by_kind": [], "top": [],
                                                "older": {"count": 0, "since": None}},
                                     spdr={"state": "never"}))
    assert '<section id="identity">' in empty and "</section>" in empty, "the empty state is reachable too"


# ── Discord export readiness card (#242) ─────────────────────────────────────
def _discord(**over):
    d = {"ready": True, "min_version": "2.45", "checks": [
        {"key": "cli", "label": "DCE CLI path (RC_DCE_CLI)", "state": "ok",
         "detail": "C:\\tools\\DiscordChatExporter\\DiscordChatExporter.Cli.exe"},
        {"key": "version", "label": "DCE version", "state": "ok", "detail": "2.48.0 (needs 2.45 or newer)"},
        {"key": "token", "label": "Discord token (DISCORD_TOKEN)", "state": "ok",
         "detail": "DISCORD_TOKEN is set. Its value is never shown."},
        {"key": "guild", "label": "Server id (RC_DISCORD_GUILD_ID)", "state": "ok",
         "detail": "RC_DISCORD_GUILD_ID is set."},
    ]}
    d.update(over)
    return d


def _not_ready():
    checks = _discord()["checks"]
    checks[0] = dict(checks[0], state="bad", detail="RC_DCE_CLI is not set. Point it at the CLI.")
    checks[1] = dict(checks[1], state="skipped", detail="Needs the CLI path above first.")
    checks[2] = dict(checks[2], state="unknown", detail="Could not read it.")
    return _discord(ready=False, checks=checks)


def test_the_discord_card_renders_every_check_with_its_id_state_and_detail():
    html = _with(discord=_discord())
    card = html[html.index('id="discord-readiness"'):]
    for key in ("cli", "version", "token", "guild"):
        assert f'id="discord-check-{key}"' in card, key
    assert "DCE CLI path (RC_DCE_CLI)" in card and "2.48.0 (needs 2.45 or newer)" in card
    assert card.count('class="st-ok">ok</span>') == 4


def test_the_discord_card_colours_each_state_by_its_meaning():
    card = _with(discord=_not_ready())
    card = card[card.index('id="discord-readiness"'):]
    row = lambda key: card[card.index(f'id="discord-check-{key}"'):].split("</tr>")[0]   # noqa: E731
    assert 'class="st-bad">fix</span>' in row("cli")
    assert 'class="st-warn">not checked</span>' in row("version")
    assert 'class="st-warn">unknown</span>' in row("token")
    assert 'class="st-ok">ok</span>' in row("guild")


def test_the_discord_summary_says_ready_only_when_ready():
    ready = _with(discord=_discord())
    assert 'id="discord-summary"' in ready
    summary = ready[ready.index('id="discord-summary"'):].split("</div>")[0]
    assert "Ready" in summary and "Not ready" not in summary
    broken = _with(discord=_not_ready())
    summary = broken[broken.index('id="discord-summary"'):].split("</div>")[0]
    assert "Not ready" in summary and "3 checks need attention" in summary


def test_the_discord_summary_uses_the_singular_for_one_check():
    d = _discord(ready=False)
    d["checks"][2] = dict(d["checks"][2], state="bad", detail="DISCORD_TOKEN is missing.")
    html = _with(discord=d)
    summary = html[html.index('id="discord-summary"'):].split("</div>")[0]
    assert "1 check needs attention" in summary


def test_the_discord_card_says_it_cannot_verify_the_token_and_reads_no_network():
    html = _with(discord=_discord())
    note = html[html.index('id="discord-note"'):].split("</div>")[0]
    assert "token" in note and "network" in note


def test_the_discord_card_escapes_every_data_derived_string():
    d = _discord()
    d["checks"][0] = dict(d["checks"][0], label="<i>lbl</i>", detail='<script>alert(1)</script> & "x"')
    html = _with(discord=d)
    assert "<script>alert(1)</script>" not in html and "<i>lbl</i>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;x&quot;" in html


def test_no_discord_card_without_the_payload_key_or_when_it_is_none():
    assert "discord-readiness" not in report.render_settings_dashboard(_payload())
    assert "discord-readiness" not in _with(discord=None)


def test_the_discord_card_sits_after_the_identity_card_and_before_the_errors_and_boards():
    html = _with(rows=_payload()["rows"], identity=_identity(), discord=_discord(), boards=[_card()],
                 errors=[{"table": "equity_universe", "where": "_count", "error": "x"}])
    assert (html.index('class="panel"') < html.index('id="identity-health"') < html.index('id="discord-readiness"')
            < html.index('id="errors"') < html.index("Board inputs"))
    assert '<section id="discord"><div class="bcard" id="discord-readiness">' in html
    assert html.count('id="discord"') == 1


def test_the_discord_card_shows_without_the_identity_card():
    html = _with(discord=_discord())
    assert 'id="identity-health"' not in html and 'id="discord-readiness"' in html


def test_the_help_text_mentions_the_discord_card():
    html = _with(discord=_discord())
    help_ = html[html.index('class="help"'):html.index("</details>", html.index('class="help"'))]
    assert "Discord" in help_ and "--version" in help_
    # the card does read the token, to see that it is set: the text must say so, and that it is never shown
    assert "token is read only to check that it is set" in help_ and "never shown" in help_
    assert "never the token" not in help_
    assert "no key or token" in help_, "the version probe starts without keys or tokens in its environment"


# --- Automations panel (#130) ------------------------------------------------------

def _autos():
    from RevenueChaser.core import automations
    return automations.static_rows()


def test_automations_section_renders_from_embedded_rows():
    p = _payload()
    p["automations"] = _autos()
    html = report.render_settings_dashboard(p)
    assert 'id="auto-body"' in html and "Automations" in html
    assert "var AUTOMATIONS = " in html
    assert "RC-Store-Backup" in html and "/api/automations" in html


def test_automations_section_is_absent_without_rows():
    html = report.render_settings_dashboard(_payload())
    assert 'id="auto-body"' not in html


def test_the_automations_section_says_a_line_opens():
    """G10 (#124): a reader must know the long text is behind the line, not gone."""
    p = _payload()
    p["automations"] = _autos()
    html = report.render_settings_dashboard(p)
    assert "press a line to see" in html


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
    """The Daily store backup row as the poll returns it: installed, last run ok, next run known."""
    row = dict(_autos()[0], state="enabled", result_kind="ok", result_text="ok",
               last_run="2026-10-23T04:00:05", next_run="2026-10-24T04:00:00",
               missed_runs=0, last_success_age="8 h ago", log_tail=[])
    row.update(fields)
    return row


def test_the_automation_line_carries_its_name_schedule_state_result_and_next_run():
    line = _one_line(_render_rows([_live_row()]))
    assert "Daily store backup" in line and "RC-Store-Backup" in line
    assert "daily 04:00 (local)" in line
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
               last_run="2026-10-22T23:00:00", next_run="2026-10-23T23:00:00", missed_runs=0,
               last_success_age="18 h ago", log_tail=[])
    html = _render_rows([row])
    line = _one_line(html)
    assert "23:00 (local)" in line and "after RC-Daily-Bars" not in line
    assert "weekly 23:00 (local)" in line and "Wednesday" not in line
    opened = _behind_the_click(html)
    assert "after RC-Daily-Bars" in opened and "Wednesday" in opened


def test_the_expanded_part_keeps_the_description_command_and_run_conditions_word_for_word():
    auto = _autos()[0]
    opened = _behind_the_click(_render_rows([_live_row()]))
    assert auto["description"] in opened
    assert auto["command"] in opened
    assert auto["settings"] in opened
    assert auto["schedule"] in opened
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


def test_the_daily_store_backup_keeps_its_off_machine_line_and_sizes_in_the_expanded_part():
    row = _live_row(offmachine={"configured": True, "kind": "ok", "text": "OK, 8 h ago"},
                    backup_size={"kind": "warn",
                                 "text": "local 21.50 GB, above 20 GB (RAW_STORE_BACKUP_WARN_GB)"})
    html = _render_rows([row])
    opened = _behind_the_click(html)
    assert "off-machine copy: OK, 8 h ago" in opened
    assert "backup size: local 21.50 GB, above 20 GB (RAW_STORE_BACKUP_WARN_GB)" in opened
    line = _one_line(html)
    assert "off-machine copy" not in line and "backup size" not in line


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
    seen = _auto_poll(open_ids=["auto-RC-Store-Backup"], focused_id=None)
    assert seen["rendered"] > 0 and seen["open"] == [True]


def test_the_focused_automation_keeps_focus_across_a_poll():
    """A keyboard reader inside an opened row must not be dropped back to the page by the 5 s poll."""
    seen = _auto_poll(open_ids=["auto-RC-Store-Backup"], focused_id="auto-RC-Store-Backup")
    assert seen["open"] == [True] and seen["focused"] is True


def _drill_rows(detail: dict) -> str:
    """``drillFill(detail)`` run in node: the helpers of ``DRILL_JS`` without the DOM wiring at its end."""
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


def test_the_drill_shows_d01_bars_when_the_legacy_cache_is_empty():
    """The number the drill shows is D01, not swing_ohlc. Empty cache must not read 'no bars'."""
    html = _drill_rows({
        "name": "Nvidia",
        "presence": {
            "price_bars_daily": {"bars": 3, "newest": "2026-02-02"},
            "swing_ohlc": {"bars": 0, "newest": None},
        },
    })
    assert ('<span class="dlbl">Daily price bars</span>'
            '<span class="dval">3 bars · newest 2026-02-02</span>' in html)
    assert "Daily OHLC" not in html


def test_the_drill_shows_zero_d01_and_the_legacy_count_under_its_label():
    html = _drill_rows({
        "name": "SPY",
        "presence": {
            "price_bars_daily": {"bars": 0, "newest": None},
            "swing_ohlc": {"bars": 4, "newest": "2026-02-02"},
        },
    })
    assert '<span class="dlbl">Daily price bars</span><span class="dval">no bars</span>' in html
    assert ('<span class="dlbl">Legacy OHLC cache (SPY fallback)</span>'
            '<span class="dval">4 bars · newest 2026-02-02</span>' in html)


def test_the_drill_lists_the_five_scoring_datasets_with_their_newest_date():
    """S10 (#126): bars, quote, vendor statements, EDGAR facts and ownership, in plain English."""
    html = _drill_rows({
        "ticker": "MU", "name": "Micron",
        "presence": {
            "price_bars_daily": {"bars": 2, "newest": "2026-02-02"},
            "quote_snapshot": {"rows": 1, "newest": "2026-10-20", "source": "uw"},
            "vendor_financials": {"rows": 2, "newest": "2026-06-04"},
            "edgar_financials": {"periods": 2, "newest": "2026-08-27"},
            "ownership_snapshot": {"rows": 1, "newest": "2026-10-21", "source": "yfinance"},
        },
    })
    assert ('<span class="dlbl">Daily price bars</span>'
            '<span class="dval">2 bars · newest 2026-02-02</span>' in html)
    assert ('<span class="dlbl">Quote</span>'
            '<span class="dval">1 row · newest 2026-10-20 · uw</span>' in html)
    assert ('<span class="dlbl">Vendor financials</span>'
            '<span class="dval">2 rows · newest 2026-06-04</span>' in html)
    assert ('<span class="dlbl">EDGAR facts</span>'
            '<span class="dval">2 periods · newest 2026-08-27</span>' in html)
    assert ('<span class="dlbl">Ownership</span>'
            '<span class="dval">1 row · newest 2026-10-21 · yfinance</span>' in html)


def test_the_drill_says_which_scoring_dataset_has_no_row():
    html = _drill_rows({
        "name": "NVDA",
        "presence": {
            "price_bars_daily": {"bars": 1, "newest": "2026-02-02"},
            "quote_snapshot": {"rows": 0, "newest": None, "source": None},
            "vendor_financials": {"rows": 0, "newest": None},
            "edgar_financials": {"periods": 0, "newest": None},
            "ownership_snapshot": {"rows": 0, "newest": None, "source": None},
        },
    })
    assert ('<span class="dlbl">Quote</span><span class="dval">no quote stored</span>' in html)
    assert ('<span class="dlbl">Vendor financials</span>'
            '<span class="dval">no vendor statements stored</span>' in html)
    assert ('<span class="dlbl">EDGAR facts</span><span class="dval">no facts stored</span>' in html)
    assert ('<span class="dlbl">Ownership</span><span class="dval">no ownership stored</span>' in html)


def test_the_drill_shows_the_identifiers_at_the_top():
    html = _drill_rows({"ticker": "MU", "cik": "0000723125", "figi": "BBG000BLNNH6",
                        "cusip": "n/a", "isin": "n/a", "presence": {}})
    assert ('<span class="dlbl">CIK</span><span class="dval">0000723125</span>' in html)
    assert ('<span class="dlbl">FIGI</span><span class="dval">BBG000BLNNH6</span>' in html)
    # The columns arrive in a later PR: the drill says n/a rather than pretending there is no value.
    assert ('<span class="dlbl">CUSIP</span><span class="dval">n/a</span>' in html)
    assert ('<span class="dlbl">ISIN</span><span class="dval">n/a</span>' in html)


def test_the_drill_escapes_a_hostile_identifier():
    bad = "<img src=x onerror=__x(1)>"
    html = _drill_rows({"ticker": "MU", "cik": bad, "figi": bad, "cusip": bad, "isin": bad,
                        "presence": {}})
    assert "<img" not in html and html.count("&lt;img") == 4


def test_the_drill_shows_the_sector_and_the_industry_text():
    """#266: Sector and Industry rows; the industry group and sub-industry rows are gone."""
    html = _drill_rows({"name": "Micron", "sector": "Technology", "industry": "Semiconductors",
                        "presence": {"swing_ohlc": {}}})
    assert ('<span class="dlbl">Sector</span><span class="dval">Technology</span>' in html)
    assert ('<span class="dlbl">Industry</span><span class="dval">Semiconductors</span>' in html)
    assert "Industry group" not in html and "Sub-industry" not in html


def test_the_drill_shows_a_dash_for_a_missing_industry_and_escapes_a_hostile_one():
    bare = _drill_rows({"name": "x", "sector": None, "industry": None, "presence": {"swing_ohlc": {}}})
    assert '<span class="dlbl">Industry</span><span class="dval">—</span>' in bare
    bad = "<img src=x onerror=__x(1)>"
    html = _drill_rows({"name": "x", "sector": bad, "industry": bad, "presence": {"swing_ohlc": {}}})
    assert "<img" not in html and html.count("&lt;img") == 2


def test_panel_posts_send_the_panel_header():
    assert "'X-RC-Panel': 'automations'" in report.AUTOMATIONS_JS


def test_renderer_shows_the_off_machine_line_with_a_fixed_class():
    row = dict(_autos()[0], state="enabled",
               offmachine={"configured": True, "kind": "red", "text": "FAILED 8 h ago; last OK 1 d ago"})
    html = _render_rows([row])
    assert "off-machine copy: FAILED 8 h ago; last OK 1 d ago" in html
    assert 'class="auto-desc st-bad"' in html
    ok = _render_rows([dict(row, offmachine={"configured": True, "kind": "ok", "text": "OK, 8 h ago"})])
    assert 'class="auto-desc st-ok"' in ok and "st-bad" not in ok.split("off-machine copy:")[1].split("</div>")[0]


def test_renderer_shows_no_off_machine_line_for_rows_that_have_none():
    html = _render_rows([dict(_autos()[0], state="enabled", offmachine=None)])
    assert "off-machine" not in html
    assert "off-machine" not in _render_rows(_autos())          # embedded rows before the first poll


def test_renderer_escapes_the_off_machine_text_and_never_takes_a_class_from_it():
    bad = "<img src=x onerror=__x(1)>"
    row = dict(_autos()[0], state="enabled", offmachine={"configured": True, "kind": bad, "text": bad})
    html = _render_rows([row])
    assert "<img" not in html and "&lt;img" in html
    assert 'class="auto-desc "' in html or 'class="auto-desc"' in html


def _size_div(html: str) -> str:
    return html.split("backup size:")[0].rsplit("<div", 1)[1] + "backup size:" + \
        html.split("backup size:")[1].split("</div>")[0]


def test_renderer_shows_the_backup_size_line_and_warns_with_a_fixed_class():
    """#309 part 3: both folder sizes on the backup row; amber above a threshold."""
    row = dict(_autos()[0], state="enabled",
               backup_size={"kind": "warn", "text": "local 21.50 GB, above 20 GB (RAW_STORE_BACKUP_WARN_GB)"})
    html = _render_rows([row])
    assert "backup size: local 21.50 GB, above 20 GB (RAW_STORE_BACKUP_WARN_GB)" in html
    assert 'class="auto-desc st-warn"' in _size_div(html)
    ok = _render_rows([dict(row, backup_size={"kind": "ok", "text": "local 1.00 GB, off-machine 2.00 GB"})])
    assert 'class="auto-desc st-ok"' in _size_div(ok)
    none = _render_rows([dict(row, backup_size={"kind": "none", "text": "not measured yet"})])
    assert "st-" not in _size_div(none)


def test_renderer_shows_no_size_line_for_rows_that_have_none():
    assert "backup size" not in _render_rows([dict(_autos()[0], state="enabled", backup_size=None)])
    assert "backup size" not in _render_rows(_autos())


def test_renderer_escapes_the_size_text_and_never_takes_a_class_from_it():
    bad = "<img src=x onerror=__x(1)>"
    html = _render_rows([dict(_autos()[0], state="enabled", backup_size={"kind": bad, "text": bad})])
    assert "<img" not in html and "&lt;img" in html


_BACKUPS = {
    "folder": "/b", "recorded_at": "2026-10-16T04:05:00+00:00",
    "local": {"state": "warn", "bytes": 21_500_000_000, "files": 3382, "duckdb_bytes": 9_589_997_568,
              "duckdb_files": 21, "docs_zip_bytes": 2_668_865_089, "docs_zip_files": 2, "limit_gb": 20.0,
              "text": "21.50 GB, above 20 GB (RAW_STORE_BACKUP_WARN_GB)"},
    "offmachine": {"state": "ok", "bytes": 7_676_019_838, "files": 3375, "limit_gb": 320.0, "text": "7.68 GB"},
    "checkpoints": {"state": "unknown", "text": "unknown, not recorded: the backup job does not write that folder"},
    "scan": {"state": "bad", "last_day": "2026-10-16", "failed": 2, "checked": 3382, "next_due": "2026-10-17",
             "text": "2 files do not match"},
}


def _backups_card(b) -> str:
    html = report.render_settings_dashboard(dict(_payload(), backups=b))
    return html[html.index('id="backups"'):].split("</section>")[0] if 'id="backups"' in html else ""


def test_the_backups_card_shows_every_fact_with_its_id():
    """#309: sizes, file counts, the .duckdb / docs zip split, checkpoints, the last scan and when it was recorded."""
    card = _backups_card(_BACKUPS)
    loc = card[card.index('id="backup-local"'):].split("</tr>")[0]
    assert "21.50 GB, above 20 GB (RAW_STORE_BACKUP_WARN_GB)" in loc and "3,382" in loc
    assert "9.59 GB in 21 .duckdb" in loc and "2.67 GB in 2 edgar_docs zips" in loc
    assert 'class="st-warn"' in loc
    off = card[card.index('id="backup-offmachine"'):].split("</tr>")[0]
    assert "7.68 GB" in off and "3,375" in off and "320 GB" in off and 'class="st-ok"' in off
    cp = card[card.index('id="backup-checkpoints"'):].split("</tr>")[0]
    assert "unknown" in cp and "not recorded" in cp
    scan = card[card.index('id="backup-scan"'):].split("</tr>")[0]
    assert "2026-10-16" in scan and "2 files do not match" in scan and "2026-10-17" in scan and 'class="st-bad"' in scan
    assert 'id="backup-recorded"' in card and "2026-10-16" in card[card.index('id="backup-recorded"'):]


def test_the_backups_card_shows_unknown_for_missing_records():
    unknown = {"folder": "/b", "recorded_at": None,
               "local": {"state": "unknown", "bytes": None, "text": "unknown: no backup run has recorded sizes yet"},
               "offmachine": {"state": "unknown", "bytes": None, "text": "unknown"},
               "checkpoints": {"state": "unknown", "text": "not recorded"},
               "scan": {"state": "unknown", "last_day": None, "next_due": None, "text": "unknown: no scan yet"}}
    card = _backups_card(unknown)
    assert "unknown: no backup run has recorded sizes yet" in card and "unknown: no scan yet" in card
    assert "st-ok" not in card and "never" in card[card.index('id="backup-recorded"'):]


def test_no_backups_card_without_the_key():
    assert _backups_card(None) == ""


def test_the_backups_card_escapes_its_text_and_never_takes_a_class_from_it():
    bad = "<img src=x onerror=__x(1)>"
    b = dict(_BACKUPS, folder=bad, local=dict(_BACKUPS["local"], state=bad, text=bad))
    card = _backups_card(b)
    assert "<img" not in card and "&lt;img" in card


# ── G8 of the GUI brief (#124): where the backups are, what is kept, the last self-check ──────────
def _backup_line(card: str, ident: str) -> str:
    """One summary line of the Backups card, from its id to the end of its div."""
    return card[card.index(f'id="{ident}"'):].split("</div>")[0]


def test_the_backups_card_shows_the_local_folder_path_in_full_and_in_monospace():
    """avo 2026-10-23: "show where the backups are" — the whole path, not a shortened one."""
    long_path = "D:/RevenueChaser backups/raw-store/2026-10-16-with-a-deliberately-long-folder-name"
    card = _backups_card(dict(_BACKUPS, folder=long_path))
    assert f'<code class="bpath" id="backup-folder">{long_path}</code>' in card
    assert "font-family:ui-monospace" in report.CSS[report.CSS.index(".bpath"):].split("}")[0]


def test_the_backups_card_says_unknown_in_words_when_the_record_carries_no_folder():
    card = _backups_card(dict(_BACKUPS, folder=None))
    assert 'id="backup-folder">unknown</code>' in card


def test_the_backups_card_shows_the_offmachine_copy_location_the_record_names():
    """The location is the job's own record of where it copies; the card never opens that folder."""
    where = "G:/my-drive/revenuechaser-backups"
    card = _backups_card(dict(_BACKUPS, offmachine_path=where))
    assert f'<code class="bpath" id="backup-offmachine-path">{where}</code>' in card
    assert 'id="backup-offmachine-path">unknown' in _backups_card(_BACKUPS)   # _BACKUPS carries no location


def test_the_retention_rule_is_read_from_the_job_registry_description():
    """The rule's numbers live in core/automations.py; the card reads them there instead of retyping them."""
    line = _backup_line(_backups_card(_BACKUPS), "backup-retention")
    assert ("keeps 5 daily raw and 5 daily history snapshots, the newest 12 weekly raw copies, "
            "and 2 daily prices snapshots") in line


def test_a_changed_registry_rule_reaches_the_card(monkeypatch):
    """Mutation guard: edit the registry text and the card must follow it, not a copy of its own."""
    from dataclasses import replace

    from RevenueChaser.core import automations
    real = automations.entry("RC-Store-Backup")
    monkeypatch.setattr(report.automations, "entry",
                        lambda name: replace(real, description="Backup; keeps 9 daily raw snapshots. Then a scan."))
    line = _backup_line(_backups_card(_BACKUPS), "backup-retention")
    assert "keeps 9 daily raw snapshots." in line and "12 weekly" not in line


def test_a_rule_the_record_carries_wins_over_the_registry_text():
    line = _backup_line(_backups_card(dict(_BACKUPS, retention="keeps 3 daily copies")), "backup-retention")
    assert "keeps 3 daily copies" in line and "12 weekly" not in line


def test_the_integrity_self_check_reads_as_one_sentence_with_its_numbers():
    line = _backup_line(_backups_card(_BACKUPS), "backup-selfcheck")
    assert "last checked 2026-10-16" in line
    assert "next due 2026-10-17" in line
    assert "3,382 files re-read against their hashes" in line


def test_a_missing_self_check_says_never_and_invents_no_count():
    line = _backup_line(_backups_card(dict(_BACKUPS, scan={"state": "unknown", "text": "unknown: no scan yet"})),
                        "backup-selfcheck")
    assert "last checked never" in line and "next due the next backup run" in line
    assert "no files re-read yet" in line


def test_the_backups_card_has_no_delete_control_and_says_nothing_is_deleted():
    """G8 ships no delete: arming one is G9, a separate gated change with avo's typed words."""
    card = _backups_card(_BACKUPS)
    assert "<button" not in card and "<form" not in card
    assert "Nothing on this page deletes or moves a backup file" in _backup_line(card, "backup-nodelete")


def test_the_backups_card_escapes_the_paths_and_the_rule():
    bad = "<img src=x onerror=__x(1)>"
    card = _backups_card(dict(_BACKUPS, folder=bad, offmachine_path=bad, retention=bad))
    assert "<img" not in card and card.count("&lt;img") >= 3


def test_null_cap_text_does_not_claim_it_is_outside_the_universe():
    """DF99: a NULL cap also occurs before the caps backfill has resolved it."""
    html = report.render_settings_dashboard(_payload())
    assert "not in the $300M classification universe" not in html
    assert "no cap stored" in html


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


def test_the_table_leads_with_source_coverage_and_fresh_then_the_old_columns():
    html = report.render_settings_dashboard(_s5_payload())
    assert _thead_cells(html) == ["Dataset", "Source", "Coverage", "Freshness",
                                  "Last data date", "Rows", "Holes"]


def test_the_three_new_cells_carry_the_registry_facts():
    html = report.render_settings_dashboard(_s5_payload())
    cells = _cells(_main_row(html, "universe"))
    assert cells[0] == "Equity universe"
    assert cells[1] == "Yahoo Finance"
    assert cells[2] == "812 / 905 (90%)"
    assert cells[3] == "2026-09-20"
    assert cells[4] == "no date of its own" and cells[5] == "7,403"
    assert cells[6].startswith("1,710") and "null market cap" in cells[6]


def test_a_row_the_registry_does_not_know_shows_a_dash_in_the_three_new_cells():
    html = report.render_settings_dashboard(_payload())      # neither row carries a source
    cells = _cells(_main_row(html, "corpus"))
    assert cells[1:4] == ["—", "—", "—"], cells


def test_store_tables_writer_action_and_notes_live_in_a_closed_detail_row():
    html = report.render_settings_dashboard(_s5_payload())
    main = _main_row(html, "universe")
    detail = _detail_row(html, "universe")
    assert '<tr class="detail" id="det-universe" hidden>' in html, "closed until asked for"
    assert '<button type="button" class="det-btn" aria-expanded="false"' in main
    assert 'aria-controls="det-universe"' in main
    for gone in ("equity_universe", "RC-Screeners-Daily", "/run/universe-stats",
                 "provenance: __meta__"):
        assert gone not in main, gone
    for held in ("Store table(s)", "equity_universe", "RC-Screeners-Daily",
                 "caps-only fill is POST /run/universe-stats",
                 'data-fetch="/run/universe-stats"', 'id="msg-universe"',
                 "provenance: __meta__"):
        assert held in detail, held
    assert 'class="tbl"' in detail and 'colspan="7"' in detail


def test_every_row_has_one_detail_row_carrying_its_own_action():
    html = report.render_settings_dashboard(_s5_payload())
    assert 'id="det-corpus"' in html
    detail = _detail_row(html, "corpus")
    assert 'data-fetch="/run/edgar-sync"' in detail and 'id="msg-corpus"' in detail
    assert 'id="msg-universe"' not in detail, "an action belongs to its own dataset"


def test_a_coverage_error_is_red_and_says_why():
    p = _payload()
    p["rows"][0].update(source="sec", coverage={"label": "error", "fraction": None,
                                                "problems": ["the summary could not read the store"]},
                        fresh=None)
    html = report.render_settings_dashboard(p)
    cells = _cells(_main_row(html, "universe"))
    assert cells[2].startswith("error")
    assert 'class="warnv"' in _main_row(html, "universe")
    assert "the summary could not read the store" in _main_row(html, "universe")
    assert cells[3] == "—"


def test_the_new_cells_escape_a_hostile_label_and_never_take_a_class_from_it():
    bad = "<img src=x onerror=__x(1)>"
    p = _payload()
    p["rows"][0].update(source=bad, fresh=bad, writer=bad, writer_note=bad,
                        coverage={"label": "<script>alert(1)</script>", "fraction": 0.5,
                                  "problems": [bad]})
    html = report.render_settings_dashboard(p)
    assert "<img" not in html and "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert html.count("&lt;img src=x onerror=__x(1)&gt;") >= 3
    assert "covbar" in html, "the bar's own class comes from the band, never from the label"


def test_the_source_header_offers_the_grouping_toggle():
    html = report.render_settings_dashboard(_s5_payload())
    assert '<th data-group="source">' in html
    assert "function settingsGroupRows(" in html and "function settingsDetail(" in html


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


@pytest.mark.parametrize("db", ["raw", "edgar", "discord", "history", "prices"])
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
    for db in ("raw", "edgar", "discord", "history", "prices"):
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
        {"key": "cftc", "label": "CFTC E-mini COT", "tables": ["cftc_cot"], "rows": 2,
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


def test_every_dataset_row_carries_the_facts_the_filters_read():
    html = report.render_settings_dashboard(_g4_payload())
    row = _main_row(html, "universe")
    assert 'data-db="raw"' in row, "the store file, from the registry"
    assert 'data-source="yfinance"' in row
    assert 'data-state=""' in row, "a healthy row answers no state chip"
    assert "equity_universe" not in row, "the store tables stay in the detail row, where they are read"


def test_a_row_below_seventy_percent_coverage_or_with_a_failed_query_needs_attention():
    html = report.render_settings_dashboard(_g4_payload())
    assert 'data-state="needs attention|no time column"' in _main_row(html, "cftc"), \
        "4% coverage and a failed query, and it has no last data date"
    assert 'data-state="needs attention"' in _main_row(html, "quotes"), \
        "20% coverage is the whole of its problem"
    assert 'data-state="no denominator"' in _main_row(html, "corpus")
    assert 'data-state="no time column"' in _main_row(html, "mystery")


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


def test_a_dataset_the_registry_does_not_know_carries_no_store_file_and_no_source():
    row = _main_row(report.render_settings_dashboard(_g4_payload()), "mystery")
    assert 'data-db=""' in row and 'data-source=""' in row


def test_the_filter_attributes_are_escaped():
    bad = '<img src=x onerror=__x(1)>"'
    p = _g4_payload()
    p["rows"] = [dict(p["rows"][0], db=bad, source=bad, label=bad, tables=[bad])]
    row = _main_row(report.render_settings_dashboard(p), "universe")
    assert "<img" not in row
    assert row.count("&lt;img src=x onerror=__x(1)&gt;") >= 3, "each of the three attributes"
    assert 'data-db="&lt;img' in row and 'data-source="&lt;img' in row


def test_the_count_line_always_says_how_many_of_the_total_are_shown():
    html = report.render_settings_dashboard(_g4_payload())
    bar = _filter_bar(html)
    assert html.index('id="inv-filters"') < html.index('id="inv-table"'), "above the table"
    assert 'class="flbl" for="inv-find">Find a dataset or store table<' in bar
    assert 'Showing 5 of 5 datasets' in bar
    assert 'id="inv-clear"' in bar and "Clear filters" in bar
    assert 'aria-pressed="true"' not in bar, "no filter is on by default"


def test_the_chip_groups_list_only_the_values_the_rows_carry():
    bar = _filter_bar(report.render_settings_dashboard(_g4_payload()))
    assert _chip_texts(bar, "db") == ["raw", "edgar"], "prices / history / discord are not here"
    assert _chip_texts(bar, "source") == ["FINRA and the Commodity Futures Trading Commission",
                                          "SEC EDGAR filings", "Yahoo Finance"]
    assert 'data-value="finra/cftc"' in bar and 'data-value="sec"' in bar, "the filter still compares the machine value"
    assert _chip_texts(bar, "state") == ["needs attention", "no time column", "no denominator"]
    assert bar.count('<button type="button" class="fchip"') == 8
    assert bar.count('aria-pressed="false"') == 8, "every chip is a toggle and none is on"
    assert 'role="group" aria-label="Store file"' in bar
    assert 'role="group" aria-label="Source"' in bar
    assert 'role="group" aria-label="State"' in bar


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
            dataset("CFTC E-mini COT", "cftc_cot", "raw", "finra/cftc",
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

    all_three = ["Equity universe", "10-K / 20-F corpus", "CFTC E-mini COT"]
    assert got["none"] == {"shown": all_three, "count": "Showing 3 of 3 datasets",
                           "clear": False, "details": [True, True, True]}
    assert got["byName"]["shown"] == ["CFTC E-mini COT"], "the dataset name matches"
    assert got["byName"]["count"] == 'Showing 1 of 3 datasets · filtered by search "COT"'
    assert got["byName"]["clear"] is True
    assert got["byTable"]["shown"] == ["10-K / 20-F corpus"], "a store table name matches"
    assert got["and"]["shown"] == [], "raw AND sec is no row at all"
    assert got["and"]["count"] == ("Showing 0 of 3 datasets · filtered by store file raw · "
                                  "source sec")
    assert got["or"]["shown"] == all_three, "two chips in one group widen the result"
    assert got["stateChip"]["shown"] == ["CFTC E-mini COT"]
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


def test_summary_tiles_count_the_rows_already_collected():
    """G5 (#124): every number is a count over the payload's own rows — no second query, no guess."""
    html = _tile_html([
        _tile_row("healthy"),
        _tile_row("empty", rows=0, coverage={"fraction": None, "label": "n/a"}),
        _tile_row("thin", coverage={"fraction": 0.42, "label": "5 / 12 (42%)"}),
        _tile_row("timeless", last_date="—"),
        _tile_row("broken"),
        _tile_row("unknown_count", rows=None),
    ], errors=[{"table": "broken_table", "where": "_quotes_row", "error": "the read failed"}])
    assert _tile_value(html, "Datasets") == "6"
    assert _tile_value(html, "Needs attention") == "2", "the 42% row and the row with a failed query"
    assert _tile_value(html, "Empty") == "1", "a row count of 0; an unknown count is not empty"
    assert _tile_value(html, "No time column") == "1"


def test_a_dataset_at_exactly_seventy_percent_does_not_need_attention():
    """The line is 'below 70%': 0.70 is in, 0.69 is out."""
    assert _tile_value(_tile_html([_tile_row("line", coverage={"fraction": 0.70,
                                                               "label": "7 / 10 (70%)"})]),
                       "Needs attention") == "0"
    assert _tile_value(_tile_html([_tile_row("under", coverage={"fraction": 0.69,
                                                                "label": "69 / 100 (69%)"})]),
                       "Needs attention") == "1"


def test_a_tile_with_nothing_to_count_shows_a_zero():
    """G5 (#124): zero is written, never left blank — a blank tile reads as 'not measured'."""
    html = _tile_html([_tile_row("healthy")])
    assert _tile_value(html, "Needs attention") == "0"
    assert _tile_value(html, "Empty") == "0"
    assert _tile_value(html, "No time column") == "0"
    empty = _tile_html([])
    assert _tile_value(empty, "Datasets") == "0"
    assert _tile_value(empty, "Needs attention") == "0"


def test_refresh_took_shows_the_seconds_the_collect_measured():
    assert _tile_value(_tile_html([_tile_row("healthy")],
                                  timing={"collect_seconds": 1.39}), "Refresh took") == "1.39 seconds"


def test_refresh_took_says_not_measured_when_the_payload_carries_no_timing():
    """No timing is a fact about the payload, not a zero: it is said in words."""
    for payload in ({}, {"timing": {}}, {"timing": {"collect_seconds": None}},
                    {"timing": {"collect_seconds": "fast"}}):
        row = _tile_row("healthy")
        p = {"generated_at": "2026-09-25T20:00:00+00:00", "rows": [row], **payload}
        assert _tile_value(report.render_settings_dashboard(p), "Refresh took") == "not measured"


def test_the_summary_tiles_sit_above_the_table_with_their_labels_and_no_script():
    """Placement and accessibility: after the help block, before the table, plain labelled divs."""
    html = _tile_html([_tile_row("healthy")], timing={"collect_seconds": 1.0})
    block = html[html.index('id="summary-tiles"'):html.index('id="inv-table"')]
    assert html.index("What am I looking at?") < html.index('id="summary-tiles"'), "after the intro"
    assert html.index('id="summary-tiles"') < html.index('id="inv-table"'), "before the table"
    for label in ("Datasets", "Needs attention", "Empty", "No time column", "Refresh took"):
        assert f">{label}</div>" in block, label
    assert 'role="group"' in block and 'aria-labelledby="tile-datasets"' in block
    assert 'aria-labelledby="tile-refresh-took"' in block
    assert "<script" not in block.lower() and "onclick" not in block.lower()

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


def test_the_other_store_tables_section_names_the_table_store_file_rows_and_newest_date():
    p = dict(_payload(), other_tables=[
        {"table": "chain_filings", "db": "edgar", "rows": 3278, "newest": "2026-03-01"},
        {"table": "watchlist", "db": "raw", "rows": 2, "newest": None},
    ])
    html = report.render_settings_dashboard(p)
    section = _other_section(html)
    assert "Other store tables" in section
    assert _OTHER_SENTENCE in section
    assert _thead_cells(section) == ["Table", "Store file", "Rows", "Newest date"]
    assert "chain_filings" in section and "edgar.duckdb" in section
    assert "3,278" in section and "2026-03-01" in section
    assert "watchlist" in section and "raw.duckdb" in section
    assert "no time column" in section, "a table with no date column says so in plain words"


def test_the_other_store_tables_section_escapes_its_cells():
    bad = "<img src=x onerror=__x(1)>"
    p = dict(_payload(), other_tables=[{"table": bad, "db": bad, "rows": 1, "newest": bad}])
    section = _other_section(report.render_settings_dashboard(p))
    assert "<img" not in section
    assert section.count("&lt;img src=x onerror=__x(1)&gt;") == 3


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
