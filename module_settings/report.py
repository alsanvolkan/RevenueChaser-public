"""Settings / Data health board — self-contained dark-theme HTML renderer.

An ops inventory, not an equity board: no filter bar, no Theme cascade; one ticker
combobox (the filter kit's, #383 C1b) opens the stored-data drill. One row per dataset with its store table(s), newest data date,
row count, hole count and a per-dataset Fetch button. Refresh re-reads the store
with zero network; Fetch enqueues that dataset's async job. See
``docs/data_health.md`` §2.
"""
from __future__ import annotations

import html as _html
from datetime import datetime

from RevenueChaser.core.html_safe import script_json
from RevenueChaser.core.reporting_common import (
    FILTERS_CSS,
    FILTERS_JS,
    JOB_JS,
    combobox_html,
    help_html,
    provider_health_html,
)

CSS = """
:root{--bg:#05111F;--panel:#12161e;--panel2:#1a1f2b;--border:#232a38;
--text:#d7dce5;--muted:#8a93a6;--accent:#0EA5E9;--green:#2ecc71;--amber:#f1c40f;
--neon:#00e6a8;--red:#e74c3c;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5}
.wrap{max-width:100%;margin:0 auto;padding:20px 24px 60px}
.topbar{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:10px;border-bottom:1px solid var(--border);padding-bottom:12px;margin-bottom:14px}
.topbar h1{margin:0;font-size:22px;letter-spacing:-.4px}
.topbar .sub{color:var(--muted);margin-top:2px;font-size:13px}
.sync{color:var(--muted);font-size:12.5px;white-space:nowrap}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:4px 6px;overflow-x:auto}
table.inv{border-collapse:collapse;width:100%;font-size:12.5px}
table.inv th,table.inv td{padding:7px 10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top;white-space:nowrap}
table.inv th{color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.03em}
table.inv td.num{text-align:right;font-variant-numeric:tabular-nums}
table.inv tr:last-child td{border-bottom:none}
table.inv .tbl{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11.5px}
table.inv .note{color:var(--muted);white-space:normal;max-width:320px}
table.inv .ok{color:var(--neon)}
table.inv .warnv{color:var(--amber)}
.hlabel{color:var(--muted);font-size:11px;margin-left:6px}
.fetch-btn,.set-btn{background:rgba(14,165,233,.12);border:1px solid var(--accent);color:var(--accent);border-radius:6px;padding:4px 10px;font:600 12px/1.2 inherit;cursor:pointer}
.fetch-btn:hover,.set-btn:hover{background:rgba(14,165,233,.22)}
.fetch-btn:disabled,.set-btn:disabled{opacity:.55;cursor:default}
.fetchmsg{color:var(--muted);font-size:11.5px;margin-left:8px}
.empty{background:var(--panel);border:1px solid var(--border);border-radius:12px;color:var(--muted);font-size:13px;padding:24px;text-align:center}
.frow{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
.flbl{color:var(--muted);font-size:12px;font-weight:600}
select,input[type=text],input[type=number]{background:#10151f;border:1px solid #2a3345;color:var(--text);border-radius:6px;height:34px;padding:0 10px;font:500 13px/1 inherit}
.drillpick .rcf-combo{width:300px}
.drill{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-bottom:14px}
.drill h2{margin:0 0 10px;font-size:15px;letter-spacing:-.2px}
.drill-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:0 26px}
.drow{display:flex;justify-content:space-between;gap:14px;border-bottom:1px solid var(--border);padding:5px 0;font-size:12.5px}
.dlbl{color:var(--muted)}
.dval{text-align:right;font-variant-numeric:tabular-nums}
.dempty{color:var(--muted);font-size:13px;padding:8px 0}
.row-err,.board-err,.errlist{color:var(--red);font-size:12px;white-space:normal}
.errlist{background:var(--panel);border:1px solid var(--red);border-radius:12px;padding:10px 16px;margin-top:14px}
.errlist ul{margin:4px 0 0;padding-left:18px}
h2.sec{font-size:15px;margin:22px 0 10px;letter-spacing:-.2px}
.bcard{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-bottom:14px}
.bcard h3{margin:0 0 2px;font-size:15px}
.bcard>details>summary{cursor:pointer}
.bcard>details>summary h3{display:inline}
.bcard .asof{color:var(--muted);font-size:12.5px;margin-bottom:8px}
.bcard .kv{color:var(--muted);font-size:12.5px;margin:2px 0}
.bcard .kv b{color:var(--text);font-weight:600}
.bcard ul.params{margin:4px 0 8px;padding-left:18px;color:var(--text);font-size:12.5px}
.bnote{color:var(--muted);font-size:13px;padding:6px 0}
.frow2{display:flex;justify-content:space-between;align-items:center;gap:10px;border-bottom:1px dashed var(--border);padding:4px 0;font-size:12.5px}
.frow2 .num{font-variant-numeric:tabular-nums}
.frow2.drop .num{color:var(--amber)}
.frow2.design{color:var(--muted)}
.frow2.design .num{color:var(--muted)}
.frow2.total{border-bottom:none;font-weight:700}
.frow2.note{color:var(--amber)}
.frow2.note .num{color:var(--amber)}
.frow2.note a{color:var(--accent)}
.frow2 .tag{color:var(--muted);font-size:11.5px;margin-left:6px}
.bset{border-top:1px solid var(--border);margin-top:12px;padding-top:10px}
.bset h4{margin:0 0 8px;font-size:13px;display:flex;align-items:center;gap:8px}
.bset .custom{background:rgba(241,196,15,.12);border:1px solid rgba(241,196,15,.4);color:var(--amber);border-radius:99px;padding:0 8px;font-size:11px;font-weight:600}
.sfield{display:grid;grid-template-columns:minmax(200px,260px) 110px 1fr;gap:4px 12px;align-items:center;padding:4px 0;font-size:12.5px}
.sfield label{color:var(--text)}
.sfield input,.sfield select{width:100%;height:30px}
.sfield .rng{color:var(--muted);font-size:11.5px}
.sfield .ovr{color:var(--amber);font-size:11.5px;margin-left:6px}
.sfield .ferr{grid-column:2/-1;color:var(--red);font-size:12px;white-space:normal}
.sfield .ferr:empty{display:none}
.sderived{color:var(--muted);font-size:12px;padding:4px 0}
.sderived b{color:var(--text)}
details.fine{margin:8px 0}
details.fine>summary{cursor:pointer;color:var(--accent);font-size:12.5px;font-weight:600}
.sactions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:10px}
.seterr{color:var(--red);font-size:12px;white-space:normal}
.seterr:empty{display:none}
.bcard h4{margin:14px 0 6px;font-size:13px}
.bcard table.mini{width:auto;min-width:300px;margin:0 0 6px}
.bcard .tag{color:var(--muted);font-size:11.5px;margin-left:6px}
.bcard ul.srcs{margin:4px 0 8px;padding-left:18px;color:var(--muted);font-size:12.5px}
.st-ok{color:var(--neon)}
.st-warn{color:var(--amber)}
.st-bad{color:var(--red)}
.auto-btn{background:var(--panel2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:3px 9px;margin:1px 4px 1px 0;cursor:pointer;font-size:12px}
.auto-btn:hover{border-color:var(--accent)}
.auto-btn:disabled{opacity:.5;cursor:default}
table.auto td{vertical-align:top}
.auto-desc{color:var(--muted);font-size:12px;white-space:normal;max-width:340px}
.auto-log{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;white-space:pre-wrap;max-width:380px}
#auto-msg{color:var(--amber);margin:6px 0;min-height:1em}
"""

