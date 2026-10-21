"""Tests for the UW Interaction report renderer."""

from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_uw_interaction.report import render_board


def test_empty_payload_renders_idle_console():
    html = render_board(None)
    assert "UW Interaction" in html
    assert "Pick a tool and hit Run" in html
    assert "CATALOG" in html


def test_payload_renders_table_and_raw_json():
    payload = {
        "tool_key": "flow_alerts",
        "label": "Flow Alerts",
        "family": "Options flow",
        "path": "/option-trades/flow-alerts",
        "help": "rule based",
        "params_used": {"limit": 10},
        "columns": ["ticker", "premium"],
        "rows": [{"ticker": "AAPL", "premium": 100000}],
        "row_count": 1,
        "error": None,
        "http_status": None,
        "generated_at": "2026-09-10T12:00:00+00:00",
    }
    html = render_board(payload)
    assert "Flow Alerts" in html
    assert "AAPL" in html
    assert "100000" in html
    assert "/option-trades/flow-alerts" in html
    assert "Last sync:" in html
    assert "Raw JSON" in html


def test_error_payload_shows_reason():
    payload = {
        "tool_key": "x", "label": "X", "family": "Dark pool", "path": "/x",
        "help": "", "params_used": {}, "columns": [], "rows": [],
        "row_count": 0, "error": "expected a JSON array, got str", "http_status": None,
        "generated_at": "2026-09-10T12:00:00+00:00",
    }
    html = render_board(payload)
    assert "expected a JSON array, got str" in html
    assert "No rows returned" not in html  # the error reason, not generic empty


def test_empty_rows_says_so():
    payload = {
        "tool_key": "x", "label": "X", "family": "Options flow", "path": "/x",
        "help": "", "params_used": {}, "columns": [], "rows": [],
        "row_count": 0, "error": None, "http_status": None,
        "generated_at": "2026-09-10T12:00:00+00:00",
    }
    html = render_board(payload)
    assert "No rows returned for these parameters." in html


def test_escapes_html_in_cells():
    payload = {
        "tool_key": "x", "label": "X", "family": "X", "path": "/x",
        "help": "", "params_used": {}, "columns": ["name"],
        "rows": [{"name": "<script>alert(1)</script>"}],
        "row_count": 1, "error": None, "http_status": None,
        "generated_at": "2026-09-10T12:00:00+00:00",
    }
    html = render_board(payload)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def _payload(rows, **kw):
    cols = list(rows[0].keys()) if rows else []
    p = {"tool_key": "x", "label": "X", "family": "F", "path": "/x", "help": "",
         "params_used": {}, "columns": cols, "rows": rows, "row_count": len(rows),
         "error": None, "http_status": None, "generated_at": "2026-09-10T12:00:00+00:00"}
    p.update(kw)
    return p


def test_over_500_rows_says_how_many_are_shown():
    # DF50: rows past 500 used to vanish with no note.
    html = render_board(_payload([{"a": i} for i in range(750)]))
    assert "Showing the first 500 of 750 rows" in html


def test_500_rows_or_fewer_has_no_cut_note():
    html = render_board(_payload([{"a": i} for i in range(500)]))
    assert "Showing the first" not in html


def test_raw_json_label_says_truncated_only_when_cut():
    # DF51: the label was always "(truncated)" and the cut could land mid-row.
    small = render_board(_payload([{"a": 1}]))
    assert "Raw JSON</summary>" in small and "(truncated" not in small
    big = render_board(_payload([{"a": "x" * 300, "i": i} for i in range(1000)]))
    assert "Raw JSON (truncated" in big


def test_raw_json_cut_stays_valid_json():
    import html as _h, json, re
    big = render_board(_payload([{"a": "x" * 300, "i": i} for i in range(1000)]))
    m = re.search(r"<pre>(.*?)</pre>", big, re.S)
    data = json.loads(_h.unescape(m.group(1)))
    assert 0 < len(data["rows"]) < 1000
    assert data["row_count"] == 1000


def test_board_reopens_on_the_tool_that_produced_the_result():
    """DF52: the page script is told which family and tool made the stored result."""
    payload = {
        "tool_key": "flow_alerts", "label": "Flow Alerts", "family": "Options flow", "path": "/x",
        "help": "", "params_used": {}, "columns": [], "rows": [], "row_count": 0, "error": None,
        "http_status": None, "generated_at": "2026-09-10T12:00:00+00:00",
    }
    html = render_board(payload)
    assert 'var LAST_RUN = {"family": "Options flow", "tool_key": "flow_alerts"}' in html
    assert "var LAST_RUN = null;" in render_board(None)


def test_df52_selects_reopen_on_last_tool(tmp_path):
    """DF52 under node: the family and tool selects hold the tool that produced the stored result."""
    payload = {"tool_key": "prediction_unusual", "label": "x", "family": "Prediction markets", "path": "/x",
               "help": "", "params_used": {}, "columns": [], "rows": [], "row_count": 0, "error": None,
               "http_status": None, "generated_at": "2026-09-10T12:00:00+00:00"}
    out = run_board_js(render_board(payload),
                       'console.log(JSON.stringify({f: _els["uw-family"].value, t: _els["uw-tool"].value}))', tmp_path)
    assert out == {"f": "Prediction markets", "t": "prediction_unusual"}


def test_df49_js_sends_a_cleared_field_blank(tmp_path):
    """DF49 under node: collectParams() keeps an emptied field as a blank so the server can drop its default."""
    tail = """
document.querySelectorAll = function(sel){
  if(sel === '#uw-params [name]') return [
    {value: '   ', getAttribute: function(){ return 'min_premium'; }},
    {value: '50', getAttribute: function(){ return 'limit'; }}];
  return [];
};
console.log(JSON.stringify(collectParams()));
"""
    assert run_board_js(render_board(None), tail, tmp_path) == {"min_premium": "", "limit": "50"}
