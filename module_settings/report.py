"""Settings / Data health board — self-contained dark-theme HTML renderer.

An ops inventory, not an equity board: its own filters over the table (a find box and store file /
source / state chips, #124 G4 — not the equity filter kit), no Theme cascade; one ticker
combobox (the filter kit's, #383 C1b) opens the stored-data drill below the table (G7, #124),
which carries the identifiers, the per-dataset presence and which boards can score the name.
One row per dataset with its source, coverage, freshness, newest data date, row count and hole
count, then a detail row (store table(s), writer, per-dataset Fetch, notes) opened from the
dataset's name. Below the Board inputs, one server-rendered diagram draws every dataset's
provider → job → store file (#124 G6b). Refresh re-reads the store with zero network; Fetch
enqueues that dataset's async job. See ``docs/data_health.md`` §2.
"""
from __future__ import annotations

import html as _html
from datetime import datetime
from urllib.parse import urlsplit

from RevenueChaser.core import automations, datasets, funnel
from RevenueChaser.core.html_safe import script_json
from RevenueChaser.core.reporting_common import (
    FILTERS_CSS,
    FILTERS_JS,
    JOB_JS,
    combobox_html,
    help_html,
    provider_health_html,
)

# The empty cell of the inventory table (the inventory's own ``inventory.DASH``).
DASH = "—"

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
table.inv .note{color:var(--muted);white-space:normal;max-width:320px}
.tbl{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11.5px}
table.inv .ok{color:var(--neon)}
table.inv .warnv{color:var(--amber)}
/* G2 (#124): the Coverage bar — 64px wide inside the cell, so the table gains no width; its colour is the
   band, and the counts stay as text beside it. */
.covbar{display:inline-block;width:64px;height:6px;border-radius:3px;background:var(--panel2);
  vertical-align:middle;margin-right:7px;overflow:hidden}
.covbar>span{display:block;height:100%;background:currentColor}
.muted{color:var(--muted)}
/* G2: the store-file pill — the Data Flow artifact's DB hues, with history and prices given their own.
   The pill's text always names the file, so colour is never the only signal. */
.db{display:inline-block;background:transparent;border:1px solid currentColor;border-radius:99px;
  padding:0 6px;font:600 10.5px/1.4 inherit;letter-spacing:0;text-transform:none;vertical-align:middle}
