"""Relatedness board — one focal combobox + top peers from LLM business profiles.

Chrome follows docs/dashboard_ui.md §6: search + count, the shared filter bar
(``rc_filters``; Theme is its facet over the pinned run's ``theme_ids``, #383 C1b),
then **one** ticker combobox. One "Most related to"
panel — no Company A / Company B selects. Each peer row carries the cosine
score + percentile, the 1/3/5/10-day move and residual-vs-SPY correlation, and
coarse/medium/fine cluster chips when the store has them.
"""
from __future__ import annotations

import html as _html
from datetime import datetime

from RevenueChaser.core.html_safe import fill, script_json
from RevenueChaser.module_comovement import config as comov_config
from RevenueChaser.core.reporting_common import (
    FILTERS_CSS,
    FILTERS_JS,
    combobox_html,
    filters_bar_html,
    filters_meta,
    help_html,
    provider_health_html,
    recorded_sessions_html,
    watchlist_html,
)

CSS = """
:root{--bg:#05111F;--panel:#12161e;--panel2:#1a1f2b;--border:#232a38;
--text:#d7dce5;--muted:#8a93a6;--accent:#0EA5E9;--green:#2ecc71;--amber:#f1c40f;--neon:#00e6a8;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5}
.wrap{max-width:100%;margin:0 auto;padding:20px 24px 60px}
.topbar{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:10px;border-bottom:1px solid var(--border);padding-bottom:12px;margin-bottom:14px}
.topbar h1{margin:0;font-size:22px;letter-spacing:-.4px}
.topbar .sub{color:var(--muted);margin-top:2px;font-size:13px}
.sync{color:var(--muted);font-size:12.5px;white-space:nowrap}
.banner{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:16px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.stat .v{font-size:22px;font-weight:800}
.stat .l{color:var(--text);font-size:12px;font-weight:600;margin-top:2px}
.stat .d{color:var(--muted);font-size:11px}
.controls{display:flex;flex-direction:column;gap:8px;margin-bottom:16px}
.frow{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.flbl{color:var(--muted);font-size:12px;font-weight:600}
select,input[type=text],input[type=number]{background:#10151f;border:1px solid #2a3345;color:var(--text);border-radius:6px;height:34px;padding:0 10px;font:500 13px/1 inherit}
input[type=text]{flex:1;min-width:200px}
.count{color:var(--muted);font-size:12px;margin-left:auto;white-space:nowrap}
.pick .rcf-combo{width:300px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px}
.panel h2{margin:0 0 4px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.panel h2 .nm{text-transform:none;color:var(--text);font-size:15px;letter-spacing:0}
.peer{border-top:1px solid var(--panel2);padding:9px 0}
.peer:first-child{border-top:none}
.peer-head{font-size:14.5px}
.tk{font-weight:750;min-width:64px;letter-spacing:-.2px}
.peer-name{color:var(--text)}
.pct{color:var(--muted);font-size:12.5px;font-variant-numeric:tabular-nums}
.prow-move,.prow-cluster{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums;margin-top:3px}
.prow-move .v,.prow-cluster .on{color:var(--text);font-weight:600}
.tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.tag{background:var(--panel2);border:1px solid var(--border);border-radius:6px;padding:1px 7px;font-size:11.5px;color:var(--muted)}
.empty{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:24px;color:var(--muted);text-align:center}
.badge-stale{display:inline-block;margin-left:6px;padding:0 6px;border:1px solid var(--amber);border-radius:6px;color:var(--amber);font-size:11px;font-weight:600}
.note{color:var(--muted);font-size:12px;margin-top:12px}
"""


def _friendly(as_of: str) -> str:
    try:
        dt = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        return dt.strftime("%A, %B %d, %Y EOD")
    except (ValueError, TypeError):
        return str(as_of or "")


def _summary_html(payload: dict) -> str:
    n = payload.get("n") or 0
    pending = payload.get("pending") or 0
    no_text = payload.get("no_text") or 0
    pairs = payload.get("n_pairs") or len(payload.get("scores") or [])
    # The names without filing text are not pending (#388 D4): they wait for a 10-K text, not for the next run.
    no_text_line = f'<div class="d">{no_text:,} without filing text</div>' if no_text else ""
    return "".join([
        f'<div class="stat"><div class="v">{n}</div>'
        f'<div class="l">Profiled</div><div class="d">business profiles</div></div>',
        f'<div class="stat"><div class="v">{pending}</div>'
        f'<div class="l">Pending</div><div class="d">next off-peak run</div>{no_text_line}</div>',
        f'<div class="stat"><div class="v">{payload.get("universe_total") or 0}</div>'
        f'<div class="l">Universe</div><div class="d">operating companies, no market-cap floor</div></div>',
        f'<div class="stat"><div class="v">{pairs}</div>'
        f'<div class="l">Pairs scored</div><div class="d">cosine sample</div></div>',
    ])


