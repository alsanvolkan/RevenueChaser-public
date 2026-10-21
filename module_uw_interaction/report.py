"""UW Interaction report — a self-contained lab board.

The board is one page: a tool picker (grouped by family) with schema-driven
param inputs on top, and the last run's rows as a generic table below (plus a
raw-JSON toggle). The Run button POSTs to ``/run/uw`` (async job), polls
``/api/job/<id>``, and reloads the page when the job completes — the board owns
its own controls, so the parent shell needs no per-module fetch wiring.

Top bar follows the repo rule: title/subtitle on the left, a single
``Last sync:`` label on the right — no Regime badge, no raw scan timestamp.
"""
from __future__ import annotations

import html as _html
import json
from datetime import datetime, timezone

from RevenueChaser.core.html_safe import ESC_JS, script_json
from RevenueChaser.core.reporting_common import help_html, provider_health_html

from .catalog import catalog_payload

_MAX_TABLE_ROWS = 500
_RAW_JSON_LIMIT = 200_000


def _friendly(ts: str) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone()
        return dt.strftime("%A, %B %d, %Y %H:%M")
    except (ValueError, TypeError):
        return ts


def _fmt_cell(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return f"<code>{_html.escape(json.dumps(v, ensure_ascii=False, default=str)[:120])}</code>"
    return _html.escape(str(v))


def _rows_table(payload: dict) -> str:
    rows = payload.get("rows") or []
    cols = payload.get("columns") or []
    if not rows:
        return ""
    head = "".join(f"<th>{_html.escape(str(c))}</th>" for c in cols)
    body = []
    for r in rows[:_MAX_TABLE_ROWS]:
        tds = "".join(f"<td>{_fmt_cell(r.get(c))}</td>" for c in cols)
        body.append(f"<tr>{tds}</tr>")
    cut = ""
    if len(rows) > _MAX_TABLE_ROWS:
        cut = (f'<div class="note empty">Showing the first {_MAX_TABLE_ROWS} of '
               f'{len(rows)} rows.</div>')
    return (
        f'<div class="tbl-wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>{cut}"
    )


def _raw_json(payload: dict, limit: int = _RAW_JSON_LIMIT) -> tuple[str, bool]:
    """Payload as JSON text, and whether rows were dropped to fit ``limit``.

    The cut is on a row boundary (rows are dropped from the end), so what is
    shown is always valid JSON.
    """
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text, False
    rows = payload.get("rows") or []
    lo, hi = 0, len(rows)
    while lo < hi:  # most rows that still fit
        mid = (lo + hi + 1) // 2
        trial = json.dumps({**payload, "rows": rows[:mid]}, ensure_ascii=False, default=str)
        if len(trial) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return json.dumps({**payload, "rows": rows[:lo]}, ensure_ascii=False, default=str), True


def render_board(payload: dict | None) -> str:
    catalog = catalog_payload()
    last_run = "—"
    result_html = ""
    note_html = ""
    has_result = False
    if payload:
        last_run = _friendly(payload.get("generated_at"))
        has_result = True
        status_cls = "ok"
        if payload.get("error"):
            status_cls = "err"
            note_html = (
                f'<div class="note {status_cls}">{_html.escape(payload["error"])}</div>'
            )
        elif payload.get("row_count") == 0:
            status_cls = "empty"
            note_html = (
                '<div class="note empty">No rows returned for these parameters.</div>'
            )
        result_html = _rows_table(payload)
    elif payload is None:
        note_html = '<div class="note idle">Pick a tool and hit Run to pull live Unusual Whales data.</div>'

    raw_json = ""
    raw_cut = False
    if payload is not None:
        text, raw_cut = _raw_json(payload)
        raw_json = _html.escape(text)

    meta_line = ""
    if payload:
        tool = payload.get("label", "")
        family = payload.get("family", "")
        path = payload.get("path", "")
        meta_line = (
            f'<div class="meta-run">{_html.escape(family)} · '
            f'{_html.escape(tool)} · <code>{_html.escape(path)}</code>'
            f' · {payload.get("row_count", 0)} rows</div>'
        )

    help_block = help_html(
        "A lab for trying one <b>Unusual Whales</b> endpoint before it feeds a board: pick a tool family "
        "and a tool, fill its parameters, and the rows come back as a table below (with a raw-JSON "
        "toggle). <b>Run</b> is the Fetch here: it makes the live call and stores the result. "
        "Loading the page and Refresh show the last stored run with no network call.")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UW Interaction (lab)</title>
<style>
  :root {{ --bg:#0a0d12; --panel:#12161e; --panel2:#1a1f2b; --border:#232a38;
    --text:#d7dce5; --muted:#8a93a6; --accent:#4f9dff; --green:#2ecc71; --amber:#f1c40f; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; font-size:14px; line-height:1.5; }}
  .wrap {{ padding:20px 24px 60px; min-width:1000px; }}
  .topbar {{ display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;
    border-bottom:1px solid var(--border); padding-bottom:12px; margin-bottom:14px; }}
  .topbar h1 {{ margin:0; font-size:22px; letter-spacing:-.4px; }}
  .topbar .sub {{ color:var(--muted); margin-top:2px; font-size:13px; }}
  .sync {{ color:var(--muted); font-size:12px; white-space:nowrap; }}
  .console {{ background:var(--panel); border:1px solid var(--border); border-radius:10px;
    padding:14px 16px; margin-bottom:16px; }}
  .console-row {{ display:flex; gap:10px; flex-wrap:wrap; align-items:flex-end; }}
  .field {{ display:flex; flex-direction:column; gap:4px; font-size:11px; color:var(--muted); }}
  .field label {{ font-weight:600; text-transform:uppercase; letter-spacing:.5px; font-size:10px; }}
  .field input, .field select {{ background:var(--panel2); border:1px solid var(--border);
    color:var(--text); border-radius:6px; padding:6px 8px; font-size:13px; min-width:140px; }}
  .field select {{ min-width:220px; }}
  button.run {{ background:var(--accent); color:#04121f; border:0; border-radius:6px;
    padding:8px 16px; font-weight:700; cursor:pointer; }}
  button.run:hover {{ filter:brightness(1.1); }}
  .help {{ color:var(--muted); font-size:12px; margin-top:8px; }}
  .meta-run {{ color:var(--muted); font-size:12px; margin-bottom:10px; }}
  .note {{ padding:10px 12px; border-radius:8px; margin-bottom:12px; font-size:13px; border:1px solid var(--border); }}
  .note.idle {{ color:var(--muted); background:var(--panel); }}
  .note.empty {{ color:var(--amber); background:rgba(241,196,15,.06); border-color:var(--amber); }}
  .note.err {{ color:#e74c3c; background:rgba(231,76,60,.08); border-color:#e74c3c; }}
  .tbl-wrap {{ overflow-x:auto; background:var(--panel); border:1px solid var(--border); border-radius:10px; }}
  table {{ border-collapse:collapse; width:100%; font-size:12px; }}
  th {{ text-align:left; padding:8px 10px; color:var(--muted); font-size:10px; text-transform:uppercase;
    letter-spacing:.5px; border-bottom:2px solid var(--border); white-space:nowrap; }}
  td {{ padding:7px 10px; border-bottom:1px solid var(--border); vertical-align:top; white-space:nowrap; }}
  tr:hover td {{ background:rgba(79,157,255,.05); }}
  code {{ background:var(--panel2); padding:1px 5px; border-radius:4px; font-size:11px; color:var(--accent); }}
  details.raw {{ margin-top:14px; background:var(--panel); border:1px solid var(--border);
    border-radius:8px; padding:10px 12px; }}
  details.raw summary {{ cursor:pointer; color:var(--muted); font-size:12px; }}
  pre {{ white-space:pre-wrap; word-break:break-all; max-height:420px; overflow:auto; font-size:11px; color:#9fb0c4; }}
  .spinner {{ display:inline-block; width:12px; height:12px; border:2px solid var(--muted);
    border-top-color:var(--accent); border-radius:50%; animation:spin .8s linear infinite; vertical-align:-2px; }}
  @keyframes spin {{ to {{ transform:rotate(360deg); }} }}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>UW Interaction <span style="color:var(--muted);font-size:14px">(lab)</span></h1>
      <div class="sub">Live Unusual Whales endpoint explorer — verify a feed before promoting it into a board.</div>
    </div>
    <div class="sync">Last sync: {_html.escape(last_run)}</div>
  </div>
  {provider_health_html((payload or {}).get("provider_health"))}
  {help_block}

  <div class="console">
    <div class="console-row">
      <div class="field">
        <label>Tool family</label>
        <select id="uw-family"></select>
      </div>
      <div class="field">
        <label>Tool</label>
        <select id="uw-tool"></select>
      </div>
      <div class="field" id="uw-help" style="flex:1;min-width:260px">
        <label>About</label>
        <div id="uw-help-text" style="font-size:12px;line-height:1.5"></div>
      </div>
    </div>
    <div class="console-row" id="uw-params"></div>
    <div class="console-row" style="margin-top:12px">
      <button class="run" id="uw-run">Run</button>
      <span id="uw-status" style="color:var(--muted);font-size:12px"></span>
    </div>
  </div>

  {meta_line}
  {note_html}
  <div id="uw-results">{result_html}</div>

  {'<details class="raw"><summary>' + ("Raw JSON (truncated: first rows only)" if raw_cut else "Raw JSON") + '</summary><pre>' + raw_json + "</pre></details>" if payload is not None else ""}
</div>

<script>
{ESC_JS}
var CATALOG = {script_json(catalog)};
var FAMILY_ORDER = Object.keys(CATALOG);
var LAST_RUN = {script_json({'family': payload.get('family'), 'tool_key': payload.get('tool_key')} if payload else None)};

function fmtVal(v) {{
  if (v === undefined || v === null) return '';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  return String(v);
}}

function toolOptions() {{
  var fam = document.getElementById('uw-family').value;
  var tools = CATALOG[fam] || [];
  var sel = document.getElementById('uw-tool');
  sel.innerHTML = tools.map(function(t) {{
    return '<option value="' + rcEsc(t.key) + '">' + rcEsc(t.label) + '</option>';
  }}).join('');
  renderParams();
}}

function renderParams() {{
  var fam = document.getElementById('uw-family').value;
  var key = document.getElementById('uw-tool').value;
  var tool = (CATALOG[fam] || []).filter(function(t) {{ return t.key === key; }})[0];
  var help = document.getElementById('uw-help-text');
  help.textContent = tool ? (tool.help || '') : '';
  var box = document.getElementById('uw-params');
  if (!tool) {{ box.innerHTML = ''; return; }}
  box.innerHTML = (tool.params || []).map(function(p) {{
    var opts = '';
    if (p.options) {{
      opts = '<option value="">—</option>' + p.options.map(function(o) {{
        return '<option value="' + rcEsc(o) + '">' + rcEsc(o) + '</option>';
      }}).join('');
    }}
    var inp = p.options
      ? '<select name="' + rcEsc(p.name) + '" data-kind="select">' + opts + '</select>'
      : '<input name="' + rcEsc(p.name) + '" data-kind="' + rcEsc(p.kind) + '" value="' + rcEsc(fmtVal(p.default)) + '" ' + (p.in_path ? 'placeholder="path"' : '') + '/>';
    var req = p.required ? ' *' : '';
    return '<div class="field"><label>' + rcEsc(p.label || p.name) + req + '</label>' + inp + '</div>';
  }}).join('');
}}

function collectParams() {{
  var params = {{}};
  document.querySelectorAll('#uw-params [name]').forEach(function(el) {{
    var v = el.value.trim();
    params[el.getAttribute('name')] = v;   // a cleared field is sent blank so the server drops its default (DF49)
  }});
  return params;
}}

var running = false;
function runTool() {{
  if (running) return;
  var fam = document.getElementById('uw-family').value;
  var key = document.getElementById('uw-tool').value;
  if (!key) return;
  var body = new URLSearchParams();
  body.set('tool', key);
  var params = collectParams();
  Object.keys(params).forEach(function(k) {{ body.set(k, params[k]); }});
  var status = document.getElementById('uw-status');
  var btn = document.getElementById('uw-run');
  btn.disabled = true;
  status.innerHTML = '<span class="spinner"></span> Running…';
  running = true;
  fetch('/run/uw', {{ method: 'POST', headers: {{'X-RC-Panel': 'uw-interaction'}}, body: body }})
    .then(function(r) {{ return r.json(); }})
    .then(function(res) {{
      if (!res.ok) {{ throw new Error(res.error || 'run failed'); }}
      try {{ parent.postMessage({{type: 'rc-job-submitted', job_id: res.job_id}}, location.origin); }} catch (e) {{}}
      pollJob(res.job_id);
    }})
    .catch(function(e) {{
      status.textContent = 'Error: ' + e.message;
      btn.disabled = false;
      running = false;
    }});
}}

function pollJob(id) {{
  fetch('/api/job/' + id)
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
      var j = d.job;
      if (!j || j.status === 'queued' || j.status === 'running') {{
        setTimeout(function() {{ pollJob(id); }}, 1200);
        return;
      }}
      var status = document.getElementById('uw-status');
      var btn = document.getElementById('uw-run');
      btn.disabled = false;
      running = false;
      if (j.status === 'error') {{
        status.textContent = 'Error: ' + (j.error || 'unknown');
      }} else {{
        if (j.message) alert(j.message);
        window.location.reload();
      }}
    }})
    .catch(function() {{ setTimeout(function() {{ pollJob(id); }}, 1200); }});
}}

document.getElementById('uw-family').addEventListener('change', toolOptions);
document.getElementById('uw-tool').addEventListener('change', renderParams);
document.getElementById('uw-run').addEventListener('click', runTool);

(function() {{
  var famSel = document.getElementById('uw-family');
  famSel.innerHTML = FAMILY_ORDER.map(function(f) {{
    return '<option value="' + rcEsc(f) + '">' + rcEsc(f) + '</option>';
  }}).join('');
  if (LAST_RUN && CATALOG[LAST_RUN.family]) famSel.value = LAST_RUN.family;   // DF52: reopen on the tool that produced the result
  toolOptions();
  if (LAST_RUN && document.getElementById('uw-family').value === LAST_RUN.family
      && (CATALOG[LAST_RUN.family] || []).some(function(t) {{ return t.key === LAST_RUN.tool_key; }})) {{
    document.getElementById('uw-tool').value = LAST_RUN.tool_key;
  }}
  renderParams();
}})();
</script>
</body>
</html>
"""
