"""Value Chain board — Explorer: pick a ticker, see upstream / downstream / 2-hop.

The 2-hop graph is computed server-side per ticker (``engine.focal_graph``) and
embedded, so the client only draws it. Evidence (a plain-language sentence + the
filing snippet + form + date) sits next to every edge — without it the sankey is
just a word cloud.

Readability is deliberate: a legend with direction arrows (not colour alone), a
"what am I looking at?" panel, and an explicit note that v1 relations are
auto-extracted and can be wrong.
"""
from __future__ import annotations

from datetime import datetime

from RevenueChaser.core.config import PLOTLY_CDN_URL
from RevenueChaser.core.gics_map import sector_or_unclassified, sector_path
from RevenueChaser.core.html_safe import esc, fill, script_json
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

from . import engine
from . import precision

CSS = """
:root{--bg:#05111F;--panel:#12161e;--panel2:#1a1f2b;--border:#232a38;
--text:#d7dce5;--muted:#8a93a6;--accent:#0EA5E9;--green:#2ecc71;--red:#e74c3c;--amber:#f1c40f;--neon:#00e6a8;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5}
.wrap{max-width:100%;margin:0 auto;padding:20px 24px 60px}
.topbar{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:10px;border-bottom:1px solid var(--border);padding-bottom:12px;margin-bottom:14px}
.topbar h1{margin:0;font-size:22px;letter-spacing:-.4px}
.topbar .sub{color:var(--muted);margin-top:2px;font-size:13px}
.sync{color:var(--muted);font-size:12.5px;white-space:nowrap}
.legend{display:flex;flex-wrap:wrap;gap:16px;align-items:center;margin-bottom:14px;font-size:12.5px;color:var(--muted)}
.legend b{color:var(--text)}
.arrow{font-weight:700}
.a-sup{color:var(--accent)} .a-foc{color:var(--neon)} .a-cus{color:var(--green)}
.banner{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:16px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.stat .v{font-size:22px;font-weight:800}
.stat .l{color:var(--text);font-size:12px;font-weight:600;margin-top:2px}
.stat .d{color:var(--muted);font-size:11px}
.stat.warn{border-color:rgba(241,196,15,.45)} .stat.warn .v{color:var(--amber)}
.controls{display:flex;flex-direction:column;gap:8px;margin-bottom:16px}
.frow{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.flbl{color:var(--muted);font-size:12px;font-weight:600}
.pick .rcf-combo{width:300px}
select,input[type=text],input[type=number]{background:#10151f;border:1px solid #2a3345;color:var(--text);border-radius:6px;height:34px;padding:0 10px;font:500 13px/1 inherit}
input[type=text]{flex:1;min-width:200px}
.count{color:var(--muted);font-size:12px;margin-left:auto;white-space:nowrap}
.chip{display:inline-block;background:var(--panel2);border:1px solid var(--border);border-radius:20px;padding:1px 9px;font-size:11px;color:var(--muted)}
.chip.warn{background:rgba(241,196,15,.12);border-color:rgba(241,196,15,.4);color:var(--amber)}
.chip.share{background:rgba(14,165,233,.12);border-color:rgba(14,165,233,.4);color:var(--accent);font-weight:700}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media (max-width:1000px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px}
.panel h2{margin:0 0 10px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.edge{border-top:1px solid var(--panel2);padding:9px 0}
.edge:first-child{border-top:none}
.rel{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;font-weight:700;margin-right:6px}
.r-supplier{background:rgba(79,157,255,.16);color:var(--accent)}
.r-customer{background:rgba(46,204,113,.16);color:var(--green)}
.sentence{font-weight:600}
.snip{color:var(--muted);font-size:12.5px;margin-top:3px}
.meta{color:var(--muted);font-size:11px;margin-top:3px}
.empty{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:24px;color:var(--muted);text-align:center}
.note{color:var(--muted);font-size:12px;margin-top:12px;line-height:1.6}
"""


def _friendly(as_of: str) -> str:
    try:
        dt = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        return dt.strftime("%A, %B %d, %Y EOD")
    except (ValueError, TypeError):
        return str(as_of or "")


def _graphs(payload: dict, tickers: list[str]) -> dict:
    edges = payload.get("edges") or []
    index = engine.edge_index(edges)   # once, not per ticker (#118 C19)
    return {t: engine.focal_graph(edges, t, depth=2, index=index) for t in tickers}