JS = JOB_JS + r"""
function settingsFetch(btn){
  var route = btn.getAttribute('data-fetch');
  var out = document.getElementById('msg-' + btn.getAttribute('data-row'));
  btn.disabled = true;
  btn.textContent = 'Running…';
  if(out) out.textContent = 'Running…';
  fetch(route, {method:'POST', headers:{'X-RC-Panel':'settings'}}).then(function(r){ return r.json(); }).then(function(d){
    if(!d.ok || !d.job_id){
      btn.disabled = false; btn.textContent = 'Retry';
      if(out) out.textContent = d.error || 'failed to start';
      return;
    }
    try{ parent.postMessage({type:'rc-job-submitted', job_id: d.job_id}, location.origin); }catch(e){}
    settingsPoll(btn, out, d.job_id);
  }).catch(function(e){
    btn.disabled = false; btn.textContent = 'Retry'; if(out) out.textContent = String(e);
  });
}
function settingsSubjectKeys(msg, keys){
  // The keys a message is about: those named before " must" ("WINDOW + OOS_HOLDOUT
  // must not exceed ..." is about both), never the ones it merely mentions.
  var i = msg.indexOf(' must'), subject = i < 0 ? msg : msg.slice(0, i);
  var words = subject.split(/[^A-Za-z0-9_]+/);
  return keys.filter(function(k){ return words.indexOf(k) >= 0; });
}
function settingsSave(btn){
  var board = btn.getAttribute('data-board');
  var out = document.getElementById('msg-set-' + board);
  var general = document.getElementById('seterr-' + board);
  var inputs = Array.prototype.slice.call(document.querySelectorAll('#settings-' + board + ' [data-setting]'));
  var keys = inputs.map(function(i){ return i.getAttribute('data-setting'); });
  keys.forEach(function(k){ document.getElementById('err-' + k).textContent = ''; });
  if(general) general.textContent = '';
  var changes = {};
  inputs.forEach(function(i){
    if(String(i.value).trim() !== String(i.getAttribute('data-orig'))){
      changes[i.getAttribute('data-setting')] = String(i.value).trim() === '' ? null : Number(i.value);
    }
  });
  if(!Object.keys(changes).length){ if(out) out.textContent = 'No changes to save.'; return; }
  btn.disabled = true;
  if(out) out.textContent = 'Saving…';
  fetch('/api/settings/board/' + encodeURIComponent(board), {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-RC-Panel': 'settings'},
    body: JSON.stringify({changes: changes})
  }).then(function(r){
    return r.json().then(function(d){ return {status: r.status, d: d || {}}; });
  }).then(function(res){
    var d = res.d;
    if(res.status === 202 && d.job_id){
      if(out) out.textContent = 'Queued…';
      try{ parent.postMessage({type:'rc-job-submitted', job_id: d.job_id}, location.origin); }catch(e){}
      settingsPoll(btn, out, d.job_id);
      return;
    }
    btn.disabled = false;
    var msgs = d.messages || [d.error || ('Save failed (HTTP ' + res.status + ')')];
    if(out) out.textContent = 'Not saved — nothing changed.';
    msgs.forEach(function(m){
      var hit = settingsSubjectKeys(String(m), keys);
      if(!hit.length){
        if(general) general.textContent = (general.textContent ? general.textContent + ' ' : '') + m;
        return;
      }
      hit.forEach(function(k){
        var el = document.getElementById('err-' + k);
        el.textContent = (el.textContent ? el.textContent + ' ' : '') + m;
        var det = el.closest && el.closest('details');
        if(det) det.open = true;
      });
    });
  }).catch(function(e){
    btn.disabled = false; if(out) out.textContent = String(e);
  });
}
function settingsReset(btn){
  var board = btn.getAttribute('data-board');
  boardSettingsReset(btn, document.getElementById('msg-set-' + board), board);
}
document.querySelectorAll('.fetch-btn').forEach(function(b){
  b.addEventListener('click', function(){ settingsFetch(b); });
});
document.querySelectorAll('.set-save').forEach(function(b){
  b.addEventListener('click', function(){ settingsSave(b); });
});
document.querySelectorAll('.set-reset').forEach(function(b){
  b.addEventListener('click', function(){ settingsReset(b); });
});
"""