# Company keys the page script never reads (REL-4, #388): the term weights and the raw profile were about a third of
# the page. The engine payload, relatedness_latest.json and the result history keep them.
_EMBED_SKIP = frozenset({"weights", "profile"})


def _embed_companies(companies: list[dict]) -> list[dict]:
    """The companies as the page embeds them: every key but ``_EMBED_SKIP`` (the caller's payload is not touched)."""
    return [{k: v for k, v in c.items() if k not in _EMBED_SKIP} for c in companies]


def render_relatedness_dashboard(payload: dict) -> str:
    payload = payload or {}
    companies = payload.get("companies") or []
    peers = payload.get("peers") or {}
    sync = _friendly(payload.get("as_of"))
    model = _html.escape(str(payload.get("model") or ""))
    n = payload.get("n") or 0
    body = f"""
  <div class="banner">{_summary_html(payload)}</div>
  <div class="controls">
    <div class="frow">
      <label class="flbl" for="q">Search</label>
      <input type="text" id="q" placeholder="Search ticker or name…" oninput="onQuery()" aria-label="Search ticker or name" spellcheck="false" autocomplete="off">
      <span class="count" id="count" aria-live="polite"></span>
    </div>
  </div>
  {filters_bar_html("rc-filters")}
  <div class="controls">
    <div class="frow pick">
      <span class="flbl" id="ticker-combo-label">Ticker</span>
      {combobox_html("ticker-combo", "ticker-combo-label", "Search ticker or name")}
    </div>
  </div>
  <div class="panel">
    <h2 id="panel-title">Most related to</h2>
    <div class="pct" id="meta"></div>
    <div id="peers" aria-live="polite"></div>
  </div>"""

    JS = r"""
var DATA = __COMPANIES__;
var PEERS = __PEERS__;
var META = __META__;
var QUERY = "";
var FOCAL = null;
var RCF = null;
var BY = {}; DATA.forEach(function(c){ BY[c.ticker] = c; });
// The shared filter bar (rc_filters, #383 C1b) plus the board's own search box narrow the focal picker.
function pass(c){
  if(RCF && !RCF.pass(c)) return false;
  if(QUERY){
    var q = QUERY.toLowerCase();
    if((c.ticker||"").toLowerCase().indexOf(q) < 0 && (c.name||"").toLowerCase().indexOf(q) < 0) return false;
  }
  return true;
}
function onQuery(){
  QUERY = (document.getElementById("q").value || "").trim();
  render();
}
function onFocal(ticker){
  if(ticker) FOCAL = ticker;
  render();
}
function rebuildOptions(){
  var vis = DATA.filter(pass);
  if(!vis.some(function(c){ return c.ticker === FOCAL; })){
    FOCAL = vis.length ? vis[0].ticker : null;
  }
  var input = document.getElementById("ticker-combo");
  if(input){
    var item = vis.filter(function(c){ return c.ticker === FOCAL; })[0] || null;
    input.value = item ? item.ticker + " — " + (item.name || item.ticker) : "";
  }
  document.getElementById("count").textContent = vis.length + " names";
}
function tags(list){ return (list||[]).map(function(t){ return '<span class="tag">'+rcEsc(t)+'</span>'; }).join(""); }
function fmtMove(p){
  if(!p.move) return "";
  var parts = [];
  [1,3,5,10].forEach(function(n){
    var v = p.move[String(n)];
    if(v == null) return;
    parts.push(n + 'd ' + Number(v).toFixed(2));
  });
  if(!parts.length) return "";
  var line = 'move  ' + parts.join('   ');
  if(p.beyond_spy != null){
    line += '     beyond SPY ' + Number(p.beyond_spy).toFixed(2) + ' (__BEYOND_H__d)';
  }
  return '<div class="prow-move">' + line + '</div>';
}
function fmtCluster(p){
  if(!p.cluster) return "";
  var levels = ['coarse','medium','fine'];
  var out = [];
  levels.forEach(function(lvl){
    if(!(lvl in p.cluster)) return;
    var on = p.cluster[lvl];
    out.push('<span class="' + (on ? 'on' : '') + '">' + (on ? '●' : '○') + ' ' +
             lvl.charAt(0).toUpperCase() + lvl.slice(1) + '</span>');
  });
  if(!out.length) return "";
  return '<div class="prow-cluster">cluster  ' + out.join('  ') + '</div>';
}
function staleBadge(t){
  return (BY[t] && BY[t].stale) ? '<span class="badge-stale" title="Profiled under an older model/prompt; refreshes on the next run">stale</span>' : "";
}
function peerRow(p){
  var name = p.name || "";
  return '<div class="peer">' +
    '<div class="peer-head"><span class="tk">' + rcEsc(p.peer) + '</span>' +
    (name ? '<span class="peer-name"> — ' + rcEsc(name) + '</span>' : '') + staleBadge(p.peer) + '</div>' +
    rcWatchRow(p.peer) +
    '<div class="pct">' + Number(p.score||0).toFixed(2) + ' · p' + p.percentile + '</div>' +
    fmtMove(p) + fmtCluster(p) +
    '<div class="tags">' + tags(p.shared) + '</div>' +
    '</div>';
}
function render(){
  rebuildOptions();
  var meta = document.getElementById("meta");
  var panel = document.getElementById("panel-title");
  var body = document.getElementById("peers");
  if(!FOCAL){
    meta.textContent = "";
    panel.textContent = "Most related to";
    body.innerHTML = '<div class="empty">' + (DATA.length
      ? "No companies match the current filters."
      : "No profiles yet. Run <b>Fetch</b> to build business profiles.") + '</div>';
    return;
  }
  var c = BY[FOCAL] || {};
  panel.innerHTML = "Most related to " + rcEsc(FOCAL) +
    (c.name ? ' <span class="nm">— ' + rcEsc(c.name) + '</span>' : "") + staleBadge(FOCAL) + rcWatchButtons(FOCAL);
  meta.textContent =
    (c.sector || "") + (c.industry ? " · " + c.industry : "") +
    (c.theme_coarse ? " · " + c.theme_coarse : "");
  var list = PEERS[FOCAL] || [];
  body.innerHTML = list.map(peerRow).join("") || '<div class="empty">No peers.</div>';
}
function init(){
  RCF = RCFilters.mount({root: "#rc-filters", rows: DATA, meta: META, onChange: function(){ render(); }});
  RCInfo.wire();
  var input = document.getElementById("ticker-combo");
  if(input) RCCombobox.mount({input: input, list: "#ticker-combo-list", rows: DATA, filter: pass, clear: true,
    onPick: function(r){ onFocal(r.ticker); }});
  FOCAL = (DATA[0] || {}).ticker || null;
  render();
}
"""

    JS = fill(JS, {"__BEYOND_H__": str(int(comov_config.DEFAULT_HORIZON)),
                   "__COMPANIES__": script_json(_embed_companies(companies)),
                   "__PEERS__": script_json(peers),
                   "__META__": script_json(filters_meta(
                       "relatedness", earnings_mode="filter",
                       themes=payload.get("theme_catalog") or {}))})

    help_block = help_html(
        "Each company was read from its latest 10-K/20-F and summarized into business "
        "attributes (products, end markets, business model, customers, value-chain role). "
        "Relatedness is how much two companies' attributes overlap — it measures "
        "<b>similar business</b>, not co-movement (see Co-movement) or trading links "
        f"(see Value Chain). {n} companies · model <b>{model}</b>. <b>Fetch</b> (the off-peak run) "
        "downloads missing filings from SEC and asks the LLM for new profiles; <b>Refresh</b> rebuilds "
        "the board from the stored profiles and scores with no network call.")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Relatedness</title>
<style>{CSS}</style>
{FILTERS_CSS}
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>Relatedness</h1>
      <div class="sub">Business similarity from 10-K Item 1 / 20-F Item 4 — not price, not supply chain</div>
    </div>
    <div class="sync">Last sync: {_html.escape(sync)}</div>
  </div>
  {provider_health_html(payload.get("provider_health"))}
  {help_block}
  {recorded_sessions_html("module_relatedness")}
  {body}
</div>
<script>{FILTERS_JS}</script>
{watchlist_html("relatedness")}
<script>{JS}
init();
</script>
</body>
</html>
"""