def _precision_tile(rep: dict | None) -> str:
    """Measured precision of supplier/customer links on the hand-labelled fixture."""
    rep = rep or {}
    p = rep.get("precision")
    cover = ""
    if rep.get("links") is not None:
        cover = f' · labels cover {rep.get("covered") or 0} of {rep["links"]} links'
    if p is None:
        return ('<div class="stat"><div class="v">—</div><div class="l">Precision</div>'
                f'<div class="d">not measured{cover}</div></div>')
    n = rep.get("n") or 0
    # Below the minimum sample the gate is not judged (#118 C27).
    small = rep.get("status") == "insufficient sample" or n < precision.MIN_LABELED_N
    cls = "stat" if rep.get("passes") and not small else "stat warn"
    gate = f"insufficient sample (min {precision.MIN_LABELED_N})" if small else "gate 70%"
    detail = esc(f"hand-checked n={n} · {gate}{cover}")
    return (f'<div class="{cls}"><div class="v">{round(p * 100)}%</div>'
            f'<div class="l">Precision</div>'
            f'<div class="d">{detail}</div></div>')


def _summary_html(payload: dict, n_filings: int, n_drawn: int) -> str:
    fetch = payload.get("fetch") or {}
    pending = payload.get("pending")
    if pending is None:
        pending = fetch.get("pending") or 0
    # Tickers left out of Pending (#116 C26): a recorded miss or no CIK.
    pending_d = "not yet fetched"
    misses, no_cik = int(payload.get("misses") or 0), int(payload.get("no_cik") or 0)
    if misses:
        pending_d += f" · {misses} with no 10-K / 20-F"
    if no_cik:
        pending_d += f" · {no_cik} with no CIK"
    return "".join([
        f'<div class="stat"><div class="v">{n_filings}</div>'
        f'<div class="l">Filings</div><div class="d">latest 10-K / 20-F</div></div>',
        f'<div class="stat"><div class="v">{n_drawn}</div>'
        f'<div class="l">Links drawn</div><div class="d">named near supplier / customer words</div></div>',
        f'<div class="stat"><div class="v">{payload.get("universe_total") or 0}</div>'
        f'<div class="l">Universe</div><div class="d">operating companies, no market-cap floor</div></div>',
        f'<div class="stat"><div class="v">{pending}</div>'
        f'<div class="l">Pending</div><div class="d">{esc(pending_d)}</div></div>',
        _precision_tile(payload.get("precision")),
    ])


def _picker_rows(filings: list[dict], extra: set[str]) -> list[dict]:
    """Every filing plus mention-src tickers, with filter fields for the picker."""
    by = {f["ticker"]: f for f in filings}
    rows = []
    for t in sorted(set(by) | extra):
        f = by.get(t) or {}
        rows.append({
            "ticker": t,
            "name": f.get("name") or t,
            "sector": sector_or_unclassified(f.get("sector")),
            "industry": f.get("industry") or "",
            "market_cap": f.get("market_cap"),
            "earnings_date": f.get("earnings_date"),
            "index": f.get("index") or [],
            # The shared filter row (#383 C1b): the sector path and the pinned theme run's ids.
            "gics": f.get("gics") or sector_path(sector_or_unclassified(f.get("sector")), f.get("industry") or ""),
            "theme_ids": f.get("theme_ids") or [],
            "form": f.get("form"),
            "filed": f.get("filed"),
            "quality": f.get("quality"),
        })
    return rows


def _default_focal(tickers: list[str], edges: list[dict]) -> str | None:
    """The visible ticker with the most edges (deterministic tie-break: name)."""
    degree = {t: 0 for t in tickers}
    for e in edges:
        s, d = e.get("src"), e.get("dst_ticker")
        if s in degree:
            degree[s] += 1
        if d in degree:
            degree[d] += 1
    if not degree:
        return None
    return sorted(degree, key=lambda t: (-degree[t], t))[0]