# Automations panel (#130). One row renderer feeds both the embedded rows (static registry
# fields, no live state yet) and the /api/automations poll. Every field goes through rcEsc
# (html_safe.ESC_JS); class names come from the fixed maps below, never from row data.
AUTOMATIONS_JS = r"""
var AUTO_STATE = {not_installed:['Not installed',''], disabled:['Disabled','st-warn'],
  enabled:['Enabled','st-ok'], running:['Running','st-warn'],
  unavailable:['Scheduler unavailable','st-warn'], error:['Scheduler error','st-bad']};
var AUTO_RESULT = {ok:'st-ok', neutral:'', running:'st-warn', red:'st-bad', none:'', warn:'st-warn'};
var AUTO_BUTTONS = {not_installed:[['install','Install']],
  enabled:[['disable','Disable'],['run','Run now'],['remove','Remove']],
  disabled:[['enable','Enable'],['remove','Remove']]};
var AUTO_CONFIRM = {install:'Install', remove:'Remove', run:'Run now'};
function autoRowHtml(r){
  var live = r.state != null;
  var st = AUTO_STATE.hasOwnProperty(r.state) ? AUTO_STATE[r.state] : [r.state, ''];
  var stText = live ? st[0] : 'checking…';
  var rcls = AUTO_RESULT.hasOwnProperty(r.result_kind) ? AUTO_RESULT[r.result_kind] : '';
  var btns = '';
  if(live && AUTO_BUTTONS.hasOwnProperty(r.state)){
    AUTO_BUTTONS[r.state].forEach(function(b){
      btns += '<button type="button" class="auto-btn" data-act="' + b[0] + '" data-name="' +
              rcEsc(r.name) + '">' + b[1] + '</button>';
    });
  }
  var tail = (r.log_tail || []).map(rcEsc).join('\n');
  var tables = (r.tables_written || []).map(rcEsc).join(', ');
  var off = r.offmachine ? '<div class="auto-desc ' +
    (AUTO_RESULT.hasOwnProperty(r.offmachine.kind) ? AUTO_RESULT[r.offmachine.kind] : '') + '">off-machine copy: ' +
    rcEsc(r.offmachine.text) + '</div>' : '';
  var size = r.backup_size ? '<div class="auto-desc ' +
    (AUTO_RESULT.hasOwnProperty(r.backup_size.kind) ? AUTO_RESULT[r.backup_size.kind] : '') + '">backup size: ' +
    rcEsc(r.backup_size.text) + '</div>' : '';
  return '<tr>' +
    '<td><b>' + rcEsc(r.label) + '</b> <span class="tbl">' + rcEsc(r.name) + '</span>' +
      '<div class="auto-desc">' + rcEsc(r.description) + '</div>' +
      '<div class="auto-desc">' + rcEsc(r.command) + '</div>' +
      '<div class="auto-desc">' + rcEsc(r.settings) + '</div>' +
      (tables ? '<div class="auto-desc">writes ' + tables + '</div>' : '') + off + size + '</td>' +
    '<td>' + rcEsc(r.schedule) + (r.offpeak_gated ? '<div class="auto-desc">off-peak gated</div>' : '') + '</td>' +
    '<td class="' + st[1] + '">' + rcEsc(stText) + '</td>' +
    '<td class="' + rcls + '">' + (live ? rcEsc(r.result_text) : '—') +
      (live ? '<div class="auto-desc">last run ' + rcEsc(r.last_run || '—') +
       ' · next ' + rcEsc(r.next_run || '—') + ' · missed ' + rcEsc(r.missed_runs) +
       ' · last success ' + rcEsc(r.last_success_age) + '</div>' : '') + '</td>' +
    '<td><div class="auto-log">' + rcEsc(r.log_path) + (tail ? '\n' + tail : '') + '</div></td>' +
    '<td>' + btns + '</td></tr>';
}
function autoTableHtml(rows, available){
  var head = '<div class="panel"><table class="inv auto"><thead><tr><th>Automation</th>' +
    '<th>Schedule</th><th>State</th><th>Last result</th><th>Log</th><th>Action</th></tr></thead><tbody>';
  var note = available === false
    ? '<div class="auto-desc">Task Scheduler is not available on this host.</div>' : '';
  return note + head + rows.map(autoRowHtml).join('') + '</tbody></table></div>';
}
var autoTimer = null;
function autoRender(rows, available){
  var body = document.getElementById('auto-body');
  if(body) body.innerHTML = autoTableHtml(rows, available);
}
function autoMsg(text){
  var m = document.getElementById('auto-msg');
  if(m) m.textContent = text || '';
}
function autoLoad(){
  clearTimeout(autoTimer);
  fetch('/api/automations').then(function(r){ return r.json(); }).then(function(d){
    if(!d.ok){ autoMsg(d.error || 'could not read the scheduler'); return; }
    autoMsg('');
    autoRender(d.rows || [], d.available);
    var busy = (d.rows || []).some(function(r){ return r.state === 'running'; });
    if(busy) autoTimer = setTimeout(autoLoad, 5000);
  }).catch(function(){ autoMsg('could not read the scheduler'); });
}
function autoAct(btn){
  var name = btn.getAttribute('data-name');
  var act = btn.getAttribute('data-act');
  if(AUTO_CONFIRM[act] && !confirm(AUTO_CONFIRM[act] + ' the scheduled task ' + name + ' on this PC?')) return;
  btn.disabled = true;
  fetch('/api/automations/' + encodeURIComponent(name) + '/' + encodeURIComponent(act),
        {method: 'POST', headers: {'X-RC-Panel': 'automations'}}).then(function(r){ return r.json(); }).then(function(d){
    autoMsg(d.ok ? '' : (d.error || 'failed'));
    autoLoad();
  }).catch(function(){ autoMsg('request failed'); autoLoad(); });
}
function autoInit(){
  var body = document.getElementById('auto-body');
  if(!body) return;
  autoRender(AUTOMATIONS, true);
  body.addEventListener('click', function(ev){
    var b = ev.target.closest ? ev.target.closest('.auto-btn') : null;
    if(b) autoAct(b);
  });
  autoLoad();
}
"""
AUTOMATIONS_BOOT = "autoInit();"