.db-raw{color:#4f8cff}
.db-edgar{color:#e0a030}
.db-discord{color:#a070e0}
.db-history{color:#3cb89a}
.db-prices{color:#e06088}
.dblegend{color:var(--muted);font-size:11.5px;margin:6px 2px 0}
.dblegend .db{margin:0 8px 0 4px}
/* G11 (#124): the gap tag beside the dataset name - the same words as its detail line, the colour
   the kind. Colour is never the only signal: the chip always carries its words. */
.gap{display:inline-block;background:transparent;border:1px solid currentColor;border-radius:99px;
  padding:0 6px;font:600 10.5px/1.4 inherit;letter-spacing:0;text-transform:none;vertical-align:middle}
.gap-fixable{color:var(--green)}
.gap-in_build{color:var(--accent)}
.gap-needs_decision{color:var(--amber)}
.gap-ceiling{color:var(--red)}
.gap-self_heals{color:var(--muted)}
.gap-none{color:var(--neon)}
/* G6b (#124): the Data Flow picture — provider → job → store file, rendered server-side from the
   table's own rows. Page weight only: it reads no store and takes no second pass over the registry.
   A line's colour is its store file (the pills' hues); the legend names the five, so colour is not
   the only signal. Press a provider or a job to follow its lines, press it again to show all. */
.flow{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:10px 16px;margin-bottom:14px;overflow-x:auto}
.flow>summary{cursor:pointer;font-size:15px;font-weight:600;letter-spacing:-.2px}
.flow .fnote{color:var(--muted);font-size:12.5px;margin:6px 0 10px;max-width:900px}
.flowsvg{display:block;max-width:100%;height:auto;min-width:760px}
.flowsvg text{fill:var(--text);font:11.5px/1.2 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.flowsvg .fh{fill:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.flowsvg .flow-node rect{fill:var(--panel2);stroke:var(--border)}
.flowsvg .flow-node[data-job],.flowsvg .flow-node[data-provider]{cursor:pointer}
.flowsvg .flow-node:hover rect{stroke:var(--accent)}
.flowsvg .flow-node:focus-visible rect{stroke:var(--accent);stroke-width:2}
/* A picked node dims the rest of the picture; the box under the keys stays at full ink, so Tab
   never lands on a near-invisible control. */
.flowsvg .flow-node:focus-visible{opacity:1}
.flowsvg .flow-edge{fill:none;stroke-width:2;opacity:.75}
.flowsvg .flow-edge.flow-on{opacity:1;stroke-width:3.5}
.flowsvg .flow-dim{opacity:.15}
.flowlegend{color:var(--muted);font-size:12px;display:flex;flex-wrap:wrap;align-items:center;gap:14px;margin-top:10px}
.flowlegend i{display:inline-block;width:18px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
/* S5 (#124): one closed detail row per dataset; the store tables, writer, action and notes live
   there, and the cell wraps so the table gains no nowrap column. */
table.inv tr[hidden]{display:none}
table.inv tr.detail td{white-space:normal;background:var(--panel2)}
table.inv tr.detail .dline{margin:2px 0}
table.inv tr.detail .dline b{color:var(--text);font-weight:600;margin-right:6px}
.det-btn{background:none;border:0;color:var(--text);font:inherit;padding:0;text-align:left;cursor:pointer}
.det-btn:hover{color:var(--accent)}
.det-btn::before{content:"▸ ";color:var(--muted)}
.det-btn[aria-expanded="true"]::before{content:"▾ "}
.grp{background:var(--panel2);border:1px solid var(--border);border-radius:6px;color:var(--muted);
  font:600 10.5px/1.2 inherit;padding:1px 6px;margin-left:6px;cursor:pointer;text-transform:none;letter-spacing:0}
.grp:hover,.grp[aria-pressed="true"]{border-color:var(--accent);color:var(--accent)}
.hlabel{color:var(--muted);font-size:11px;margin-left:6px}
.fetch-btn,.set-btn{background:rgba(14,165,233,.12);border:1px solid var(--accent);color:var(--accent);border-radius:6px;padding:4px 10px;font:600 12px/1.2 inherit;cursor:pointer}
.fetch-btn:hover,.set-btn:hover{background:rgba(14,165,233,.22)}
.fetch-btn:disabled,.set-btn:disabled{opacity:.55;cursor:default}
.fetchmsg{color:var(--muted);font-size:11.5px;margin-left:8px}
/* G5 (#124): the summary tiles above the table, counted from the rows already collected. */
.stiles{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px}
.stile{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:10px 16px;min-width:132px}
.stile-n{font-size:22px;font-weight:700;letter-spacing:-.4px;font-variant-numeric:tabular-nums}
.stile-l{color:var(--muted);font-size:12px}
.empty{background:var(--panel);border:1px solid var(--border);border-radius:12px;color:var(--muted);font-size:13px;padding:24px;text-align:center}
.frow{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
.flbl{color:var(--muted);font-size:12px;font-weight:600}
select,input[type=text],input[type=number]{background:#10151f;border:1px solid #2a3345;color:var(--text);border-radius:6px;height:34px;padding:0 10px;font:500 13px/1 inherit}
.drillpick .rcf-combo{width:300px}
.drill{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-bottom:14px}
.drill-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:0 26px}
/* G7 (#124): which boards can score the name — one row per board, across the whole grid. */
.dgroup{grid-column:1/-1;margin-top:10px;border-top:1px solid var(--border);padding-top:8px}
.dhead{font-size:12.5px;font-weight:600;margin-bottom:2px}
.dnote{color:var(--muted);font-size:11.5px;padding:6px 0 0}
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
/* G8 (#124): a folder path shown in full, in monospace; it wraps rather than being shortened. */
.bpath{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;color:var(--text);
  background:var(--panel2);border:1px solid var(--border);border-radius:4px;padding:1px 6px;
  white-space:pre-wrap;word-break:break-all}
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
/* G6a (#124): the board inputs in the Data Flow artifact's language — provider → dataset → store
   file, one line per store table the board reads, with the dataset's coverage beside it. */
.bflow{margin:4px 0 10px;padding:0;list-style:none}
.bflow-row{display:flex;flex-wrap:wrap;align-items:center;gap:2px 10px;font-size:12.5px;
  padding:4px 0;border-bottom:1px dashed var(--border)}
.bflow-row:last-child{border-bottom:none}
.bflow-row .arrow{color:var(--muted)}
.bflow-row .prov{color:var(--muted)}
.bflow-row .ds{color:var(--text);font-weight:600}
.bflow-row .notclaimed{color:var(--muted)}
/* The store file and the coverage sit at the right edge, so the bars of one card line up. */
.bflow-row .tail{margin-left:auto;display:inline-flex;align-items:center;gap:12px}
.bflow-row .nowrap{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.bflow-row .covbar{flex:0 0 64px}
.bcard h4.bflow-cap{margin:10px 0 4px;font-size:11.5px;color:var(--muted);font-weight:600}
/* S7 (#124): the universe funnel is ``core.funnel``'s shared panel, so its own classes are styled
   here the way the board's tables are: a small bordered table inside a collapsible panel. */
.bcard details.rules{border:1px solid var(--border);border-radius:8px;padding:6px 10px;margin:2px 0 8px}
.bcard details.rules>summary{cursor:pointer;color:var(--accent);font-size:12.5px;font-weight:600}
.bcard details.rules table{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:6px}
.bcard details.rules th,.bcard details.rules td{padding:4px 8px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
.bcard details.rules td:nth-child(2){text-align:right;font-variant-numeric:tabular-nums}
.bcard details.rules .note{color:var(--muted);white-space:normal}
.st-ok{color:var(--neon)}
.st-warn{color:var(--amber)}
.st-bad{color:var(--red)}
.auto-btn{background:var(--panel2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:3px 9px;margin:1px 4px 1px 0;cursor:pointer;font-size:12px}
.auto-btn:hover{border-color:var(--accent)}
.auto-btn:disabled{opacity:.5;cursor:default}
/* G10 (#124): one line per automation. The description, command, run conditions, store tables,
   log and buttons sit behind the row's own ▸, so ten tasks read as ten lines. */
.auto-list{font-size:12.5px}
.auto-item{border-bottom:1px solid var(--border)}
.auto-item:last-child{border-bottom:none}
.auto-item>summary{display:grid;grid-template-columns:minmax(220px,1.5fr) minmax(160px,1.1fr) minmax(120px,max-content) minmax(150px,1fr) minmax(170px,1.1fr);gap:12px;align-items:baseline;padding:8px 10px;cursor:pointer;list-style:none}
.auto-item>summary:hover{background:rgba(255,255,255,.03)}
.auto-item>summary:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.auto-item[open]>summary{background:rgba(14,165,233,.06);box-shadow:inset 2px 0 0 var(--accent)}
.auto-item>summary::-webkit-details-marker{display:none}
.auto-caret::before{content:"▸ "}
.auto-item[open] .auto-caret::before{content:"▾ "}
.auto-when,.auto-next{color:var(--muted);font-variant-numeric:tabular-nums}
.auto-detail{border-top:1px dashed var(--border);padding:6px 10px 12px 26px}
.auto-detail .aline{margin:3px 0;white-space:normal}
.auto-detail .aline b{color:var(--text);font-weight:600;margin-right:6px}
.auto-detail .auto-desc{max-width:none;margin:3px 0}
.auto-detail .auto-log{max-width:none;margin:3px 0;overflow-wrap:anywhere}
.auto-actions{margin:2px 0 10px}
.auto-desc{color:var(--muted);font-size:12px;white-space:normal;max-width:340px}
.auto-log{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;white-space:pre-wrap;max-width:380px}
#auto-msg{color:var(--amber);margin:6px 0;min-height:1em}
/* G4 (#124): the inventory filters above the table. Each chip carries its value as its text, so
   colour is never the only signal; a pressed chip also shows a tick beside its word. */
.filters{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:10px 12px;margin-bottom:14px}
.filters .frow{margin-bottom:8px}
.filters .frow:last-child{margin-bottom:0}
.fchips{display:flex;flex-wrap:wrap;gap:6px}
.fchip{background:var(--panel2);border:1px solid var(--border);border-radius:99px;color:var(--text);
  font:600 12px/1.2 inherit;padding:4px 10px;cursor:pointer}
.fchip:hover{border-color:var(--accent)}
.fchip[aria-pressed="true"]{background:rgba(14,165,233,.18);border-color:var(--accent);color:var(--accent)}
.fchip[aria-pressed="true"]::before{content:"✓ "}
.fcount{color:var(--text);font-size:12.5px;font-variant-numeric:tabular-nums}
.fclear{background:none;border:1px solid var(--border);border-radius:6px;color:var(--muted);
  font:600 12px/1.2 inherit;padding:4px 10px;cursor:pointer}
.fclear:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
.fclear:disabled{opacity:.55;cursor:default}
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
// S5 (#124): each dataset's detail row (store tables, writer, action, notes) opens from its name.
// A button, not a click on a <td>: it is focusable and announces its state.
function settingsDetail(btn){
  var row = document.getElementById(btn.getAttribute('aria-controls'));
  if(!row) return;
  var open = btn.getAttribute('aria-expanded') !== 'true';
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  row.hidden = !open;
}
// One pair per dataset — its detail row always travels with the row above it — for the group
// toggle and the filters: the count line always says how many of the total are shown.
function settingsRowPairs(body){
  var pairs = [], cur = null;
  Array.prototype.forEach.call(body.children, function(tr){
    if(tr.className.indexOf('detail') >= 0){ if(cur) cur.push(tr); return; }
    cur = [tr]; pairs.push(cur);
  });
  return pairs;
}
// Group the inventory rows by Source, or put them back in the rendered (worst coverage first) order.
function settingsGroupRows(body, on){
  if(!body) return;
  var pairs = settingsRowPairs(body);
  function key(pair){
    var order = String(pair[0].getAttribute('data-order') || 0);
    var source = on ? (pair[0].getAttribute('data-source') || '\uffff') : '';
    return source + ('0000' + order).slice(-4);
  }
  pairs.sort(function(a, b){ var x = key(a), y = key(b); return x < y ? -1 : x > y ? 1 : 0; });
  pairs.forEach(function(pair){ pair.forEach(function(tr){ body.appendChild(tr); }); });
}
function settingsFilter(){
  var body = document.querySelector('#inv-table tbody'); if(!body) return;
  var box = document.getElementById('inv-find'), word = String((box && box.value) || '').trim();
  var pick = {db: [], source: [], state: []}, named = [], shown = 0, pairs = settingsRowPairs(body);
  document.querySelectorAll('#inv-filters button[aria-pressed="true"]').forEach(function(b){
    pick[b.getAttribute('data-chips')].push(b.getAttribute('data-value')); });
  pairs.forEach(function(pair){
    var tr = pair[0], btn = tr.querySelector('.det-btn'), tbl = pair[1] && pair[1].querySelector('.tbl');
    var find = ((btn ? btn.textContent : '') + ' ' + (tbl ? tbl.textContent : '')).toLowerCase();
    var states = String(tr.getAttribute('data-state') || '').split('|');
    var ok = (!word || find.indexOf(word.toLowerCase()) >= 0)
      && (!pick.db.length || pick.db.indexOf(tr.getAttribute('data-db')) >= 0)
      && (!pick.source.length || pick.source.indexOf(tr.getAttribute('data-source')) >= 0)
      && (!pick.state.length || pick.state.some(function(s){ return states.indexOf(s) >= 0; }));
    if(ok){ tr.hidden = false; shown += 1; return; }
    if(btn) btn.setAttribute('aria-expanded', 'false');   // a hidden dataset's detail row goes
    pair.forEach(function(el){ el.hidden = true; });      // down with it, and stays collapsed
  });
  if(word) named.push('search "' + word + '"');
  ['db', 'source', 'state'].forEach(function(k){ if(pick[k].length)
    named.push((k === 'db' ? 'store file' : k) + ' ' + pick[k].join(' or ')); });
  document.getElementById('inv-count').textContent = 'Showing ' + shown + ' of ' + pairs.length +
    ' datasets' + (named.length ? ' · filtered by ' + named.join(' · ') : '');
  document.getElementById('inv-clear').disabled = !named.length;
}
function settingsFilterClear(){
  document.getElementById('inv-find').value = '';
  document.querySelectorAll('#inv-filters button[aria-pressed="true"]')
    .forEach(function(b){ b.setAttribute('aria-pressed', 'false'); });
  settingsFilter();
}
var invFind = document.getElementById('inv-find'), invClear = document.getElementById('inv-clear');
if(invFind) invFind.addEventListener('input', settingsFilter);
if(invClear) invClear.addEventListener('click', settingsFilterClear);
document.querySelectorAll('#inv-filters button[aria-pressed]').forEach(function(b){
  b.addEventListener('click', function(){
    b.setAttribute('aria-pressed', b.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
    settingsFilter();
  });
});
var groupBtn = document.querySelector('[data-group="source"] button');
if(groupBtn){
  groupBtn.addEventListener('click', function(){
    var on = groupBtn.getAttribute('aria-pressed') !== 'true';
    groupBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    settingsGroupRows(document.querySelector('#inv-table tbody'), on);
  });
}
document.querySelectorAll('.det-btn').forEach(function(b){
  b.addEventListener('click', function(){ settingsDetail(b); });
});
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
// G10 (#124): the closed line carries the name, the schedule time, the state word, the last
// result and the next run. Everything else — description, command, run conditions, store
// tables, off-machine copy, sizes, log and buttons — is inside the row's own <details>.
function autoWhen(r){
  // The clock part of the schedule: the day list and ", after <task>" are conditions, and read in the detail.
  var text = String(r.schedule == null ? '' : r.schedule).replace(/\s*,\s*after .*$/, '');
  return text.replace(/^weekly\s+.*?\s(\d{1,2}:\d{2}\s+\(local\))$/, 'weekly $1');
}
function autoNext(r){
  // Task Scheduler answers with an ISO stamp ("2026-10-24T04:00:00.0000000+02:00"); show date and time.
  var text = String(r.next_run == null ? '' : r.next_run);
  return text.indexOf('T') > 0 ? text.replace('T', ' ').slice(0, 16) : text;
}
function autoSummaryHtml(r, live, st, stText, rcls){
  var result = live && r.result_text && r.result_text !== '—' ? 'last result: ' + rcEsc(r.result_text) : '';
  var next = live && r.next_run ? 'next run: ' + rcEsc(autoNext(r)) : '';
  return '<summary>' +
    '<span class="auto-name"><span class="auto-caret" aria-hidden="true"></span><b>' + rcEsc(r.label) + '</b> ' +
      '<span class="tbl">' + rcEsc(r.name) + '</span></span>' +
    '<span class="auto-when">' + rcEsc(autoWhen(r)) + '</span>' +
    '<span class="auto-state ' + st[1] + '">' + rcEsc(stText) + '</span>' +
    '<span class="auto-res ' + rcls + '">' + result + '</span>' +
    '<span class="auto-next">' + next + '</span>' +
    '</summary>';
}
function autoDetailHtml(r, live, btns, tables, tail, off, size){
  var lines = '<div class="aline"><b>What it does</b>' + rcEsc(r.description) + '</div>' +
    '<div class="aline"><b>Command</b><span class="tbl">' + rcEsc(r.command) + '</span></div>' +
    '<div class="aline"><b>Schedule</b>' + rcEsc(r.schedule) + '</div>' +
    '<div class="aline"><b>Run conditions</b>' + rcEsc(r.settings) + '</div>';
  if(r.offpeak_gated) lines += '<div class="aline"><b>Off-peak only</b>it defers itself outside the peak hours</div>';
  if(tables) lines += '<div class="aline"><b>Store tables it writes</b><span class="tbl">' + tables + '</span></div>';
  if(live){
    lines += '<div class="aline"><b>Last run</b>' + rcEsc(r.last_run || 'not recorded') +
      ' · next run ' + rcEsc(autoNext(r) || 'not known') +
      ' · missed runs ' + rcEsc(r.missed_runs) +
      ' · last success ' + rcEsc(r.last_success_age) + '</div>';
  }
  lines += off + size +
    '<div class="aline"><b>Log file</b><div class="auto-log">' + rcEsc(r.log_path) +
    (tail ? '\n' + tail : '') + '</div></div>';
  return '<div class="auto-detail">' + (btns ? '<div class="auto-actions">' + btns + '</div>' : '') +
    lines + '</div>';
}
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
  return '<details class="auto-item" id="auto-' + rcEsc(r.name) + '">' +
    autoSummaryHtml(r, live, st, stText, rcls) +
    autoDetailHtml(r, live, btns, tables, tail, off, size) +
    '</details>';
}
function autoTableHtml(rows, available){
  var note = available === false
    ? '<div class="auto-desc">Task Scheduler is not available on this host.</div>' : '';
  var list = rows.length
    ? '<div class="auto-list">' + rows.map(autoRowHtml).join('') + '</div>'
    : '<div class="auto-desc">No scheduled tasks are registered.</div>';
  return note + '<div class="panel">' + list + '</div>';
}
var autoTimer = null;
function autoRender(rows, available){
  var body = document.getElementById('auto-body');
  if(!body) return;
  // The panel re-renders on every poll while a task runs: the row the reader opened stays open,
  // and focus stays in that row instead of falling back to the page.
  var open = [];
  var was = body.querySelectorAll ? body.querySelectorAll('details[open]') : [];
  Array.prototype.forEach.call(was, function(d){ if(d.id) open.push(d.id); });
  var here = document.activeElement && document.activeElement.closest
    ? document.activeElement.closest('details') : null;
  var focusId = here && here.id ? here.id : '';
  body.innerHTML = autoTableHtml(rows, available);
  open.forEach(function(id){
    var d = document.getElementById(id);
    if(d) d.open = true;
  });
  if(focusId){
    var row = document.getElementById(focusId);
    var head = row && row.querySelector ? row.querySelector('summary') : null;
    if(head && head.focus) head.focus();
  }
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
    + drillRow('Ticker', d.ticker || '—')
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
    + drillRow('Relatedness profile', p.relatedness_profiles ? 'present' : 'missing')
    + drillBoards(d.boards);
}
// Which boards can score this name (G7, #124). The server decides each board's state from the
// store tables this drill already measured; this only prints it. A board that reads a table the
// drill does not measure says so in words — it never claims one way or the other.
var DRILL_BOARD_WORDS = {can: 'yes — every dataset it reads is stored',
                         cannot: 'no — missing ',
                         not_checked: 'not checked for this board'};
function drillBoards(list){
  if(!list || !list.length) return '';
  var rows = '';
  for(var i = 0; i < list.length; i++){
    var b = list[i] || {};
    var said;
    if(b.state === 'can') said = DRILL_BOARD_WORDS.can;
    else if(b.state === 'cannot') said = DRILL_BOARD_WORDS.cannot
      + ((b.missing || []).join(', ') || 'a dataset this board reads');
    else said = DRILL_BOARD_WORDS.not_checked;
    rows += drillRow(b.label || '', said);
  }
  return '<div class="dgroup" id="drill-boards"><div class="dhead">Boards that can score this name</div>'
    + rows
    + '<div class="dnote">A board can score the name when every store table it reads that this '
    + 'drill measures has a row for the name. A board that reads a store table this drill does not '
    + 'measure says "not checked for this board": it may be able to score the name, and this drill '
    + 'does not know.</div></div>';
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
    body.innerHTML = drillFill(d);
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

# G6b (#124): the Data Flow picture's one behaviour. Press a provider or a job node to follow its
# lines and dim every other; press it again to clear. Class and aria-pressed toggles only — the
# diagram itself is server-rendered, so nothing here puts a string into the page. Guarded: a page
# with no diagram (an empty store) does nothing at all.
FLOW_JS = r"""
// G6b (#124): press a provider or a job to follow its lines; press it again to clear.
(function(){
  var svg = document.getElementById('flow-svg'), picked = '';
  if(!svg || !svg.addEventListener) return;
  function node(ev){ return ev.target && ev.target.closest ? ev.target.closest('[data-node]') : null; }
  function paint(){
    var edges = svg.querySelectorAll('.flow-edge'), nodes = svg.querySelectorAll('[data-node]'), keep = {}; keep[picked] = 1;
    for(var i = 0; i < edges.length; i++){
      var e = edges[i], a = e.getAttribute('data-a'), b = e.getAttribute('data-b');
      var on = !!picked && (a === picked || b === picked);
      if(on){ keep[a] = 1; keep[b] = 1; }
      e.classList.toggle('flow-on', on); e.classList.toggle('flow-dim', !!picked && !on);
    }
    for(var j = 0; j < nodes.length; j++){
      var id = nodes[j].getAttribute('data-node');
      nodes[j].classList.toggle('flow-dim', !!picked && !keep[id]);
      if(nodes[j].getAttribute('aria-pressed') !== null) nodes[j].setAttribute('aria-pressed', picked === id ? 'true' : 'false');
    }
  }
  function pick(g){
    var id = g.getAttribute('data-node');
    if(!g.getAttribute('data-job') && !g.getAttribute('data-provider')) return;
    picked = picked === id ? '' : id; paint();
  }
  svg.addEventListener('click', function(ev){ var g = node(ev); if(g) pick(g); });
  svg.addEventListener('keydown', function(ev){
    if(ev.key !== 'Enter' && ev.key !== ' ') return;
    ev.preventDefault(); var g = node(ev); if(g) pick(g);
  });
})();
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


# G5 (#124): the coverage line under which a dataset is called out on the summary tiles.
NEEDS_ATTENTION_BELOW = 0.70


def _shows_dash(value) -> bool:
    """True when the cell renders the dash: an empty value and the dash look the same on the table."""
    text = str(value or "").strip()
    return not text or text == DASH


def _seconds_text(payload: dict) -> str:
    """How long the collect took, in words; a payload that carries no measurement says so."""
    timing = (payload or {}).get("timing")
    seconds = timing.get("collect_seconds") if isinstance(timing, dict) else None
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds >= 0:
        return f"{float(seconds):.2f} seconds"
    return "not measured"


def _tile(key: str, label: str, value: str, title: str = "") -> str:
    """One summary tile: a number and the words it counts. A plain labelled div, no script."""
    tid = f"tile-{key}"
    tip = f' title="{_html.escape(title, quote=True)}"' if title else ""
    return (f'<div class="stile" role="group" aria-labelledby="{tid}"{tip}>'
            f'<div class="stile-n">{_html.escape(value)}</div>'
            f'<div class="stile-l" id="{tid}">{_html.escape(label)}</div></div>')


def _summary_tiles(rows: list[dict], payload: dict) -> str:
    """The board's totals, counted from the rows already collected: no second query, no guess."""
    rows = rows or []
    errors = (payload or {}).get("errors") or []
    attention = empty = no_time = 0
    for row in rows:
        coverage = row.get("coverage")
        fraction = coverage.get("fraction") if isinstance(coverage, dict) else None
        thin = (isinstance(fraction, (int, float)) and not isinstance(fraction, bool)
                and fraction < NEEDS_ATTENTION_BELOW)
        if thin or _row_error_records(row, errors):
            attention += 1
        count = row.get("rows")
        if isinstance(count, (int, float)) and not isinstance(count, bool) and count == 0:
            empty += 1
        if _shows_dash(row.get("last_date")):
            no_time += 1
    return ('<div class="stiles" id="summary-tiles">'
            + _tile("datasets", "Datasets", str(len(rows)),
                    "Every dataset in the table below")
            + _tile("needs-attention", "Needs attention", str(attention),
                    "Datasets whose coverage is below 70%, or whose query failed")
            + _tile("empty", "Empty", str(empty), "Datasets that store nothing yet")
            + _tile("no-time-column", "No time column", str(no_time),
                    "Datasets whose last data date is a dash")
            + _tile("refresh-took", "Refresh took", _seconds_text(payload),
                    "Seconds this page took to read the store")
            + "</div>")


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


def _row_error_records(row: dict, errors: list[dict]) -> list[dict]:
    """The payload's error records that name one of this row's tables."""
    mine = set(row.get("tables") or [])
    return [e for e in errors if e.get("table") in mine]


def _row_errors(row: dict, errors: list[dict]) -> str:
    """Red notes for query failures on this row's tables (F4)."""
    return "".join(
        f'<div class="row-err">query failed ({_html.escape(str(e.get("where") or ""))}): '
        f'{_html.escape(str(e.get("error") or ""))}</div>'
        for e in _row_error_records(row, errors))


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


def _provider_name(source) -> str:
    """The provider in words, from the page's one list of provider names (``SOURCE_WORDS``).

    The dataset table, these board rows and the flow diagram all read that list, so one provider
    cannot be called three things on one page. A source the list does not know is named as unnamed,
    never as a wrong name.
    """
    key = str(getattr(source, "value", source) or "").strip()
    if not key:
        return "Provider not recorded"
    words = source_words(key.lower())
    if words == key.lower():
        return f"Provider not named in the dataset registry ({key})"
    return words


def _board_flows(card: dict, rows_by_key: dict) -> list[dict]:
    """One line per store table the board reads: its dataset, provider, store file, coverage row.

    The provider, the dataset label and the store file come from the registry index the dataset
    table already read; the coverage record is the one ``collect()`` attached to that dataset's
    own row. A render therefore reads the payload it was given and asks the store nothing (the
    Refresh budget, #124). A table no dataset claims is kept, to be said in words.
    """
    flows = []
    claimed_tables = datasets.tables_claimed()  # the index, read once, never the store
    for table in card.get("tables") or []:
        name = str(table)
        try:
            spec = datasets.dataset_of_table(name)
        except KeyError:
            # ``dataset_of_table`` refuses a table two datasets claim as well as an unknown one, and
            # those are different facts: say which, so a line never makes a claim that is not true.
            flows.append({"table": name, "claimed": False,
                          "shared": name in claimed_tables})
            continue
        flows.append({"table": name, "claimed": True, "provider": _provider_name(spec.source),
                      "dataset": spec.label, "db": spec.db, "row": rows_by_key.get(spec.key)})
    return flows


def _flow_row(flow: dict) -> str:
    """One source → dataset → store line. Every string here is data, so every string is escaped."""
    table = _html.escape(flow["table"])
    if not flow.get("claimed"):
        words = ("claimed by more than one dataset" if flow.get("shared")
                 else "not claimed by any dataset")
        return (f'<li class="bflow-row"><span class="muted">store table</span>'
                f'<span class="tbl">{table}</span>'
                f'<span class="arrow" aria-hidden="true">→</span>'
                f'<span class="notclaimed">{words}</span></li>')
    pill = _db_pill(flow.get("db"))
    store = (f'<span class="muted">store file</span> {pill}' if pill
             else '<span class="muted">no store file named</span>')
    # The bar and its percentage are the coverage cell of the dataset's own table row, so the two
    # pages cannot disagree; a dataset with no collected row claims no coverage here.
    cell = _coverage_cell(flow["row"]) if flow.get("row") else ""
    cov = (f'<span class="nowrap"><span class="muted">coverage</span> {cell}</span>'
           if cell else "")
    return (f'<li class="bflow-row"><span class="prov">{_html.escape(flow["provider"])}</span>'
            f'<span class="arrow" aria-hidden="true">→</span>'
            f'<span class="ds">{_html.escape(flow["dataset"])}</span>'
            f'<span class="tbl">({table})</span>'
            f'<span class="tail"><span class="nowrap">{store}</span>{cov}</span></li>')


def _board_card(card: dict, rows_by_key: dict | None = None) -> str:
    label = _html.escape(str(card.get("label") or card.get("key") or ""))
    state = card.get("state")
    flows = _board_flows(card, rows_by_key or {})
    reads = ('<h4 class="bflow-cap">What this board reads</h4><ul class="bflow">'
             + "".join(_flow_row(f) for f in flows) + "</ul>") if flows else ""
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
        f"{reads}"
        f'<div class="kv"><b>Price basis</b> {basis}</div>'
        + (f'<div class="kv"><b>Window</b> {win_txt}</div>' if win_txt else "")
        + (f'<ul class="params">{params}</ul>' if params else "")
        + note + funnel + _settings_form(card))
    return (
        f'<div class="bcard" id="board-{_html.escape(str(card.get("key") or ""))}">'
        f'<details><summary><h3>{label}</h3></summary>'
        f'<div class="bbody">{body}</div></details></div>')


def _boards_section(cards: list[dict], rows: list[dict] | None = None) -> str:
    """The Board inputs cards. ``rows`` is the inventory's own list, keyed here once so each card
    can show its datasets' coverage without a second read (G6a, #124)."""
    if not cards:
        return ""
    by_key = {str(r.get("key") or ""): r for r in (rows or []) if isinstance(r, dict)}
    return ('<h2 class="sec">Board inputs</h2>'
            + "".join(_board_card(c, by_key) for c in cards))


def _automations_section(rows: list[dict]) -> str:
    """The Automations panel shell; ``AUTOMATIONS_JS`` fills it from the embedded rows."""
    if not rows:
        return ""
    return (
        '<h2 class="sec">Automations</h2>'
        '<div class="sub">Scheduled tasks RevenueChaser owns. Nothing is installed '
        "until you press <b>Install</b>; the state below is read live from Task Scheduler "
        "(this page itself stays store-only). Each task is one line; press a line to see "
        "what it does, its command, its run conditions, its log and its buttons.</div>"
        '<div id="auto-msg"></div><div id="auto-body"></div>')


def _cov_bar(fraction) -> tuple[str, str]:
    """The bar's band class and drawn percentage, or ``("", "")`` when the record has no fraction.

    The drawn width is clamped to 100%, but the label beside it keeps the real counts: a coverage
    above 100% draws a full bar and still says so. Bands are the page's own state classes.
    """
    if fraction is None:
        return "", ""
    try:
        pct = max(0, min(100, int(round(float(fraction) * 100))))
    except (TypeError, ValueError):
        return "", ""
    band = "st-ok" if pct >= 95 else "st-warn" if pct >= 70 else "st-bad"
    return band, str(pct)


def _coverage_cell(row: dict) -> str:
    """The Coverage cell: the fill bar and its percentage, then the counts (S5, G2).

    A record with no fraction (``n/a``, ``unknown``) draws no bar and reads muted, never 0%, so an
    unmeasurable dataset cannot look empty. ``error`` keeps its red label: a failed query is not a
    quiet hole.
    """
    coverage = row.get("coverage") or {}
    label = str(coverage.get("label") or DASH)
    band, pct = _cov_bar(coverage.get("fraction"))
    bar = (f'<span class="covbar {band}" role="img" aria-label="coverage {pct} percent">'
           f'<span style="width:{pct}%"></span></span>') if band else ""
    text = _html.escape(label)
    if label == "error":
        out = f'<span class="warnv">{text}</span>'
    elif band:
        out = text
    else:
        out = f'<span class="muted">{text}</span>'
    problems = [str(p) for p in coverage.get("problems") or [] if str(p).strip()]
    if problems:
        out += f'<span class="hlabel">{_html.escape("; ".join(problems))}</span>'
    return bar + out


# The five store files a dataset can be written to: the Data Flow artifact's DB hues (G2).
DB_PILLS = ("raw", "edgar", "discord", "history", "prices")


def _db_pill(db) -> str:
    """The store-file pill for ``db``, or ``""`` for no file and for any name outside the palette.

    The name is matched against the palette, never interpolated into the class, so a hostile value
    draws nothing rather than a class.
    """
    name = str(db or "").strip().lower()
    if name not in DB_PILLS:
        return ""
    title = _html.escape(f"stored in {name}.duckdb under .store/", quote=True)
    return f'<span class="db db-{name}" title="{title}">{name}</span>'


def _db_legend() -> str:
    """One line under the table naming every store file, so a pill is explained once (G2)."""
    pills = " ".join(_db_pill(db) for db in DB_PILLS)
    return (f'<div class="dblegend" id="db-legend">Store file: {pills} '
            "— each pill names the .store/ DuckDB that dataset is written to.</div>")


# Provider names spelled out for the reader (avo's copy rule, 23 Oct 2026). The registry value stays the
# machine key (``data-source``, the filter chips' ``data-value``); only the visible text is words.
SOURCE_WORDS = {
    "uw": "Unusual Whales",
    "yfinance": "Yahoo Finance",
    "sec": "SEC EDGAR filings",
    "finra/cftc": "FINRA and the Commodity Futures Trading Commission",
    "cboe": "Cboe (Chicago Board Options Exchange)",
    "fred": "FRED (Federal Reserve Economic Data)",
    "openfigi": "OpenFIGI",
    "derived": "worked out by us from other datasets",
    "manual": "entered by hand",
    "store": "the store's own bookkeeping",
    "unknown": "more than one provider, or not known",
}


def source_words(value) -> str:
    """The plain-words name of a registry source value; an unlisted value is shown as it is."""
    return SOURCE_WORDS.get(str(value), str(value))
# ── G6b (#124): the Data Flow picture — provider → job → store file ─────────────────────────────
# Built from the rows the inventory table already carries, so the diagram costs page weight and
# never a query: no store read, no second pass over the registry (the Refresh budget is 2.5 s).
FLOW_TITLE = "Where the data comes from and where it is stored"
# A dataset the registry gives no writer: no scheduled task writes it, a person starts the Fetch.
FLOW_BY_HAND = "started by hand from the dashboard"
# A dataset the registry gives no single provider (its own ``unknown`` value, or none at all).
FLOW_UNKNOWN_PROVIDER = "unknown provider"
# A dataset whose store file neither the row nor the registry names. Said, never guessed.
FLOW_NO_STORE = "no store file recorded"
# Past this much SVG the <details> starts collapsed, so a much larger registry cannot cost the
# first paint of the table above it (the brief's 60 KB line). Measured on the SVG alone.
FLOW_MAX_BYTES = 60_000
# The inks the edges and store nodes use: the .db-* pills' hues, so a colour means one file
# everywhere on this board. A file outside the five (or none) draws in the page's muted ink.
DB_COLORS = {"raw": "#4f8cff", "edgar": "#e0a030", "discord": "#a070e0",
             "history": "#3cb89a", "prices": "#e06088"}
FLOW_UNKNOWN_COLOR = "#8a93a6"


# The three columns: x, width, header, and the graph key it draws. The gaps hold the edges. A name
# longer than the column holds is cut at ``FLOW_TEXT_MAX`` and kept in full in its hover title.
FLOW_COLUMNS = ((8, 260, "Provider", "providers"),
                (308, 380, "Job that writes it", "jobs"),
                (728, 180, "Store file", "stores"))
FLOW_PAD = 8
FLOW_TOP = 46
FLOW_ROW = 44
FLOW_H = 30
FLOW_W = 916
# The longest node text the columns hold at 11.5px. Longer names are cut here (the full name stays
# in the node's hover title), so two columns can never run into each other.
FLOW_TEXT_MAX = 42


def _flow_provider(value) -> str:
    """The provider's words, from the page's one list of provider names (``SOURCE_WORDS``).

    A value that list does not know is drawn as it stands; a row with no provider at all becomes
    the ``unknown provider`` node.
    """
    name = str(value or "").strip()
    return source_words(name.lower()) if name else FLOW_UNKNOWN_PROVIDER


def _flow_store_order(name: str) -> tuple:
    """The legend's own order for the five store files, then any other name by its spelling."""
    return (DB_PILLS.index(name) if name in DB_PILLS else len(DB_PILLS), name.lower())


def _flow_nodes(counts: dict[str, int], order) -> list[dict]:
    return [{"name": name, "count": n} for name, n in sorted(counts.items(), key=lambda kv: order(kv[0]))]


def flow_graph(rows: list[dict] | None) -> dict:
    """The provider → job → store-file graph of the datasets already collected (G6b, #124).

    Pure: it reads the rows and, for a row without its own store file, the registry's record — never
    the store. Nodes are de-duplicated by their words and every edge counts the datasets that take
    it, so one line can say how many datasets it carries. The provider name is the registry's
    ``source`` spelled out, the job is its ``writer`` (``started by hand from the dashboard`` when
    there is none), and the store file is the row's ``db``, else the registry's.
    """
    providers: dict[str, int] = {}
    jobs: dict[str, int] = {}
    stores: dict[str, int] = {}
    edges: dict[tuple[str, str, str], int] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        provider = _flow_provider(row.get("source"))
        job = str(row.get("writer") or "").strip() or FLOW_BY_HAND
        store = _db_of(row) or FLOW_NO_STORE
        for counted, name in ((providers, provider), (jobs, job), (stores, store)):
            counted[name] = counted.get(name, 0) + 1
        edges[(provider, job, store)] = edges.get((provider, job, store), 0) + 1
    return {
        "providers": _flow_nodes(providers, str.lower),
        "jobs": _flow_nodes(jobs, str.lower),
        "stores": _flow_nodes(stores, _flow_store_order),
        "edges": [{"provider": p, "job": j, "store": s, "count": n}
                  for (p, j, s), n in sorted(
                      edges.items(),
                      key=lambda kv: (_flow_store_order(kv[0][2]), kv[0][0].lower(), kv[0][1].lower()))],
    }


def _flow_count(n: int) -> str:
    """``1 dataset`` / ``4 datasets``: the unit is always named."""
    return f"{n} dataset" if n == 1 else f"{n} datasets"


def _flow_words(name: str) -> str:
    """The node's text in full, cut only past what its column holds; the hover title keeps all of it."""
    return name if len(name) <= FLOW_TEXT_MAX else name[:FLOW_TEXT_MAX - 1] + "…"


def _flow_node(kind: str, name: str, x: int, y: int, width: int, count: int) -> str:
    """One rounded rectangle. A provider or a job is a real button (keyboard reachable); a store
    file is not, because pressing it would select nothing a reader could act on."""
    node_id = _html.escape(f"{kind}:{name}", quote=True)
    said = f"{name}.duckdb" if kind == "store" and name in DB_PILLS else name
    title = _html.escape(f"{said} — {_flow_count(count)}")
    if kind == "store":
        # The border carries the file's hue. The palette lives in DB_COLORS, so it is written
        # inline: an inline style outranks the stylesheet's default border, and the five colours
        # keep one source.
        return (f'<g class="flow-node" data-node="{node_id}"><title>{title}</title>'
                f'<rect x="{x}" y="{y}" width="{width}" height="{FLOW_H}" rx="6"'
                f' style="stroke:{DB_COLORS.get(name, FLOW_UNKNOWN_COLOR)}"/>'
                f'<text x="{x + 10}" y="{y + 19}">{_html.escape(_flow_words(said))}</text></g>')
    who = "Provider" if kind == "provider" else "Job"
    label = _html.escape(f"{who} {name}: {_flow_count(count)}; press to follow its lines", quote=True)
    attribute = "data-provider" if kind == "provider" else "data-job"
    return (f'<g class="flow-node" tabindex="0" role="button"'
            f' {attribute}="{_html.escape(name, quote=True)}" aria-pressed="false"'
            f' data-node="{node_id}" aria-label="{label}">'
            f"<title>{title}</title>"
            f'<rect x="{x}" y="{y}" width="{width}" height="{FLOW_H}" rx="6"/>'
            f'<text x="{x + 10}" y="{y + 19}">{_html.escape(_flow_words(said))}</text></g>')


def _flow_curve(x1: int, y1: int, x2: int, y2: int) -> str:
    """A cubic path from one column into the next: it leaves and arrives flat, so its ends read clean."""
    mid = (x1 + x2) // 2
    return f"M{x1},{y1} C{mid},{y1} {mid},{y2} {x2},{y2}"


def _flow_svg(graph: dict) -> str:
    """The diagram: three columns of rounded rectangles, one cubic line per edge.

    A line's colour is the store file it lands in, and its hover title names the whole way —
    provider, job, file and the number of datasets — so the colour is never the only signal.
    """
    depth = max([len(graph[key]) for key in ("providers", "jobs", "stores")] + [1])
    height = FLOW_TOP + depth * FLOW_ROW + FLOW_PAD
    px, pw, _, _ = FLOW_COLUMNS[0]
    jx, jw, _, _ = FLOW_COLUMNS[1]
    sx = FLOW_COLUMNS[2][0]
    centre = {key: {node["name"]: FLOW_TOP + i * FLOW_ROW + FLOW_H // 2
                    for i, node in enumerate(graph[key])}
              for key in ("providers", "jobs", "stores")}
    out = [f'<svg class="flowsvg" id="flow-svg" width="{FLOW_W}" height="{height}"'
           f' viewBox="0 0 {FLOW_W} {height}" role="group" aria-labelledby="flow-svg-title">',
           f'<title id="flow-svg-title">{FLOW_TITLE}</title>',
           "<desc>Providers on the left write through the jobs in the middle into the store files "
           "on the right. Each line is one provider, one job and one store file; pointing at a line "
           "names it and says how many datasets take it. Press a provider or a job to follow its "
           "lines; press it again to show every line.</desc>"]
    for x, _width, header, _key in FLOW_COLUMNS:
        out.append(f'<text class="fh" x="{x + 2}" y="22">{header}</text>')
    segments = []
    total: dict[tuple[str, str], int] = {}
    for edge in graph["edges"]:
        colour = DB_COLORS.get(edge["store"], FLOW_UNKNOWN_COLOR)
        store = f"{edge['store']}.duckdb" if edge["store"] in DB_PILLS else edge["store"]
        said = _html.escape(f"{edge['provider']} → {edge['job']} → {store} · "
                            f"{_flow_count(edge['count'])}")
        provider_y = centre["providers"][edge["provider"]]
        job_y = centre["jobs"][edge["job"]]
        store_y = centre["stores"][edge["store"]]
        for start, start_y, end, end_y, x1, x2 in (
                (f"provider:{edge['provider']}", provider_y, f"job:{edge['job']}", job_y, px + pw, jx),
                (f"job:{edge['job']}", job_y, f"store:{edge['store']}", store_y, jx + jw, sx)):
            segments.append((start, end, start_y, end_y, x1, x2, colour, said))
            total[(start, end)] = total.get((start, end), 0) + 1
    # Two flows that take the same pair of boxes would draw one exact line over the other, and the
    # later colour would hide the earlier one. Each duplicate is nudged sideways, and the nudge
    # shrinks as the bundle grows so every line stays inside the box it leaves and enters.
    seen: dict[tuple[str, str], int] = {}
    for start, end, start_y, end_y, x1, x2, colour, said in segments:
        index = seen.get((start, end), 0)
        seen[(start, end)] = index + 1
        count = total[(start, end)]
        if count > 1:
            shift = (index - (count - 1) / 2) * min(5.0, 24 / (count - 1))
            start_y, end_y = round(start_y + shift, 1), round(end_y + shift, 1)
        out.append(f'<path class="flow-edge" {_attr("data-a", start)} {_attr("data-b", end)}'
                   f' stroke="{colour}" d="{_flow_curve(x1, start_y, x2, end_y)}">'
                   f"<title>{said}</title></path>")
    for key, kind in (("providers", "provider"), ("jobs", "job"), ("stores", "store")):
        x, width = next((c[0], c[1]) for c in FLOW_COLUMNS if c[3] == key)
        out.extend(_flow_node(kind, node["name"], x, FLOW_TOP + i * FLOW_ROW, width, node["count"])
                   for i, node in enumerate(graph[key]))
    out.append("</svg>")
    return "".join(out)


def _flow_legend(store_names: list[str]) -> str:
    """The five store files in the pills' own hues, so a line's colour is named once (G2's palette)."""
    items = "".join(f'<span id="flow-store-{db}"><i style="background:{DB_COLORS[db]}"></i>'
                    f"{db}.duckdb</span>" for db in DB_PILLS)
    extra = ("" if all(name in DB_PILLS for name in store_names) else
             f'<span id="flow-store-unknown"><i style="background:{FLOW_UNKNOWN_COLOR}"></i>'
             "a grey line means the registry names no store file, or names one outside the five</span>")
    return (f'<div class="flowlegend" id="flow-legend"><b>Line colour is the store file:</b>'
            f"{items}{extra}</div>")


def _flow_section(rows: list[dict] | None) -> str:
    """The Data Flow picture, under Board inputs (G6b, #124).

    Rendered from ``rows`` alone — the rows the table already collected — so it adds bytes to the
    page and nothing to Refresh. No rows means no section at all rather than an empty frame; a
    diagram past ``FLOW_MAX_BYTES`` starts collapsed, so the table above always paints first.
    """
    graph = flow_graph(rows)
    if not graph["edges"]:
        return ""
    svg = _flow_svg(graph)
    opened = " open" if len(svg.encode("utf-8")) <= FLOW_MAX_BYTES else ""
    return (
        f'<section id="data-flow"><details class="flow"{opened}>'
        f"<summary>{FLOW_TITLE}</summary>"
        '<div class="fnote">Every dataset in the table above, drawn as the provider it comes from, '
        "the job that writes it, and the store file it lands in. Press a provider or a job to "
        "follow its lines; press it again to show every line.</div>"
        f"{svg}{_flow_legend([node['name'] for node in graph['stores']])}</details></section>")


def _source_cells(row: dict) -> tuple[str, str, str]:
    """Source, Coverage and Fresh. A row the registry does not know is a dash in all three."""
    if row.get("source") is None:
        return DASH, DASH, DASH
    return (_html.escape(source_words(row.get("source"))), _coverage_cell(row), _fresh_cell(row))


# The Fresh record's failed state (``inventory._fresh_record``, S5b): matched as text, the way the
# coverage cell matches "error", because that is what the row carries by the time it reaches here.
FRESH_FAILED = "failed"


def _fresh_cell(row: dict) -> str:
    """The Freshness cell (S5b, G3, #124): what this dataset can claim, in plain words.

    The record ``inventory._fresh_record`` attaches carries the words, so the cell says "no time
    column - see the provenance rows", "taken from the run log, 2026-10-20", "retired - no
    freshness claim made" and so on instead of a bare dash. A failed query is called out in colour
    and says it is not an empty dataset: those two must never look alike. A bare date string is
    still accepted, so a payload collected before this change renders as it always did; a row with
    no record at all (the registry does not know it) keeps the dash. Colour comes from the state
    name, never from the text.
    """
    record = row.get("fresh")
    if isinstance(record, str):
        return _html.escape(record) if record else DASH
    if not isinstance(record, dict):
        return DASH
    text = str(record.get("text") or "").strip()
    if not text:
        return DASH
    escaped = _html.escape(text)
    if record.get("state") == FRESH_FAILED:
        return f'<span class="warnv">{escaped}</span>'
    return escaped


def _writer_cell(row: dict) -> str:
    """The writer's name, or a dash, with the note a spec without a writer carries."""
    writer = row.get("writer")
    out = _html.escape(str(writer)) if writer else DASH
    note = str(row.get("writer_note") or "").strip()
    if note:
        out += f'<span class="hlabel">{_html.escape(note)}</span>'
    return out


# G4 of the GUI brief (#124): the filters above the table. Every axis is a fact the row already
# carries — its store file, its source, its state — so no filter changes what a number means.

# A row whose coverage is below this is called out as needing attention.
ATTENTION_FRACTION = 0.70
# What a coverage with no definable population reads (``inventory.COVERAGE_NA``, S2 / ADR-2).
# Matched as text because that is what a row carries by the time it reaches the table.
COVERAGE_NA = "n/a"
# The state chips, in the order they are offered. A row may answer more than one of them.
STATE_CHIPS = ("needs attention", "no time column", "no denominator")


def _attr(name: str, value) -> str:
    """One escaped HTML attribute. Every ``data-*`` the filters read is written through here."""
    return f'{name}="{_html.escape(str(value), quote=True)}"'


def _db_of(row: dict) -> str:
    """The store file a dataset lives in: the row's own ``db`` when ``_attach_registry`` set it (G2),
    else the registry's, read without a query. Empty when the registry does not know the key."""
    if row.get("db"):
        return str(row["db"])
    try:
        return str(datasets.by_key(str(row.get("key") or "")).db)
    except KeyError:
        return ""


def _states_of(row: dict, errors: list[dict]) -> list[str]:
    """The state chips this row answers, in ``STATE_CHIPS`` order.

    ``needs attention`` — its coverage is below 0.70, or one of its tables failed a query;
    ``no time column`` — it has no last data date, so no freshness can be read from it;
    ``no denominator`` — its coverage has no definable population (``n/a``), which is not 0%.
    """
    coverage = row.get("coverage") or {}
    fraction = coverage.get("fraction")
    states = []
    if _row_errors(row, errors) or (isinstance(fraction, (int, float)) and fraction < ATTENTION_FRACTION):
        states.append("needs attention")
    if str(row.get("last_date") or "").strip() in ("", DASH):
        states.append("no time column")
    if str(coverage.get("label") or "").strip() == COVERAGE_NA:
        states.append("no denominator")
    return states


def _filter_chip(kind: str, value: str) -> str:
    """One filter chip: a real toggle button. ``data-value`` is the machine value the filter compares; the
    visible text is the plain-words name for a source and the value itself for the other axes."""
    val = _html.escape(value, quote=True)
    text = source_words(value) if kind == "source" else value
    return (f'<button type="button" class="fchip" data-chips="{kind}" data-value="{val}" '
            f'aria-pressed="false">{_html.escape(text)}</button>')


def _filter_group(label: str, kind: str, values: list[str]) -> str:
    """One chip group. A group with no value present renders nothing rather than a dead chip."""
    if not values:
        return ""
    lbl = _html.escape(label, quote=True)
    return (f'<span class="flbl">{lbl}</span>'
            f'<div class="fchips" role="group" aria-label="{lbl}">'
            + "".join(_filter_chip(kind, v) for v in values) + "</div>")


def _filter_bar(rows: list[dict], errors: list[dict]) -> str:
    """The filter bar above the inventory table (G4, #124): a find box over the dataset and store
    table names, one chip group per axis, the count line and Clear filters.

    No filter is on by default, and the count line always says how many of the total are shown, so
    a filter can never make a broken row look fixed by hiding it silently. The client half is
    ``settingsFilter``; it reads the ``data-*`` facts ``_table`` puts on each row.
    """
    state_of = [_states_of(r, errors) for r in rows]
    present = {_db_of(r) for r in rows}
    dbs = [d for d in datasets.DBS if d in present]
    sources = sorted({str(r.get("source")) for r in rows if r.get("source")}, key=str.lower)
    states = [s for s in STATE_CHIPS if any(s in got for got in state_of)]
    groups = (_filter_group("Store file", "db", dbs) + _filter_group("Source", "source", sources)
              + _filter_group("State", "state", states))
    shown = len(rows)  # no filter is on, so the count starts at every dataset shown
    return (
        '<div class="filters" id="inv-filters">'
        '<div class="frow"><label class="flbl" for="inv-find">Find a dataset or store table</label>'
        '<input id="inv-find" type="text" autocomplete="off"></div>'
        + (f'<div class="frow">{groups}</div>' if groups else "")
        + f'<div class="frow"><span class="fcount" id="inv-count" role="status">Showing {shown} of '
          f'{shown} datasets</span>'
          '<button type="button" class="fclear" id="inv-clear" disabled>Clear filters</button></div>'
          "</div>")


def _fresh_note_html(row: dict) -> str:
    """The registry's freshness caveat, when it carries one (the ``from_run_log`` #82/#83 note)."""
    note = str(row.get("fresh_note") or "").strip()
    if not note:
        return ""
    return f'<div class="dline"><b>Freshness note</b>{_html.escape(note)}</div>'


# ── G11 (#124): what is missing, and how it improves ─────────────────────────────────────────
# The number is measured (the coverage record) and the interpretation is written (the registry's
# ``gap_kind`` and ``next_step``). Neither is ever the other: no prose here states a count, and a
# gap with no written kind draws no chip rather than a "Complete" it cannot prove.
GAP_WORDS = {
    "fixable": "Can be fixed now",
    "in_build": "Being built",
    "needs_decision": "Waiting for a decision from you",
    "ceiling": "Cannot be improved",
    "self_heals": "Fills in by itself over time",
    "none": "Complete",
}


def _gap_kind(row: dict) -> str:
    """The row's gap kind when it is one of the six, else ``""`` — so a class never comes from data."""
    kind = str(row.get("gap_kind") or "")
    return kind if kind in GAP_WORDS else ""


def _what_is_missing(row: dict) -> str:
    """The measured hole in plain words, from the coverage record alone.

    ``nothing, by design`` for a retired dataset; ``the coverage query failed`` for a failed read;
    ``the coverage could not be read`` when the summary returned no ``Health``; ``no population is
    defined`` when the record carries no denominator; otherwise ``N of M names have no row``, with
    ``N`` zero at full coverage.
    """
    if row.get("retired"):
        return "nothing, by design"
    coverage = row.get("coverage") or {}
    label = str(coverage.get("label") or "").strip()
    if label == "error":
        return "the coverage query failed"
    expected, covered = coverage.get("expected"), coverage.get("covered")
    if coverage.get("fraction") is None or expected is None or covered is None:
        return "the coverage could not be read" if label == "unknown" else "no population is defined"
    missing = max(int(expected) - int(covered), 0)
    return f"{missing:,} of {int(expected):,} names have no row"


def _how_it_improves(row: dict) -> str:
    """The gap kind in words plus its next step; ``—`` when nothing is written down for the row.

    A ``ceiling``'s next step IS its reason, so the two read as one sentence: "Cannot be improved:
    <reason>".
    """
    kind = _gap_kind(row)
    if not kind:
        return DASH
    words = GAP_WORDS[kind]
    step = str(row.get("next_step") or "").strip()
    if kind == "ceiling":
        return f"{words}: {step}" if step else words
    return f"{words}. {step}" if step else words


def _gap_chip(row: dict) -> str:
    """The tag beside the dataset name: the kind's words, and a class from the six, never from data."""
    kind = _gap_kind(row)
    if not kind:
        return ""
    return f'<span class="gap gap-{kind}">{_html.escape(GAP_WORDS[kind])}</span>'


def _detail_row(row: dict, row_id: str, errors: list[dict]) -> str:
    """One dataset's gap, store tables, writer, action, freshness note and notes: closed until its
    name is pressed."""
    tables = ", ".join(_html.escape(str(t)) for t in (row.get("tables") or []))
    note = str(row.get("note") or "").strip()
    return (
        f'<tr class="detail" id="det-{_html.escape(row_id, quote=True)}" hidden>'
        '<td colspan="7">'
        f'<div class="dline"><b>What is missing</b>{_html.escape(_what_is_missing(row))}</div>'
        f'<div class="dline"><b>How it improves</b>{_html.escape(_how_it_improves(row))}</div>'
        f'<div class="dline"><b>Store table(s)</b><span class="tbl">{tables or DASH}</span></div>'
        f'<div class="dline"><b>Writer</b>{_writer_cell(row)}</div>'
        f'<div class="dline"><b>Action</b>{_action_cell(row)}</div>'
        f'{_fresh_note_html(row)}'
        f'<div class="dline"><b>Notes</b>{_html.escape(note) if note else DASH}'
        f'{_master_age_html(row)}</div>'
        f'{_row_errors(row, errors)}'
        "</td></tr>")


# What the Last data date cell reads when the dataset's own table carries no date: the reason is
# the Freshness cell's business, one column over (S5b, G3).
NO_OWN_DATE = "no date of its own"


def _last_date_cell(row: dict) -> str:
    """The Last data date cell: the newest date this dataset's own table carries, or words.

    A bare dash here read the same whether the table has no time column, holds no rows or could not
    be read. The cell says what it knows instead, and the Freshness cell beside it says why.
    """
    date = str(row.get("last_date") or "").strip()
    if _shows_dash(date):
        return NO_OWN_DATE
    return _html.escape(date)


def _table(rows: list[dict], errors: list[dict] | None = None) -> str:
    """The inventory table (S5, #124): Source / Coverage / Fresh lead, then last date, rows, holes.

    Each dataset's store tables, writer, action and notes sit in one closed detail row, opened by
    a button on its name (keyboard reachable). The detail cell wraps, so the table gains no nowrap
    column. ``data-order`` is the rendered (worst coverage first) position, which the group toggle
    uses to restore this order; ``data-source`` is what it groups by.

    ``data-db`` / ``data-source`` / ``data-state`` are the facts the G4 filters read
    (``_filter_bar``); ``data-state`` is a ``|``-separated list.
    """
    errors = errors or []
    body = []
    for order, row in enumerate(rows):
        row_id = str(row.get("key") or f"row-{order}")
        rid = _html.escape(row_id, quote=True)
        source, coverage, fresh = _source_cells(row)
        # A query failure on this row is stated in the detail; the marker keeps it visible here.
        mark = ' <span class="row-err">⚠</span>' if _row_errors(row, errors) else ""
        pill = _db_pill(row.get("db"))
        if pill:
            pill = " " + pill
        chip = _gap_chip(row)
        if chip:
            chip = " " + chip
        body.append(
            f'<tr class="row" id="row-{rid}" data-order="{order}"'
            f" {_attr('data-db', _db_of(row))}"
            f" {_attr('data-source', row.get('source') or '')}"
            f" {_attr('data-state', '|'.join(_states_of(row, errors)))}>"
            f'<td><button type="button" class="det-btn" aria-expanded="false" '
            f'aria-controls="det-{rid}">'
            f'{_html.escape(str(row.get("label") or row.get("key") or ""))}</button>{pill}{chip}{mark}</td>'
            f"<td>{source}</td>"
            f"<td>{coverage}</td>"
            f"<td>{fresh}</td>"
            f'<td>{_last_date_cell(row)}</td>'
            f'<td class="num">{_fmt_rows(row.get("rows"))}</td>'
            f'<td class="num">{_fmt_holes(row)}</td>'
            "</tr>")
        body.append(_detail_row(row, row_id, errors))
    return (
        _filter_bar(rows, errors)
        + '<div class="panel"><table class="inv" id="inv-table">'
        "<thead><tr>"
        "<th>Dataset</th>"
        '<th data-group="source">Source<button type="button" class="grp" aria-pressed="false" '
        'title="Group the rows by source">Group</button></th>'
        "<th>Coverage</th><th>Freshness</th><th>Last data date</th><th>Rows</th><th>Holes</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
        + _db_legend())


def _other_tables_section(rows: list[dict] | None) -> str:
    """S6 (#124): the store tables no dataset claims, with a row count and newest date only.

    An empty list renders nothing at all — no heading, no empty table: the honest intermediate
    state says nothing when there is nothing to say. The sentence states plainly that these
    tables carry no source and no coverage, because the inventory table above cannot speak
    for them.
    """
    if not rows:
        return ""
    body = []
    for row in rows:
        db = str(row.get("db") or "")
        newest = row.get("newest")
        body.append(
            f'<tr><td>{_html.escape(str(row.get("table") or DASH))}</td>'
            f'<td>{_html.escape(f"{db}.duckdb") if db else DASH}</td>'
            f'<td class="num">{_fmt_rows(row.get("rows"))}</td>'
            f'<td>{_html.escape(str(newest)) if newest else "no time column"}</td></tr>')
    return (
        '<section id="other-tables"><div class="bcard" id="other-store-tables">'
        "<h3>Other store tables</h3>"
        '<div class="asof" id="other-tables-note">These store tables are not claimed by any dataset '
        "yet, so they carry no source and no coverage.</div>"
        f'{_mini_table(["Table", "Store file", "Rows", "Newest date"], body)}'
        "</div></section>")


# ── S7 of the Data Health plan (#124): "Identity coverage" ───────────────────────────────────
# Three objects in one card, every number from ``inventory.identity_coverage``: the universe
# funnel (``core.funnel``'s shared panel), the inputs the surviving names carry, and the identifier
# matrix. A row whose read failed says "could not be read"; a number the store holds no denominator
# for says "not measured" — a zero is only ever a real zero.
_STATE_WORDS = {"unreadable": "could not be read", "unmeasured": "not measured"}
_STATE_CLASSES = {"unreadable": "warnv", "unmeasured": "muted"}


def _share_text(covered, expected) -> str:
    """The share of a coverage row, as a whole percentage; ``—`` when there is nothing to divide."""
    if not isinstance(covered, int) or not isinstance(expected, int) or expected <= 0:
        return DASH
    return f"{covered / expected:.0%}"


def _covered_cell(row: dict) -> str:
    """The covered (or count) cell: the number, or the words that say why there is none."""
    state = str(row.get("state") or "")
    if state:
        words = _STATE_WORDS.get(state, state)
        return f'<span class="{_STATE_CLASSES.get(state, "muted")}">{_html.escape(words)}</span>'
    covered = row.get("covered")
    return f"{int(covered):,}" if isinstance(covered, int) else DASH


def _coverage_table(rows: list[dict], heads: tuple[str, str]) -> str:
    """One coverage table: label (with its detail), the count, its denominator and the share."""
    body = []
    for row in rows:
        label = _html.escape(str(row.get("label") or DASH))
        detail = str(row.get("detail") or "").strip()
        if detail:
            label += f'<span class="hlabel">{_html.escape(detail)}</span>'
        body.append(
            f'<tr><td>{label}</td>'
            f'<td class="num" id="cov-{_html.escape(str(row.get("key") or ""), quote=True)}">'
            f'{_covered_cell(row)}</td>'
            f'<td>{_html.escape(str(row.get("denominator") or DASH))}</td>'
            f'<td class="num">{_html.escape(_share_text(row.get("covered"), row.get("expected")))}</td></tr>')
    return _mini_table([*heads, "Out of", "Share"], body)


def _identity_coverage_section(cov: dict | None) -> str:
    """S7 (#124): the identity coverage card — the funnel, the inputs, the identifiers.

    Nothing for a payload without the section, so a store that has not built it renders no heading.
    """
    if not cov:
        return ""
    kept = cov.get("names_kept")
    kept_text = (f"the {int(kept):,} name{'s' if int(kept) != 1 else ''} the boards screen"
                 if isinstance(kept, int) else "the names the boards screen")
    funnel_html = funnel.funnel_panel_html(
        cov.get("funnel"), panel_id="identity-funnel",
        summary="Every drop from the SEC file to the names the boards screen")
    if not funnel_html:
        reason = str(cov.get("funnel_note") or "not measured")
        funnel_html = f'<div class="asof" id="identity-funnel-note">{_html.escape(reason)}</div>'
    return (
        '<section id="identity-coverage"><div class="bcard" id="identity-coverage-card">'
        "<h3>Identity coverage</h3>"
        '<div class="asof" id="identity-coverage-note">How far the stored identity data reaches, '
        "name by name. The funnel starts at the SEC ticker and exchange file and ends at "
        f"{_html.escape(kept_text)}; the two tables below count each input and each identifier over "
        "those names.</div>"
        "<h4>Where names drop out of the universe</h4>"
        f"{funnel_html}"
        "<h4>How many names carry each input</h4>"
        f"{_coverage_table(cov.get('inputs') or [], ('Input', 'Names covered'))}"
        "<h4>Identifier coverage</h4>"
        f"{_coverage_table(cov.get('identifiers') or [], ('Identifier', 'Count'))}"
        "</div></section>")


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


# ── #162: the IBKR Gateway login STATE card ─────────────────────────────────────────────────────
# The pair of the "IBKR gateway" shortcut: that one is a one-line link near the top that logs you in,
# this one sits with the other availability cards and says whether that login is still good. The
# shortcut keeps its documented place above the help block; this card is several lines, so it belongs
# down here beside Discord readiness and Backups. The one availability fact whose history matters, because the
# gateway's session expires and is logged in again by hand, so the card answers "still logged in?"
# and "if not, since when?". Every value comes from ``core.ibkr_login.collect``, which reads the
# login check's record and opens no socket, so this card costs the Refresh nothing. Every field goes
# through ``esc``: ``checked_detail`` carries the probe's own words, and those carry the host out of
# the environment.
# Its ids are the ``ibkr-state-*`` namespace, deliberately clear of the shortcut's ``ibkr-login-*``:
# the two cards share a page, and one id in two places is invalid HTML and an assertion that passes
# on the wrong card.
_IBKR_STATES = {"up": "st-ok", "down": "st-bad", "silent": "st-warn",
                "unknown": "st-warn", "not set": "st-warn"}
_IBKR_CHANGE_WORDS = {"up": "Logged in", "down": "Login needed",
                      "silent": "Port open, the API is not answering"}


def _ibkr_kv(key: str, label: str, value: str) -> str:
    return f'<div class="kv" id="ibkr-state-{key}">{label}: {_html.escape(value)}</div>'


def _ibkr_changes(changes: list[dict]) -> str:
    """The recorded state changes, oldest first, so a weekly login cycle reads as a cycle."""
    body = []
    for c in changes:
        word = _IBKR_CHANGE_WORDS.get(str(c.get("to")), str(c.get("to") or ""))
        cls = _IBKR_STATES.get(str(c.get("to")), "st-warn")
        body.append(f"<tr><td>{_html.escape(_friendly(c.get('at')))}</td>"
                    f'<td class="{cls}">{_html.escape(word)}</td></tr>')
    if not body:
        return '<div class="kv" id="ibkr-state-changes">No change has been recorded yet.</div>'
    return _mini_table(["When", "Changed to"], body)


def _ibkr_state_card(ibkr: dict | None) -> str:
    """The IBKR Gateway login state: the state, since when, the last login, and the recent changes.

    Without the key (an older payload, or a read that failed and was recorded in the errors) there is
    no card. The card is drawn even when the check is switched off, so the switch stays discoverable.
    """
    if not ibkr:
        return ""
    esc = _html.escape
    state = str(ibkr.get("state") or "unknown")
    cls = _IBKR_STATES.get(state, "st-warn")
    lines = [f'<div class="kv" id="ibkr-state-text"><span class="{cls}">{esc(str(ibkr.get("text") or ""))}</span></div>']
    if ibkr.get("for_text"):
        lines.append(_ibkr_kv("for", "This state has held for", str(ibkr["for_text"])))
    if ibkr.get("since"):
        lines.append(_ibkr_kv("since", "Since", _friendly(ibkr.get("since"))))
    if state != "up" and ibkr.get("last_up"):
        lines.append(_ibkr_kv("last-up", "Last logged in", _friendly(ibkr.get("last_up"))))
    if state == "up" and ibkr.get("server_version") is not None:
        lines.append(_ibkr_kv("version", "API server version", str(ibkr["server_version"])))
    if ibkr.get("checked_text"):
        cls_checked = "st-warn" if ibkr.get("stale") else ""
        lines.append(f'<div class="kv" id="ibkr-state-checked"><span class="{cls_checked}">'
                     f'{esc(str(ibkr["checked_text"]))}</span></div>')
    if ibkr.get("checked_detail"):
        lines.append(f'<div class="kv note" id="ibkr-state-detail">{esc(str(ibkr["checked_detail"]))}</div>')
    changes = _ibkr_changes(ibkr.get("changes") or []) if state not in ("not set",) else ""
    return ('<section id="ibkr-state"><div class="bcard" id="ibkr-state-card">'
            "<h3>IBKR Gateway login state</h3>"
            '<div class="asof" id="ibkr-state-note">Read from the login check\'s own record, not from the gateway: '
            "this page opens no connection. The check itself opens the port and sends the API version "
            "handshake, and nothing else - no account, position, order or execution is ever asked for. "
            f'The gateway it watches is {esc(str(ibkr.get("target") or "")) or "not configured"}.</div>'
            + "".join(lines) + changes + "</div></section>")


_BACKUP_STATES = {"ok": "st-ok", "warn": "st-warn", "bad": "st-bad", "unknown": "st-warn", "not set": "st-bad"}


def _gb(nbytes) -> str:
    return f"{nbytes / 1e9:.2f} GB" if isinstance(nbytes, (int, float)) else "—"


def _size_text(nbytes) -> str:
    """A size for a sentence: bytes below a megabyte, else GB with two decimals (the unit the card already uses)."""
    if not isinstance(nbytes, (int, float)):
        return "unknown"
    if nbytes < 1_000_000:
        return f"{int(nbytes):,} bytes"
    return f"{nbytes / 1e9:.2f} GB"


def _retention_from_description(description: str) -> str:
    """"keeps 5 daily raw and 5 daily history snapshots, ..." — the retention clause of the backup job's registry
    description, cut out of the text itself so the numbers are read here and never retyped in the renderer."""
    text = str(description or "")
    start = text.find("keeps ")
    if start < 0:
        return ""
    end = text.find(".", start)
    return text[start:end + 1 if end >= 0 else len(text)].strip()


def _retention_rule(b: dict) -> str:
    """The retention rule in plain words: the record's own text when it carries one, else the registry
    description of RC-Store-Backup (``core.automations``, the job's own entry), else "unknown"."""
    rule = str(b.get("retention") or "").strip()
    if rule:
        return rule
    try:
        rule = _retention_from_description(automations.entry("RC-Store-Backup").description)
    except Exception:  # noqa: BLE001 - a registry without the entry says so on the card, never fails the page
        rule = ""
    return rule or "unknown: the job's record names no retention rule"


def _backups_card(b: dict | None) -> str:
    """The Backups card (#309, G8, G9): where the backups are (the local folder in full, and the off-machine
    location the job's own record names), the retention rule, the last integrity self-check in words, what that
    rule marks expired (a read-only dry run of the LOCAL folder, ``inventory._backups``), and a delete control that
    is disabled in the markup itself. Nothing here deletes: the local folder is listed read-only and the
    off-machine copy is never opened. Without that key there is no card; a missing record reads "unknown"."""
    if not b:
        return ""
    esc = _html.escape

    def cell(part: dict) -> str:
        cls = _BACKUP_STATES.get(part.get("state"), "")
        return f'<span class="{cls}">{esc(str(part.get("text") or part.get("state") or "unknown"))}</span>'

    loc, off = b.get("local") or {}, b.get("offmachine") or {}
    cp, scan = b.get("checkpoints") or {}, b.get("scan") or {}
    expired = b.get("expired") or {}
    folder = str(b.get("folder") or "").strip() or "unknown"
    offmachine_at = str(b.get("offmachine_path") or "").strip() or "unknown"
    checked = scan.get("checked")
    selfcheck = (f'last checked {scan.get("last_day") or "never"}, '
                 f'next due {scan.get("next_due") or "the next backup run"}, '
                 + (f"{_fmt_rows(checked)} files re-read against their hashes" if isinstance(checked, int)
                    else "no files re-read yet" if not scan.get("last_day")
                    else "the number of files re-read was not recorded"))
    # G9 (#124): the totals of the dry run. A scan that could not read the folder carries no counts at all, so the
    # line reads "unknown" and never a zero it did not measure.
    exp_n, exp_b = expired.get("count"), expired.get("bytes")
    if expired.get("state") == "ok" and isinstance(exp_n, int) and isinstance(exp_b, int):
        expired_text = (f"The retention rule marks {exp_n:,} file{'' if exp_n == 1 else 's'} as expired, "
                        f"{_size_text(exp_b)} in total. This is a dry run: nothing has been deleted.")
    else:
        why = str(expired.get("why") or "").strip()
        expired_text = (f"unknown: {why}" if why else "unknown: the local backup folder could not be read")
        expired_text += "; this page makes no claim about expired files."
    delete_row = (
        '<div class="kv" id="backup-delete-row">'
        '<button type="button" class="set-btn" id="backup-delete-expired" disabled '
        'title="Arming the delete is a separate change: it needs your typed confirmation, a fresh backup and no '
        'job running.">Delete expired backups</button> '
        '<span id="backup-delete-note">Disabled: this page only lists what the retention rule marks expired. '
        "Arming a delete is a separate change and needs your typed confirmation.</span></div>"
    )
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
    summary = (
        f'<div class="kv">Local backup folder: <code class="bpath" id="backup-folder">{esc(folder)}</code></div>'
        '<div class="kv">Off-machine copy location: '
        f'<code class="bpath" id="backup-offmachine-path">{esc(offmachine_at)}</code></div>'
        f'<div class="kv" id="backup-retention">How long backups are kept: {esc(_retention_rule(b))}</div>'
        f'<div class="kv" id="backup-selfcheck">Integrity self-check: {esc(selfcheck)}</div>'
        f'<div class="kv" id="backup-expired">Expired backups, dry run: {esc(expired_text)}</div>'
        f'{delete_row}'
        '<div class="kv" id="backup-nodelete">Nothing on this page deletes or moves a backup file: it reads the '
        "backup job's own records and shows them.</div>"
    )
    return ('<section id="backups"><div class="bcard" id="backups-card"><h3>Backups</h3>'
            '<div class="asof" id="backups-note">Read from the nightly backup job\'s own records in the local backup '
            "folder (shown below), plus one read-only listing of that local folder for the expired-backup dry run; "
            "the off-machine (Drive) copy is never read, walked or listed. The "
            "self-check re-reads the local folder against the hashes taken when each file was copied, every 7 days "
            "by default.</div>"
            f'{summary}'
            f'<div class="kv" id="backup-recorded">Sizes recorded {esc(str(recorded).replace("T", " ").replace("+00:00", " UTC") if recorded else "never")}</div>'
            f'{_mini_table(["Backup", "Size / result", "Files", "Detail"], rows)}</div></section>')


def _ibkr_login_href(value) -> str | None:
    """The gateway login address to link to, or ``None`` when there is nothing to show.

    Only an absolute ``http``/``https`` address with a host counts. Anything else — a
    ``javascript:`` or ``data:`` value, a bare path, a scheme with no host — is treated as not
    configured, so a mistyped or hostile variable cannot become a live link on the page.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    return raw if parts.scheme in ("http", "https") and parts.netloc else None


def _ibkr_login_card(value) -> str:
    """The "IBKR gateway" shortcut (avo, 23 Oct 2026): one link to the gateway's own login screen.

    The address is never in the code: it is ``RC_IBKR_LOGIN_URL``, so an unset variable shows no
    card at all and this repository pins no host. The page only writes the link; it makes no
    request to it.
    """
    href = _ibkr_login_href(value)
    if href is None:
        return ""
    esc = _html.escape
    return ('<section id="ibkr-login"><div class="bcard" id="ibkr-login-card"><h3>IBKR gateway</h3>'
            f'<div class="kv"><a class="set-btn" id="ibkr-login-link" href="{esc(href, quote=True)}" '
            'target="_blank" rel="noopener noreferrer">Open the IBKR gateway login screen</a></div>'
            '<div class="asof" id="ibkr-login-note">The address comes from <code>RC_IBKR_LOGIN_URL</code>. '
            'This page makes no call to it and shows nothing when the variable is unset; only an absolute '
            'http or https address is accepted.</div></div></section>')


def render_settings_dashboard(payload: dict) -> str:
    payload = payload or {}
    rows = payload.get("rows") or []
    generated = payload.get("generated_at")

    help_block = help_html(
        "This is an ops inventory of the local <b>.store/</b>: one row per dataset with its row count, "
        "newest data date and known holes. <b>Source</b> is the provider the registry names, <b>Coverage</b> "
        "is the stored rows against the population they should cover (<b>n/a</b> when there is no population "
        "to count), and <b>Freshness</b> is what freshness the registry lets that dataset claim: its newest "
        "date, or the words for the one thing it can say instead — <b>no time column</b> (the date lives in "
        "a related table), <b>taken from the run log</b> (a worked-out table, dated by the job that wrote "
        "it), <b>retired</b> or <b>store bookkeeping</b> (no claim is made), <b>no date stored yet</b> (the "
        "table holds no rows), or <b>query failed</b> (a broken read, which is not an empty dataset). Press "
        "a dataset's "
        "name to open its store tables, writer and <b>Fetch</b> action. <b>Refresh</b> re-reads the store "
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
    # G7 (#124): avo kept the drill, below the dataset table, carrying everything. It keeps its
    # own search box (the filter kit's combobox) and its ``id="drill"`` anchor.
    drill = (
        '<div id="ticker-drill">'
        '<h2 class="sec">Ticker detail</h2>'
        '<div class="frow drillpick"><span class="flbl" id="ticker-combo-label">Ticker</span>'
        + combobox_html("ticker-combo", "ticker-combo-label", "Search ticker or name") + "</div>"
        '<div class="drill" id="drill" hidden>'
        '<div class="drill-grid" id="drill-body">'
        '<div class="dempty">Pick a ticker to see its stored data.</div>'
        "</div></div></div>"
    )

    identity_html = _identity_card(payload.get("identity"))
    identity_coverage_html = _identity_coverage_section(payload.get("identity_coverage"))
    other_tables_html = _other_tables_section(payload.get("other_tables"))
    discord_html = _discord_card(payload.get("discord"))
    backups_html = _backups_card(payload.get("backups"))
    ibkr_login_html = _ibkr_login_card(payload.get("ibkr_login_url"))
    ibkr_state_html = _ibkr_state_card(payload.get("ibkr_login_state"))
    errors_html = _errors_list(payload.get("errors") or [])
    boards_html = _boards_section(payload.get("boards") or [], rows)
    flow_html = _flow_section(rows)
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
  {ibkr_login_html}
  {help_block}
  {_summary_tiles(rows, payload)}
  {body}
  {drill}
  {identity_coverage_html}
  {other_tables_html}
  {identity_html}
  {discord_html}
  {ibkr_state_html}
  {errors_html}
  {backups_html}
  {automations_html}
  {boards_html}
  {flow_html}
</div>
<script>{FILTERS_JS}</script>
<script>
var DATA = {data_json};
var AUTOMATIONS = {script_json(automations)};
{JS}
{FLOW_JS}
{DRILL_JS}
{automations_js}
</script>
</body>
</html>
"""