def render_value_chain_dashboard(payload: dict) -> str:
    payload = payload or {}
    filings = payload.get("filings") or []
    edges = payload.get("edges") or []
    sync = _friendly(payload.get("as_of"))
    informed = set()
    for e in edges:
        if e.get("relation") in ("supplier", "customer"):
            informed.update(t for t in (e.get("src"), e.get("dst_ticker")) if t)
    extra = informed
    picker = [r for r in _picker_rows(filings, extra) if r["ticker"] in informed]
    tickers = [r["ticker"] for r in picker]
    graphs = _graphs(payload, tickers)
    default_focal = _default_focal(tickers, edges)
    shown = set(tickers)
    filing_meta = {f["ticker"]: f for f in filings if f.get("ticker") in shown}
    n_drawn = sum(1 for e in edges if e.get("relation") in ("supplier", "customer"))
    summary = _summary_html(payload, len(filings), n_drawn)

    body = f"""
  <div class="banner">{summary}</div>
  <div class="legend">
    <span><span class="arrow a-sup">▲</span> <b>Supplier</b> — sells to the focal company</span>
    <span><span class="arrow a-foc">●</span> <b>Focal</b> — the company you picked</span>
    <span><span class="arrow a-cus">▼</span> <b>Customer</b> — buys from the focal company</span>
  </div>
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
    <div class="frow"><span class="chip" id="fmeta"></span></div>
  </div>
  <div class="grid">
    <div class="panel"><h2>Supply chain</h2><div id="sankey" style="height:520px"></div></div>
    <div class="panel"><h2>Evidence</h2><div id="edges"></div></div>
  </div>"""

    JS = r"""
var DATA = __DATA__;
var GRAPHS = __GRAPHS__;
var FILINGS = __FILINGS__;
var DEFAULT_FOCAL = __DEFAULT_FOCAL__;
var META = __META__;
var FOCAL = null;
var QUERY = "";
var RCF = null;
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
function options(){
  FOCAL = (DEFAULT_FOCAL && GRAPHS[DEFAULT_FOCAL]) ? DEFAULT_FOCAL : ((DATA[0]||{}).ticker || null);
  rebuildOptions();
}
function sentence(l){
  var s = l.src, d = l.dst;
  if(l.relation === "supplier") return s + "'s filing names " + d + " as a possible supplier";
  if(l.relation === "customer") return s + "'s filing names " + d + " as a possible customer";
  return s + " mentions " + d;
}
function suspicious(l){
  // The chip names the list the snippet reads like, so it never says "customer list" on a customer edge (DF60).
  var s = (l.snippet || "").toLowerCase();
  if(l.relation === "supplier" && s.indexOf("customer") >= 0) return "may be a customer list";
  if(l.relation === "customer" && (s.indexOf("supplier") >= 0 || s.indexOf("purchase") >= 0)) return "may be a supplier list";
  return "";
}
// #387 D7: the share of the filer's (src's) sales (a customer) or purchases (a supplier) its filing states.
function hasShare(l){ return typeof l.share_pct === "number" && isFinite(l.share_pct); }
function shareText(l){
  if(!hasShare(l)) return "";
  return l.share_pct + "% of " + l.src + "'s " + (l.relation === "supplier" ? "purchases" : "sales");
}
function render(){
  rebuildOptions();
  if(!FOCAL){
    var none = '<div class="empty">' + (DATA.length
      ? "No companies match the current filters."
      : "No companies with supplier / customer edges. Run <b>Fetch (EDGAR)</b> if filings are missing.") + "</div>";
    document.getElementById("sankey").innerHTML = none;
    document.getElementById("edges").innerHTML = none;
    document.getElementById("fmeta").innerHTML = "";
    return;
  }
  var g = GRAPHS[FOCAL] || {links: []};
  var links = g.links || [];
  var fm = FILINGS[FOCAL] || {};
  var q = fm.quality || "";
  document.getElementById("fmeta").innerHTML = rcWatchRow(FOCAL) +     // the focal company's pair (PR 4)
    (fm.form ? rcEsc(fm.form) + " · filed " + rcEsc(fm.filed) + " · " : "") +
    (q === "ok" ? "text: ok" : (q ? '<span class="chip warn">text: ' + rcEsc(q) + ' — item not fully extracted</span>' : ""));
  drawSankey(FOCAL, links);
  drawEdges(links);
}
function drawSankey(focal, links){
  var nodes = [focal], idx = {}; idx[focal] = 0;
  links.forEach(function(l){
    [l.src, l.dst].forEach(function(n){ if(!(n in idx)){ idx[n] = nodes.length; nodes.push(n); } });
  });
  var src = links.map(function(l){ return idx[l.src]; });
  var dst = links.map(function(l){ return idx[l.dst]; });
  var val = links.map(function(){ return 1; });
  var colors = links.map(function(l){
    return l.relation === "supplier" ? "rgba(79,157,255,.5)" : "rgba(46,204,113,.5)";
  });
  var nodeColors = nodes.map(function(n){
    if(n === focal) return "#00e6a8";
    return "#1a1f2b";
  });
  var data = [{ type: "sankey", orientation: "h", arrangement: "snap",
    node: { label: nodes, pad: 12, thickness: 14, color: nodeColors,
            font: { color: "#d7dce5", size: 11 } },
    link: { source: src, target: dst, value: val, color: colors } }];
  var layout = { paper_bgcolor: "#12161e", plot_bgcolor: "#12161e",
                 font: { color: "#d7dce5" }, margin: { l: 8, r: 8, t: 8, b: 8 } };
  if(window.Plotly) Plotly.newPlot("sankey", data, layout, { displayModeBar: false, responsive: true });
}
function drawEdges(links){
  var el = document.getElementById("edges");
  if(!links.length){ el.innerHTML = '<div class="empty">No supplier / customer edges found for this company.</div>'; return; }
  var order = { supplier: 0, customer: 1 };
  // Within a hop, the edges whose filing states a share come first, largest share first (#387 D7).
  links.sort(function(a,b){
    return (a.hop - b.hop) || (hasShare(b) - hasShare(a)) ||
      (hasShare(a) && hasShare(b) ? b.share_pct - a.share_pct : 0) || (order[a.relation]-order[b.relation]);
  });
  el.innerHTML = links.map(function(l){
    var why = suspicious(l);
    var flag = why ? ' <span class="chip warn">⚠ ' + why + '</span>' : '';
    var share = hasShare(l) ? ' <span class="chip share">' + rcEsc(shareText(l)) + '</span>' : '';
    return '<div class="edge"><div><span class="rel r-'+rcEsc(l.relation)+'">'+rcEsc(l.relation)+'</span>' +
      '<span class="sentence">'+rcEsc(sentence(l))+'</span>' + share +
      (l.hop === 2 ? ' <span class="chip">2-hop</span>' : '') + flag + '</div>' +
      '<div class="snip">“'+rcEsc(l.snippet)+'”</div>' +
      '<div class="meta">auto-extracted, unverified · source: 10-K/20-F, '+rcEsc(FILINGS[l.src] ? FILINGS[l.src].filed : "")+'</div></div>';
  }).join("");
}
function init(){
  RCF = RCFilters.mount({root: "#rc-filters", rows: DATA, meta: META, onChange: function(){ render(); }});
  RCInfo.wire();
  var input = document.getElementById("ticker-combo");
  if(input) RCCombobox.mount({input: input, list: "#ticker-combo-list", rows: DATA, filter: pass, clear: true,
    onPick: function(r){ onFocal(r.ticker); }});
  options(); render();
}
"""

    JS = fill(JS, {"__DATA__": script_json(picker),
                   "__GRAPHS__": script_json(graphs),
                   "__DEFAULT_FOCAL__": script_json(default_focal or ""),
                   "__FILINGS__": script_json(filing_meta),
                   "__META__": script_json(filters_meta(
                       "value_chain", earnings_mode="filter",
                       themes=payload.get("theme_catalog") or {}))})

    help_block = help_html(
        "Edges are sentences from the company's latest <b>10-K Item 1</b> (or "
        "<b>20-F Item 4</b>) that name another company. The arrow shows the direction: "
        "a <b>supplier</b> sells to the focal company, a <b>customer</b> buys from it. "
        "A <b>2-hop</b> edge is one step further out (a supplier of your supplier). "
        "A <b>percentage</b> on an edge is the share of the filer's sales (a customer) or purchases (a supplier) "
        "that the filing itself states; those edges are listed first, largest share first. "
        "Only a fixed list of about 50 large public companies is recognised by name; "
        "any other counterparty is not shown. <b>Fetch (EDGAR)</b> is cache-first: it downloads a "
        "filing from SEC only for a name with none stored (newer 10-Ks come from the 10-K sync on "
        "Settings); <b>Refresh</b> redraws the board from the stored filings with no network call.")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Value Chain</title>
<script src="{PLOTLY_CDN_URL}"></script>
<style>{CSS}</style>
{FILTERS_CSS}
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>Value Chain</h1>
      <div class="sub">Supplier / customer edges stated in the latest 10-K / 20-F</div>
    </div>
    <div class="sync">Last sync: {esc(sync)}</div>
  </div>
  {provider_health_html(payload.get("provider_health"))}
  {help_block}
  {recorded_sessions_html("module_value_chain")}
  {body}
  <div class="note">
    ⚠ v1 uses keyword rules on filing text: a link means the filing names the company
    near a supplier / customer word, not that the relationship is confirmed. A
    hand-checked sample found most such links wrong, so read the source sentence
    before relying on one.
  </div>
</div>
<script>{FILTERS_JS}</script>
{watchlist_html("value_chain")}
<script>{JS}
init();
</script>
</body>
</html>
"""