# Drill picker: reads the shared `DATA` embedded by the renderer and fetches the
# per-ticker detail from the store-only API. The combobox behaviour itself is the
# filter kit's RCCombobox (#383 C1b) — this only wires it to the detail panel.
DRILL_JS = r"""
function drillRow(label, value){
  return '<div class="drow"><span class="dlbl">' + rcEsc(label) + '</span>' +
         '<span class="dval">' + rcEsc(value) + '</span></div>';
}
function drillCap(cap){
  if(cap == null) return 'null — no cap stored (outside the $300M universe or not yet resolved)';
  return '$' + Number(cap).toLocaleString('en-US', {maximumFractionDigits: 0});
}
// A dataset's presence as one line: its count, the newest stored session/date, and the source
// where the dataset carries one. null when the ticker has no row in it.
function drillCount(presence, unit){
  var got = presence || {};
  var n = got[unit] || 0;
  if(!n) return null;
  var text = n + ' ' + (n === 1 ? unit.replace(/s$/, '') : unit) + ' · newest ' + (got.newest || '—');
  return got.source ? text + ' · ' + got.source : text;
}
function drillFill(d){
  var p = d.presence || {};
  var d01 = p.price_bars_daily || {};
  var swing = p.swing_ohlc || {};
  var f = d.filing;
  var filing = f
    ? (f.form || '—') + ' · filed ' + (f.filed || '—') + ' · ' + (f.quality || '—')
    : 'no stored 10-K / 20-F';
  var sic = d.sic ? (d.sic + (d.sic_description ? ' — ' + d.sic_description : '')) : '—';
  return ''
    + drillRow('Name', d.name || '—')
    + drillRow('CIK', d.cik || '—')
    + drillRow('FIGI', d.figi || '—')
    + drillRow('CUSIP', d.cusip || '—')
    + drillRow('ISIN', d.isin || '—')
    + drillRow('Market cap', drillCap(d.market_cap))
    + drillRow('Price', d.price == null ? '—' : '$' + Number(d.price).toFixed(2))
    + drillRow('Sector', d.sector || '—')
    + drillRow('Industry', d.industry || '—')
    + drillRow('SIC', sic)
    + drillRow('Last 10-K / 20-F', filing)
    + drillRow('EDGAR facts', drillCount(p.edgar_financials, 'periods') || 'no facts stored')
    + drillRow('Daily price bars', d01.bars
        ? (d01.bars + ' bars · newest ' + (d01.newest || '—')) : 'no bars')
    + drillRow('Quote', drillCount(p.quote_snapshot, 'rows') || 'no quote stored')
    + drillRow('Vendor financials', drillCount(p.vendor_financials, 'rows') || 'no vendor statements stored')
    + drillRow('Ownership', drillCount(p.ownership_snapshot, 'rows') || 'no ownership stored')
    + drillRow('Legacy OHLC cache (SPY fallback)', swing.bars
        ? (swing.bars + ' bars · newest ' + (swing.newest || '—')) : 'no bars')
    + drillRow('OIPD chain', p.oipd_meta ? 'present' : 'missing')
    + drillRow('Relatedness profile', p.relatedness_profiles ? 'present' : 'missing');
}
function onDrillPick(ticker){
  var panel = document.getElementById('drill');
  var body = document.getElementById('drill-body');
  if(!panel || !body) return;
  if(!ticker){ panel.hidden = true; return; }
  panel.hidden = false;
  body.innerHTML = drillRow('Ticker', ticker) + '<div class="dempty">Loading…</div>';
  fetch('/api/settings/ticker/' + encodeURIComponent(ticker)).then(function(r){
    if(r.status === 404 || r.status === 400) throw new Error('unknown ticker');
    if(!r.ok) throw new Error('store read failed');
    return r.json();
  }).then(function(d){
    body.innerHTML = drillRow('Ticker', d.ticker) + drillFill(d);
  }).catch(function(e){
    body.innerHTML = (e && e.message === 'unknown ticker')
      ? '<div class="dempty">No stored data for ' + rcEsc(ticker) + '.</div>'
      : '<div class="dempty">Could not read the store for ' + rcEsc(ticker) + '. See the Logs tab.</div>';
  });
}
// The picker's options (a test reads them back); onPick/onClear look up onDrillPick at call time.
var DRILL_COMBO_OPTS = {input: '#ticker-combo', list: '#ticker-combo-list', rows: DATA, clear: true,
  onPick: function(r){ onDrillPick(r.ticker); }, onClear: function(){ onDrillPick(null); }};
if(document.getElementById('ticker-combo') && typeof RCCombobox !== 'undefined'){
  RCCombobox.mount(DRILL_COMBO_OPTS);
}
"""


def _friendly(ts) -> str:
    """'Thursday, September 25, 2026 EOD' from an ISO timestamp."""
    if not ts:
        return "—"
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d.strftime("%A, %B %d, %Y") + " EOD"
    except ValueError:
        return str(ts)


def _fmt_rows(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_holes(row: dict) -> str:
    holes = row.get("holes")
    if holes is None:
        cell = "—"
    elif holes == 0:
        cell = '<span class="ok">0</span>'
    else:
        cell = f'<span class="warnv">{int(holes):,}</span>'
    label = (row.get("hole_label") or "").strip()
    if label and holes is not None:
        cell += f'<span class="hlabel">{_html.escape(label)}</span>'
    return cell


def _button(spec: dict, row_id: str) -> str:
    """A Fetch button wired to the shared ``settingsFetch`` poller (JS above)."""
    route = _html.escape(str(spec.get("route") or ""))
    label = _html.escape(str(spec.get("label") or "Fetch"))
    row_id = _html.escape(row_id)
    return (f'<button class="fetch-btn" data-fetch="{route}" data-row="{row_id}">'
            f'{label}</button><span class="fetchmsg" id="msg-{row_id}"></span>')


def _action_cell(row: dict) -> str:
    fetch = row.get("fetch")
    if not fetch:
        return "—"
    key = str(row.get("key") or "")
    out = _button(fetch, key)
    alt = row.get("fetch_alt")
    if alt:
        out += _button(alt, f"{key}-alt")
    return out


def _master_age_html(row: dict) -> str:
    """The identity-master age note (#345). Warning style (``warnv``) past the threshold."""
    age = row.get("master_age")
    if not isinstance(age, dict):
        return ""
    text = str(age.get("text") or "")
    if not text:
        return ""
    safe = _html.escape(text)
    if age.get("warn"):
        return f'<div id="identity-master-age" class="warnv">{safe}</div>'
    return f'<div id="identity-master-age">{safe}</div>'


def _row_errors(row: dict, errors: list[dict]) -> str:
    """Red notes for query failures on this row's tables (F4)."""
    mine = set(row.get("tables") or [])
    out = []
    for e in errors:
        if e.get("table") in mine:
            where = _html.escape(str(e.get("where") or ""))
            msg = _html.escape(str(e.get("error") or ""))
            out.append(f'<div class="row-err">query failed ({where}): {msg}</div>')
    return "".join(out)


def _errors_list(errors: list[dict]) -> str:
    if not errors:
        return ""
    items = "".join(
        f'<li>{_html.escape(str(e.get("table") or "?"))} · '
        f'{_html.escape(str(e.get("where") or ""))}: {_html.escape(str(e.get("error") or ""))}</li>'
        for e in errors)
    return ('<div class="errlist" id="errors"><b>Some queries failed</b> — '
            f"a dash above may be an error, not missing data.<ul>{items}</ul></div>")


def _funnel_rows(card: dict) -> str:
    key = str(card.get("key") or "")
    out = []
    for st in card.get("funnel_steps") or []:
        label = _html.escape(str(st.get("label") or st.get("key") or ""))
        n = _fmt_rows(st.get("count"))
        kind = st.get("kind")
        if kind == "note":
            # Shown, never counted: a positive number and a pointer to the cause; no
            # button. A link into this board is an in-page anchor (the Identity card).
            link = st.get("link") or {}
            tab, anchor = str(link.get("tab") or ""), str(link.get("anchor") or "")
            text = _html.escape(str(link.get("label") or tab))
            if tab == "module_settings":
                a = (f' <a href="#{_html.escape(anchor, quote=True)}">{text}</a>'
                     if anchor else "")
            elif tab:
                a = f' <a href="/#{_html.escape(tab, quote=True)}" target="_top">{text}</a>'
            else:
                a = ""
            out.append(f'<div class="frow2 note"><span>⚠ {label}{a}</span>'
                       f'<span class="num">{n}</span></div>')
        elif kind == "drop":
            if st.get("by_design"):
                out.append(f'<div class="frow2 design"><span>{label}'
                           f'<span class="tag">(by design)</span></span>'
                           f'<span class="num">−{n}</span></div>')
            else:
                fix = st.get("fix")
                action = _button(fix, f"{key}-{st.get('key')}") if fix else ""
                out.append(f'<div class="frow2 drop"><span>{label}</span>'
                           f'<span><span class="num">−{n}</span> {action}</span></div>')
        else:
            cls = "total" if kind == "result" else "start"
            out.append(f'<div class="frow2 {cls}"><span>{label}</span>'
                       f'<span class="num">{n}</span></div>')
    return "".join(out)


def _num(v) -> str:
    """A number as a plain input value (5.0 -> 5, 126 -> 126)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return str(int(f)) if f == int(f) else repr(f)


def _setting_field(f: dict) -> str:
    key = str(f.get("key") or "")
    k = _html.escape(key, quote=True)
    label = _html.escape(str(f.get("label") or key))
    orig = _num(f.get("value"))
    if f.get("choices"):
        opts = "".join(
            f'<option value="{_html.escape(_num(c[0]), quote=True)}"'
            f'{" selected" if _num(c[0]) == orig else ""}>{_html.escape(str(c[1]))}</option>'
            for c in f["choices"])
        control = (f'<select id="in-{k}" data-setting="{k}" data-orig="{_html.escape(orig, quote=True)}">'
                   f"{opts}</select>")
    else:
        attrs = ""
        if f.get("lo") is not None:
            attrs += f' min="{_html.escape(_num(f["lo"]), quote=True)}"'
        if f.get("hi") is not None:
            attrs += f' max="{_html.escape(_num(f["hi"]), quote=True)}"'
        step = "any" if f.get("kind") == "float" else "1"
        control = (f'<input id="in-{k}" type="number" data-setting="{k}" step="{step}"{attrs} '
                   f'value="{_html.escape(orig, quote=True)}" '
                   f'data-orig="{_html.escape(orig, quote=True)}">')
    mark = ""
    if f.get("overridden"):
        mark = (f'<span class="ovr">custom (default {_html.escape(_num(f.get("default")))})</span>')
    rng = f'<span class="rng">{_html.escape(str(f.get("range") or ""))}</span>'
    return (f'<div class="sfield"><label for="in-{k}">{label}{mark}</label>{control}{rng}'
            f'<div class="ferr" id="err-{k}" role="alert"></div></div>')


def _settings_form(card: dict) -> str:
    """The editable inputs of a board (Save / Reset wired to ``settingsSave`` /
    ``settingsReset``). Every data-derived string is escaped."""
    s = card.get("settings")
    if not s:
        return ""
    board = _html.escape(str(s.get("board") or card.get("key") or ""), quote=True)
    badge = '<span class="custom" id="custom-badge">Custom settings</span>' if s.get("custom") else ""
    inputs = "".join(_setting_field(f) for f in s.get("inputs") or [])
    fine = "".join(_setting_field(f) for f in s.get("fine") or [])
    d = s.get("derived") or {}
    derived = (f'<div class="sderived">{_html.escape(str(d.get("label") or ""))}: '
               f'<b>{_html.escape(_num(d.get("value")))}</b> (read-only)</div>' if d else "")
    return (
        f'<div class="bset" id="settings-{board}"><h4>Editable inputs {badge}</h4>'
        f'{inputs}{derived}'
        f'<details class="fine"><summary>Fine tuning</summary>{fine}</details>'
        f'<div class="seterr" id="seterr-{board}" role="alert"></div>'
        f'<div class="sactions">'
        f'<button class="set-btn set-save" type="button" data-board="{board}">Save settings</button>'
        f'<button class="set-btn set-reset" type="button" data-board="{board}">Reset to defaults</button>'
        f'<span class="fetchmsg" id="msg-set-{board}"></span></div></div>')


def _board_card(card: dict) -> str:
    label = _html.escape(str(card.get("label") or card.get("key") or ""))
    state = card.get("state")
    tables = ", ".join(_html.escape(str(t)) for t in (card.get("tables") or []))
    params = "".join(f"<li>{_html.escape(str(p))}</li>" for p in (card.get("params") or []))
    win = card.get("window") or {}
    win_txt = " · ".join(f"{_html.escape(k.replace('_', ' '))} {_html.escape(str(v))}"
                         for k, v in win.items() if v)
    asof = (f'<div class="asof">as of {_html.escape(_friendly(card.get("as_of")))}</div>'
            if card.get("as_of") else "")
    if state == "not_rendered":
        note = (f'<div class="bnote">{label} has not been rendered yet. '
                "Open or Refresh that tab first.</div>")
    elif state == "no_funnel":
        note = (f'<div class="bnote">Rendered before funnel reporting; '
                f"Refresh the {label} tab.</div>")
    elif state in ("invalid", "error"):
        err = _html.escape(str(card.get("error") or state))
        note = f'<div class="board-err">{label}: {err}</div>'
    else:
        note = ""
    funnel = ""
    if card.get("funnel_steps") and state in ("ok", "invalid"):
        funnel = f'<div class="funnel2">{_funnel_rows(card)}</div>'
    basis = _html.escape(str(card.get("price_basis") or "—"))
    # Closed <details>: the card stays collapsed until the reader opens it (S9).
    # The outer div keeps the id the Co-movement e2e parser keys on.
    body = (
        f"{asof}"
        f'<div class="kv"><b>Reads</b> {tables}</div>'
        f'<div class="kv"><b>Price basis</b> {basis}</div>'
        + (f'<div class="kv"><b>Window</b> {win_txt}</div>' if win_txt else "")
        + (f'<ul class="params">{params}</ul>' if params else "")
        + note + funnel + _settings_form(card))
    return (
        f'<div class="bcard" id="board-{_html.escape(str(card.get("key") or ""))}">'
        f'<details><summary><h3>{label}</h3></summary>'
        f'<div class="bbody">{body}</div></details></div>')


def _boards_section(cards: list[dict]) -> str:
    if not cards:
        return ""
    return '<h2 class="sec">Board inputs</h2>' + "".join(_board_card(c) for c in cards)


def _automations_section(rows: list[dict]) -> str:
    """The Automations panel shell; ``AUTOMATIONS_JS`` fills it from the embedded rows."""
    if not rows:
        return ""
    return (
        '<h2 class="sec">Automations</h2>'
        '<div class="sub">Scheduled tasks RevenueChaser owns. Nothing is installed '
        "until you press <b>Install</b>; the state below is read live from Task Scheduler "
        "(this page itself stays store-only).</div>"
        '<div id="auto-msg"></div><div id="auto-body"></div>')


def _table(rows: list[dict], errors: list[dict] | None = None) -> str:
    errors = errors or []
    body = []
    for row in rows:
        tables = ", ".join(_html.escape(str(t)) for t in (row.get("tables") or []))
        body.append(
            "<tr>"
            f'<td>{_html.escape(str(row.get("label") or row.get("key") or ""))}</td>'
            f'<td class="tbl">{tables}</td>'
            f'<td>{_html.escape(str(row.get("last_date") or "—"))}</td>'
            f'<td class="num">{_fmt_rows(row.get("rows"))}</td>'
            f'<td class="num">{_fmt_holes(row)}</td>'
            f'<td>{_action_cell(row)}</td>'
            f'<td class="note">{_html.escape(str(row.get("note") or ""))}'
            f'{_master_age_html(row)}'
            f'{_row_errors(row, errors)}</td>'
            "</tr>")
    return (
        '<div class="panel"><table class="inv">'
        "<thead><tr>"
        "<th>Dataset</th><th>Store table(s)</th><th>Last data date</th>"
        "<th>Rows</th><th>Holes</th><th>Action</th><th>Notes</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>")


def _day(ts) -> str:
    """``2026-09-30`` from an ISO timestamp; ``—`` when empty. Plain text: the caller escapes it."""
    text = str(ts or "").strip()
    return text[:10] if text else "—"


def _when(ts) -> str:
    """``2026-09-30 00:06 UTC`` from an ISO timestamp (unparseable text is shown as is). Plain text."""
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(ts)


def _st(status) -> str:
    """A status word coloured by its meaning (ok green, partial/skipped amber, anything else red)."""
    text = str(status or "—")
    cls = "st-ok" if text == "ok" else "st-warn" if text in ("partial", "skipped", "gated") else "st-bad"
    return f'<span class="{cls}">{_html.escape(text)}</span>'


def _agree(agree, checked) -> str:
    return f"{_fmt_rows(agree)}/{_fmt_rows(checked)}"


def _identity_run(last: dict | None) -> str:
    if not last:
        return '<div class="kv" id="identity-run"><b>Last run</b> none recorded</div>'
    counts = last.get("counts") or {}
    bits = [f"started {_html.escape(_when(last.get('started_at')))}",
            f"finished {_html.escape(_when(last.get('finished_at')))}"]
    if counts.get("tickers") is not None:
        bits.append(f"{_fmt_rows(counts.get('tickers'))} tickers")
    if counts.get("conflicts") is not None:
        bits.append(f"{_fmt_rows(counts.get('conflicts'))} conflicts")
    items = []
    for name, s in (last.get("sources") or {}).items():
        if not s.get("selected"):
            continue
        err = f' · <span class="st-bad">{_html.escape(str(s["error"]))}</span>' if s.get("error") else ""
        items.append(f"<li>{_html.escape(str(name))}: {_st(s.get('status'))} · {_fmt_rows(s.get('fetched'))} fetched / "
                     f"{_fmt_rows(s.get('rows'))} rows{err}</li>")
    return (f'<div id="identity-run"><div class="kv"><b>Last run</b> {_st(last.get("status"))} · '
            f'{" · ".join(bits)}</div><ul class="srcs">{"".join(items)}</ul></div>')


def _identity_spdr(spdr: dict | None) -> str:
    spdr = spdr or {}
    state = spdr.get("state")
    if state == "ok":
        text = (f'SPDR check: {_agree(spdr.get("agree"), spdr.get("checked"))} agree '
                f'({_html.escape(_day(spdr.get("date")))})')
    elif state == "error":
        good = spdr.get("last_good")
        tail = (f'last good: {_html.escape(_day(good.get("date")))}, '
                f'{_agree(good.get("agree"), good.get("checked"))} agree' if good else "no good run yet")
        text = (f'<span class="st-bad">SPDR check failed: {_html.escape(str(spdr.get("error") or ""))} '
                f'({tail})</span>')
    else:
        text = "SPDR check: not run yet"
    return f'<div class="kv" id="identity-spdr">{text}</div>'


def _mini_table(heads: list[str], body: list[str]) -> str:
    ths = "".join(f"<th>{_html.escape(h)}</th>" for h in heads)
    return f'<table class="inv mini"><thead><tr>{ths}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def _identity_mix(section_id: str, title: str, head: str, key: str, rows: list[dict], floor_label: str) -> str:
    """A small ``<head> / All / >=floor`` table; a row flagged ``excluded`` carries a muted tag."""
    body = []
    for r in rows:
        tag = '<span class="tag">(excluded)</span>' if r.get("excluded") else ""
        body.append(f'<tr><td>{_html.escape(str(r.get(key) or "—"))}{tag}</td>'
                    f'<td class="num">{_fmt_rows(r.get("all"))}</td><td class="num">{_fmt_rows(r.get("floor"))}</td></tr>')
    return (f'<div id="{section_id}"><h4>{_html.escape(title)}</h4>'
            f'{_mini_table([head, "All", floor_label], body)}</div>')


def _identity_conflicts(c: dict | None) -> str:
    c = c or {}
    kinds = " · ".join(f'{_html.escape(str(k.get("kind")))} {_fmt_rows(k.get("count"))}'
                       for k in c.get("by_kind") or [])
    if c.get("as_of") is None:
        head = "no completed run yet"
    else:
        head = f'as of {_html.escape(_day(c.get("as_of")))}: ' + (kinds or "no current conflicts")
    older = c.get("older") or {}
    older_line = (f'<div class="kv">{_fmt_rows(older.get("count"))} older, not seen since '
                  f'{_html.escape(_day(older.get("since")))}</div>' if (older.get("count") or 0) > 0 else "")
    top = c.get("top") or []
    details = ""
    if top:
        body = "".join(
            f'<tr><td>{_html.escape(str(t.get("ticker")))}</td><td>{_html.escape(str(t.get("kind")))}</td>'
            f'<td>{_html.escape(str(t.get("field")))}</td><td class="note">{_html.escape(str(t.get("side_a") or ""))}</td>'
            f'<td class="note">{_html.escape(str(t.get("side_b") or ""))}</td>'
            f'<td class="note">{_html.escape(str(t.get("note") or ""))}</td>'
            f'<td>{_html.escape(_day(t.get("last_seen")))}</td></tr>' for t in top)
        table = _mini_table(["Ticker", "Kind", "Field", "Side A", "Side B", "Note", "Last seen"], [body])
        details = (f'<details class="fine"><summary>Top {len(top)} by market cap</summary>'
                   f'<div class="panel">{table}</div></details>')
    return (f'<div id="identity-conflicts"><h4>Conflicts</h4><div class="kv">{head}</div>'
            f'{older_line}{details}</div>')


def _identity_stale(stale: list[dict] | None) -> str:
    body = []
    for s in stale or []:
        past = s.get("past_window")
        if past is None:
            cell = "every run"
        else:
            cell = f'<span class="warnv">{_fmt_rows(past)}</span>' if past else _fmt_rows(past)
        body.append(f'<tr><td>{_html.escape(str(s.get("source")))}</td>'
                    f'<td>{_html.escape(_day(s.get("newest")))}</td><td class="num">{_fmt_rows(s.get("rows"))}</td>'
                    f'<td class="num">{cell}</td><td>{_html.escape(str(s.get("window") or "—"))}</td></tr>')
    return (f'<div id="identity-stale"><h4>Cached source freshness</h4>'
            f'{_mini_table(["Source", "Newest", "Rows", "Past window", "Window"], body)}</div>')


def _identity_card(identity: dict | None) -> str:
    """The Identity health card (issue #89): last run, SPDR check, type and sector-source mix, conflicts and
    freshness of the cached sources. Store-only: ``payload["identity"]`` comes from ``inventory``. Without
    that key (an older payload) there is no card; with no run and no master it is just the empty state.
    The card sits in ``<section id="identity">`` so other boards can link to ``#identity``."""
    if not identity:
        return ""
    # Since R3 (#86) the job is a live universe writer: say so where the button's effect is read.
    head = ('<section id="identity"><div class="bcard" id="identity-health"><h3>Identity health</h3>'
            '<div class="asof" id="identity-writes">A successful Fetch identity also writes the master\'s sector and '
            "industry, and UW\'s next earnings date, into <b>equity_universe</b>, which every board reads.</div>")
    if not identity.get("last_run") and not identity.get("type_mix"):
        return (head + '<div class="bnote" id="identity-empty">No identity run yet. '
                "Use Fetch identity in the table above.</div></div></section>")
    floor = identity.get("floor") or 0
    floor_label = f"≥${floor / 1e6:,.0f}M"
    return (head + _identity_run(identity.get("last_run")) + _identity_spdr(identity.get("spdr"))
            + _identity_mix("identity-types", "Security types", "Type", "type", identity.get("type_mix") or [],
                          floor_label)
            + _identity_mix("identity-sources", "Sector source mix", "Bucket", "bucket", identity.get("source_mix") or [],
                          floor_label)
            + _identity_conflicts(identity.get("conflicts")) + _identity_stale(identity.get("stale")) + "</div></section>")


_DISCORD_STATES = {"ok": ("st-ok", "ok"), "bad": ("st-bad", "fix"), "unknown": ("st-warn", "unknown"),
                   "skipped": ("st-warn", "not checked")}


def _discord_card(discord: dict | None) -> str:
    """The Discord export readiness card (#242): one row per check, then a ready / not ready line.

    Store-free: ``payload["discord"]`` comes from ``module_discord.readiness`` (environment and the CLI file).
    Without that key (an older payload, or a check that failed and was recorded in the errors) there is no card.
    It carries only whether the token and the server id are set, never their values.
    """
    if not discord:
        return ""
    body = []
    for c in discord.get("checks") or []:
        cls, word = _DISCORD_STATES.get(c.get("state"), ("st-warn", str(c.get("state") or "unknown")))
        body.append(f'<tr id="discord-check-{_html.escape(str(c.get("key")))}">'
                    f'<td>{_html.escape(str(c.get("label") or ""))}</td>'
                    f'<td><span class="{cls}">{_html.escape(word)}</span></td>'
                    f'<td class="note">{_html.escape(str(c.get("detail") or ""))}</td></tr>')
    if discord.get("ready"):
        summary = ('<span class="st-ok">Ready</span> to Discover and Update channels. Discord itself is not asked '
                   "until you run one.")
    else:
        n = sum(1 for c in discord.get("checks") or [] if c.get("state") != "ok")
        summary = (f'<span class="st-bad">Not ready</span>: {n} check{" needs" if n == 1 else "s need"} attention.')
    return ('<section id="discord"><div class="bcard" id="discord-readiness"><h3>Discord export readiness</h3>'
            '<div class="asof" id="discord-note">Reads the .env settings and the DiscordChatExporter file only, with '
            "no network call. It cannot tell whether Discord accepts the token: only a Discover or Update, which "
            "needs the network, can.</div>"
            f'<div class="kv" id="discord-summary">{summary}</div>'
            f'{_mini_table(["Check", "State", "Detail"], body)}</div></section>')


_BACKUP_STATES = {"ok": "st-ok", "warn": "st-warn", "bad": "st-bad", "unknown": "st-warn", "not set": "st-bad"}


def _gb(nbytes) -> str:
    return f"{nbytes / 1e9:.2f} GB" if isinstance(nbytes, (int, float)) else "—"


def _backups_card(b: dict | None) -> str:
    """The Backups card (#309): both folder sizes, the checkpoints folder, the last integrity scan and when the sizes
    were recorded. Store-free and folder-free: ``payload["backups"]`` comes from the backup job's own records
    (``inventory._backups``). Without that key there is no card; a missing record reads "unknown"."""
    if not b:
        return ""
    esc = _html.escape

    def cell(part: dict) -> str:
        cls = _BACKUP_STATES.get(part.get("state"), "")
        return f'<span class="{cls}">{esc(str(part.get("text") or part.get("state") or "unknown"))}</span>'

    loc, off = b.get("local") or {}, b.get("offmachine") or {}
    cp, scan = b.get("checkpoints") or {}, b.get("scan") or {}
    split = ""
    if loc.get("duckdb_files") is not None or loc.get("docs_zip_files") is not None:
        split = (f'{esc(_gb(loc.get("duckdb_bytes")))} in {_fmt_rows(loc.get("duckdb_files"))} .duckdb files; '
                 f'{esc(_gb(loc.get("docs_zip_bytes")))} in {_fmt_rows(loc.get("docs_zip_files"))} edgar_docs zips')
    limit = lambda part: f'warns above {part.get("limit_gb"):g} GB' if isinstance(part.get("limit_gb"), (int, float)) else ""
    rows = [
        f'<tr id="backup-local"><td>Local backup folder</td><td>{cell(loc)}</td>'
        f'<td class="num">{_fmt_rows(loc.get("files"))}</td>'
        f'<td class="note">{split}{"; " if split else ""}{esc(limit(loc))}</td></tr>',
        f'<tr id="backup-offmachine"><td>Off-machine copy</td><td>{cell(off)}</td>'
        f'<td class="num">{_fmt_rows(off.get("files"))}</td>'
        f'<td class="note">what the job wrote and kept there (its manifest); {esc(limit(off))}</td></tr>',
        f'<tr id="backup-checkpoints"><td>Off-machine checkpoints</td><td>{cell(cp)}</td>'
        f'<td class="num">—</td><td class="note"></td></tr>',
        f'<tr id="backup-scan"><td>Last integrity self-check</td><td>{cell(scan)}</td>'
        f'<td class="num">{_fmt_rows(scan.get("checked"))}</td>'
        f'<td class="note">last {esc(str(scan.get("last_day") or "never"))}; next due '
        f'{esc(str(scan.get("next_due") or "the next backup run"))}</td></tr>',
    ]
    recorded = b.get("recorded_at")
    return ('<section id="backups"><div class="bcard" id="backups-card"><h3>Backups</h3>'
            '<div class="asof" id="backups-note">Read from the nightly backup\'s own records in '
            f'<b>{esc(str(b.get("folder") or "the backup folder"))}</b>; this page walks no folder and never reads '
            "the off-machine (Drive) folder. The self-check re-reads the local folder against the hashes taken "
            "when each file was copied, every 7 days by default.</div>"
            f'<div class="kv" id="backup-recorded">Sizes recorded {esc(str(recorded).replace("T", " ").replace("+00:00", " UTC") if recorded else "never")}</div>'
            f'{_mini_table(["Backup", "Size / result", "Files", "Detail"], rows)}</div></section>')


def render_settings_dashboard(payload: dict) -> str:
    payload = payload or {}
    rows = payload.get("rows") or []
    generated = payload.get("generated_at")

    help_block = help_html(
        "This is an ops inventory of the local <b>.store/</b>: one row per dataset with "
        "its row count, newest data date and known holes. <b>Refresh</b> re-reads the store "
        "with <b>no network</b> — it is instant and always safe. <b>Fetch</b> enqueues that "
        "dataset's job, hits a provider and can take minutes; the row reports back when it "
        "finishes. The ticker drill shows <b>stored</b> data only — it does not ask SEC or "
        "Yahoo. The <b>Identity health</b> card below the table is store-only too; "
        "<b>Fetch identity</b> is what runs the network job, and a successful run also writes "
        "the master's sector and industry into <b>equity_universe</b>, so it changes sectors on every board. "
        "The <b>Discord export readiness</b> card reads the .env settings and the DiscordChatExporter file and "
        "makes no network call. The token is read only to check that it is set and is never shown. The exe is "
        "started once with <b>--version</b> to read its version, with no key or token in its environment."
    )

    if rows:
        body = _table(rows, payload.get("errors") or [])
    else:
        body = ('<div class="empty" id="empty">The store is empty — no datasets have been '
                "populated yet. Run a Fetch to load one.</div>")

    data_json = script_json(payload.get("tickers") or [])
    drill = (
        '<div class="controls"><div class="frow drillpick"><span class="flbl" id="ticker-combo-label">Ticker</span>'
        + combobox_html("ticker-combo", "ticker-combo-label", "Search ticker or name") + "</div></div>"
        '<div class="drill" id="drill" hidden>'
        "<h2>Ticker detail</h2>"
        '<div class="drill-grid" id="drill-body">'
        '<div class="dempty">Pick a ticker to see its stored data.</div>'
        "</div></div>"
    )

    identity_html = _identity_card(payload.get("identity"))
    discord_html = _discord_card(payload.get("discord"))
    backups_html = _backups_card(payload.get("backups"))
    errors_html = _errors_list(payload.get("errors") or [])
    boards_html = _boards_section(payload.get("boards") or [])
    automations = payload.get("automations") or []
    automations_html = _automations_section(automations)
    automations_js = (AUTOMATIONS_JS + AUTOMATIONS_BOOT) if automations else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Settings / Data health</title>
<style>{CSS}</style>
{FILTERS_CSS}
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>Settings / Data health</h1>
      <div class="sub">Store inventory, freshness and per-dataset Fetch</div>
    </div>
    <div class="sync">Last sync: {_html.escape(_friendly(generated))}</div>
  </div>
  {provider_health_html(payload.get("provider_health"))}
  {help_block}
  {drill}
  {body}
  {identity_html}
  {discord_html}
  {errors_html}
  {backups_html}
  {automations_html}
  {boards_html}
</div>
<script>{FILTERS_JS}</script>
<script>
var DATA = {data_json};
var AUTOMATIONS = {script_json(automations)};
{JS}
{DRILL_JS}
{automations_js}
</script>
</body>
</html>
"""
