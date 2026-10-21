"""Implied Probability (OIPD) board — self-contained dark-theme HTML renderer.

Shows what the options market implies about where a ticker's price can be at
each fitted expiry: a probability fan across maturities, the risk-neutral PDF
and CDF for one expiry, the fitted SVI smile, and tail-probability queries.
Charts are Plotly (from arrays computed in Python); the page needs no network
beyond the Plotly CDN already used by the other boards.
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime

from RevenueChaser.core.config import PLOTLY_CDN_URL
from RevenueChaser.core.html_safe import script_json
from RevenueChaser.core.reporting_common import (
    FILTERS_CSS,
    FILTERS_JS,
    combobox_html,
    filters_bar_html,
    filters_meta,
    help_html,
    provider_health_html,
    recorded_sessions_html,
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
.warn{background:rgba(241,196,15,.10);border:1px solid rgba(241,196,15,.35);color:var(--amber);border-radius:10px;padding:9px 14px;margin-bottom:14px;font-size:12.5px;display:none}
.err{background:rgba(231,76,60,.10);border:1px solid rgba(231,76,60,.4);color:#ff9b8f;border-radius:10px;padding:14px 16px;margin-bottom:14px;font-size:13px}
.banner{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.stat .v{font-size:22px;font-weight:800;font-variant-numeric:tabular-nums}
.stat .l{color:var(--text);font-size:12px;font-weight:600;margin-top:2px}
.stat .d{color:var(--muted);font-size:11px}
.controls{display:flex;flex-direction:column;gap:8px;margin-bottom:16px}
select,input[type=text],input[type=number]{background:#10151f;border:1px solid #2a3345;color:var(--text);border-radius:6px;height:34px;padding:0 10px;font:500 13px/1 inherit}
input[type=text]{flex:1;min-width:200px}
.count{color:var(--muted);font-size:12px;margin-left:auto;white-space:nowrap}
.pick .rcf-combo{width:300px}
.share-px{font-size:18px;font-weight:800;font-variant-numeric:tabular-nums;color:var(--text);margin-left:4px}
.share-px .d{display:block;color:var(--muted);font-size:11px;font-weight:600}
body.loading:after{content:"Refreshing…";position:fixed;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(5,17,31,.72);color:var(--text);font-weight:700;letter-spacing:.04em;z-index:99}
.frow{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.flbl{color:var(--muted);font-size:12px;font-weight:600}
.tabs{display:inline-flex;background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden;flex-wrap:wrap}
.tab{padding:0 14px;height:34px;line-height:34px;cursor:pointer;font-weight:600;font-size:13px;color:var(--muted);border-right:1px solid var(--border);user-select:none}
.tab:last-child{border-right:none}
.tab.on{background:var(--panel2);color:var(--text)}
.tab .cnt{color:var(--muted);font-size:11px;font-weight:400}
.tab.on .cnt{color:var(--neon)}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px;margin-bottom:16px}
.panel h2{margin:0 0 10px;font-size:15px;letter-spacing:-.2px}
.pgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:12px}
.kpi{background:var(--panel2);border:1px solid var(--border);border-radius:9px;padding:9px 11px}
.kpi .v{font-size:18px;font-weight:800;font-variant-numeric:tabular-nums}
.kpi .l{color:var(--muted);font-size:11px;font-weight:600;margin-top:1px}
.muted{opacity:.45;filter:grayscale(1)}
.pos{color:var(--green)}.neg{color:var(--red)}.acc{color:var(--accent)}.neu{color:var(--text)}
.qrow{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:10px}
.qrow input{background:#10151f;border:1px solid #2a3345;color:var(--text);border-radius:6px;height:34px;padding:0 10px;font:500 13px/1 inherit;width:140px}
.qout{font-size:13px;color:var(--muted)}
.qout b{color:var(--text);font-variant-numeric:tabular-nums}
.chart{width:100%}
table.mini{border-collapse:collapse;width:100%;font-size:12px}
table.mini th,table.mini td{padding:5px 8px;border-bottom:1px solid var(--border);text-align:right}
table.mini th:first-child,table.mini td:first-child{text-align:left;color:var(--muted)}
.empty{color:var(--muted);font-size:13px;padding:20px;text-align:center}
.disclaimer{color:var(--muted);font-size:11.5px;margin-top:8px;line-height:1.6}
"""

JS = r"""
var PLOTCFG={responsive:true,displayModeBar:false};
var FAN_COLORS={outer:'rgba(14,165,233,0.15)',inner:'rgba(14,165,233,0.30)'};
var FOCAL=null;
var RCF=null;
function baseLayout(extra){
  var l={paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',
    font:{color:'#d7dce5',size:12},
    margin:{l:56,r:56,t:44,b:44},
    xaxis:{gridcolor:'#232a38',zerolinecolor:'#232a38'},
    yaxis:{gridcolor:'#232a38',zerolinecolor:'#232a38'},
    legend:{orientation:'h',y:1.12,x:0},
    hovermode:'x unified'};
  for(var k in extra){ l[k]=extra[k]; }
  return l;
}
function fanChart(){
  var f=PAYLOAD.fan; if(!f||!f.expiries.length){ return; }
  var x=f.dte, q=f.quantiles;
  var tr=[
    {x:x,y:q['0.95'],type:'scatter',mode:'lines',line:{width:0},hoverinfo:'skip',showlegend:false},
    {x:x,y:q['0.05'],type:'scatter',mode:'lines',line:{width:0},fill:'tonexty',fillcolor:FAN_COLORS.outer,name:'5–95%',hoverinfo:'skip'},
    {x:x,y:q['0.75'],type:'scatter',mode:'lines',line:{width:0},hoverinfo:'skip',showlegend:false},
    {x:x,y:q['0.25'],type:'scatter',mode:'lines',line:{width:0},fill:'tonexty',fillcolor:FAN_COLORS.inner,name:'25–75%',hoverinfo:'skip'},
    {x:x,y:q['0.50'],type:'scatter',mode:'lines+markers',line:{color:'#00e6a8',width:3},marker:{size:8},name:'median',
     hovertemplate:'%{x}d · median %{y:.2f}<extra></extra>'}
  ];
  if(PAYLOAD.spot){
    tr.push({x:[x[0],x[x.length-1]],y:[PAYLOAD.spot,PAYLOAD.spot],type:'scatter',mode:'lines',
      line:{color:'#f1c40f',width:1.5,dash:'dot'},name:'spot',
      hovertemplate:'spot '+PAYLOAD.spot+'<extra></extra>'});
  }
  Plotly.newPlot('fan',tr,baseLayout({height:340,title:'Market-implied price fan',
    xaxis:{title:'Days to expiry',gridcolor:'#232a38'},yaxis:{title:'Price at expiry',gridcolor:'#232a38'}}),PLOTCFG);
}
function distChart(e){
  var tr=[
    {x:e.curve.S,y:e.curve.pdf,type:'scatter',mode:'lines',fill:'tozeroy',
     fillcolor:'rgba(14,165,233,0.18)',line:{color:'#0EA5E9',width:2},name:'PDF',
     hovertemplate:'%{x:.2f} · density %{y:.5f}<extra></extra>'},
    {x:e.curve.S,y:e.curve.cdf,type:'scatter',mode:'lines',
     line:{color:'#f1c40f',width:2},name:'CDF',yaxis:'y2',
     hovertemplate:'%{x:.2f} · P(S<X) %{y:.3f}<extra></extra>'}
  ];
  var l=baseLayout({height:340,title:'Risk-neutral distribution',
    xaxis:{title:'Price at expiry',gridcolor:'#232a38'},
    yaxis:{title:'Density',gridcolor:'#232a38'},
    yaxis2:{title:'Cumulative',overlaying:'y',side:'right',range:[0,1],
            gridcolor:'rgba(0,0,0,0)'},hovermode:'x'});
  Plotly.newPlot('dist',tr,l,PLOTCFG);
}
function smileChart(e){
  var s=e.smile;
  var tr=[
    {x:s.k_mkt,y:s.iv_mkt,type:'scatter',mode:'markers',
     marker:{color:'#8a93a6',size:5},name:'Market IV',
     hovertemplate:'k %{x:.3f} · IV %{y:.3f}<extra></extra>'},
    {x:s.k_fit,y:s.iv_fit,type:'scatter',mode:'lines',
     line:{color:'#00e6a8',width:2},name:'SVI fit'}
  ];
  Plotly.newPlot('smile',tr,baseLayout({height:300,title:'SVI volatility smile',
    xaxis:{title:'log(K / F)',gridcolor:'#232a38'},
    yaxis:{title:'Implied vol',tickformat:'.0%',gridcolor:'#232a38'},hovermode:'closest'}),PLOTCFG);
}
function pct(x){ return (x==null)?'—':(x*100).toFixed(1)+'%'; }
function num(x,d){ return (x==null)?'—':Number(x).toFixed(d==null?2:d); }
function cls(x,mid){ if(x==null)return 'neu'; return x>=mid?'pos':'neg'; }
function kpis(e){
  var bad=!!e.density_flag, m=bad?' muted':'';
  return [
    ['Median',num(e.quantiles.median),'50th percentile','acc'+m],
    ['P5–P95',num(e.quantiles.p05,0)+' – '+num(e.quantiles.p95,0),'5–95% band','neu'+m],
    ['P(S&lt;spot)',pct(e.prob_below_spot),'below '+num(PAYLOAD.spot,2),(bad?'neu':cls(e.prob_below_spot,0.5))+m],
    ['P(S&gt;spot)',pct(e.prob_above_spot),'above '+num(PAYLOAD.spot,2),(bad?'neu':cls(e.prob_above_spot,0.5))+m],
    ['P(up 10%)',pct(e.p_up10),'vs spot',(bad?'neu':'pos')+m],
    ['P(down 10%)',pct(e.p_down10),'vs spot',(bad?'neu':'neg')+m],
    ['ATM IV',pct(e.atm_iv),'at the forward','neu'+m],
    ['Skew g-min',num(e.density_g_min_shown,3),e.density_note,bad?'neg':'pos']
  ].map(function(r){
    return '<div class="kpi"><div class="v '+r[3]+'">'+r[1]+'</div>'+
           '<div class="l">'+r[0]+'</div><div class="l" style="color:var(--muted);font-weight:400">'+r[2]+'</div></div>';
  }).join('');
}
function quantTable(e){
  var q=e.quantiles;
  var rows=[['5%',q.p05],['10%',q.p10],['25%',q.p25],['50% (median)',q.median],
            ['75%',q.p75],['90%',q.p90],['95%',q.p95]];
  return '<table class="mini'+(e.density_flag?' muted':'')+'"><tbody>'+rows.map(function(r){
    return '<tr><td>'+r[0]+'</td><td>'+num(r[1])+'</td></tr>'; }).join('')+
    '<tr><td>Forward</td><td>'+num(e.forward)+'</td></tr>'+
    '<tr><td>Mean (RND)</td><td>'+num(e.mean)+'</td></tr>'+
    (e.density_flag?'<tr><td colspan="2">'+rcEsc(e.density_note)+': statistics unreliable</td></tr>':'')+
    '</tbody></table>';
}
function greeksTable(e){
  var g=e.greeks||{};
  if(!g.iv) return '<div class="empty">No ATM Greeks.</div>';
  var rows=[['IV (ATM)',pct(g.iv)],['Delta (call)',num(g.delta,3)],
            ['Gamma',num(g.gamma,4)],['Vega /1%',num(g.vega,4)],['Theta /day',num(g.theta,4)]];
  return '<table class="mini"><tbody>'+rows.map(function(r){
    return '<tr><td>'+r[0]+'</td><td>'+r[1]+'</td></tr>'; }).join('')+'</tbody></table>';
}
function interp(xs,ys,x){
  if(!xs.length) return null;
  if(x<=xs[0]) return ys[0];
  if(x>=xs[xs.length-1]) return ys[ys.length-1];
  for(var i=1;i<xs.length;i++){
    if(x<=xs[i]){ var t=(x-xs[i-1])/(xs[i]-xs[i-1]); return ys[i-1]+t*(ys[i]-ys[i-1]); }
  }
  return ys[ys.length-1];
}
function query(){
  var e=PAYLOAD.expiries[IDX]; if(!e) return;
  var box=document.getElementById('qx');
  var x=parseFloat(box.value);
  var out=document.getElementById('qout');
  if(!isFinite(x)){ out.innerHTML='Enter a price to read the implied odds.'; return; }
  var cdf=interp(e.curve.S,e.curve.cdf,x);
  if(cdf==null){ out.innerHTML='—'; return; }
  out.innerHTML='P(S &lt; '+x+') = <b>'+pct(cdf)+'</b> &nbsp;·&nbsp; P(S &gt; '+x+') = <b>'+pct(1-cdf)+'</b>';
}
function tabs(){
  var el=document.getElementById('exp-tabs');
  el.innerHTML=PAYLOAD.expiries.map(function(e,i){
    return '<div class="tab'+(i===IDX?' on':'')+'" onclick="selectExpiry('+i+')">'+
      rcEsc(e.date.slice(5))+' <span class="cnt">'+rcEsc(e.dte)+'d</span></div>'; }).join('');
}
function selectExpiry(i){
  IDX=i; tabs();
  var e=PAYLOAD.expiries[i]; if(!e) return;
  document.getElementById('kpis').innerHTML=kpis(e);
  document.getElementById('qtable').innerHTML=quantTable(e);
  document.getElementById('gtable').innerHTML=greeksTable(e);
  document.getElementById('exp-title').textContent=
    e.date+' · '+e.dte+' days · forward '+num(e.forward)+' · '+e.n+' strikes';
  distChart(e); smileChart(e);
  var box=document.getElementById('qx');
  if(!box.value) box.value=num(PAYLOAD.spot,2);
  query();
}
// The shared filter bar (rc_filters, #383 C1b) only narrows the picker pool; it never slices the fitted PDF.
function pass(c){ return RCF ? RCF.pass(c) : true; }
function rebuildOptions(){
  var vis = DATA.filter(pass);
  if(!FOCAL){
    FOCAL = vis.length ? vis[0].ticker : (DEFAULT_TICKER || null);
  }
  var input = document.getElementById("ticker-combo");
  if(input){
    var item = vis.filter(function(c){ return c.ticker === FOCAL; })[0] || null;
    input.value = item ? item.ticker + " — " + (item.name || item.ticker) : (FOCAL || "");
  }
}
function render(){ rebuildOptions(); }
function onPick(ticker){
  if(!ticker) return;
  window.OIPD_TICKER = ticker;
  document.body.dataset.ticker = ticker;
  document.body.classList.add("loading");
  location.search = "?refresh=1&ticker=" + encodeURIComponent(ticker);
}
function init(){
  RCF = RCFilters.mount({root: "#rc-filters", rows: DATA, meta: META, onChange: function(){ render(); }});
  RCInfo.wire();
  var input = document.getElementById("ticker-combo");
  if(input) RCCombobox.mount({input: input, list: "#ticker-combo-list", rows: DATA, filter: pass, allowFree: true, clear: true,
    onPick: function(r){ onPick(r.ticker); }});
  FOCAL = DEFAULT_TICKER || null;
  render();
  window.OIPD_TICKER = FOCAL || "";
  document.body.dataset.ticker = window.OIPD_TICKER;
  if(PAYLOAD.error){
    document.getElementById('err').style.display='block';
    document.getElementById('err').textContent=PAYLOAD.error;
    return;
  }
  if(!PAYLOAD.expiries.length){
    document.getElementById('body').innerHTML=
      '<div class="empty">No expiry could be fitted for '+rcEsc(PAYLOAD.ticker)+'. '+
      'Run a Fetch, or check the warnings above.</div>';
    return;
  }
  if(PAYLOAD.warnings && PAYLOAD.warnings.length){
    var w=document.getElementById('warn');
    w.style.display='block';
    w.innerHTML='⚠ '+PAYLOAD.warnings.map(rcEsc).join('<br>⚠ ');
  }
  tabs(); fanChart(); selectExpiry(0);
  document.getElementById('qx').addEventListener('keydown',function(ev){
    if(ev.key==='Enter'){ query(); } });
}
"""


def _share_price(spot) -> str:
    """Last share close used as the options spot, e.g. ``$100.00``."""
    try:
        if spot is None:
            return "—"
        return f"${float(spot):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _friendly(asof: str | None) -> str:
    """'Wednesday, September 23, 2026 EOD' from an ISO day."""
    if not asof:
        return "—"
    try:
        d = datetime.fromisoformat(asof[:10])
        return d.strftime("%A, %B %d, %Y") + " EOD"
    except ValueError:
        return asof


def _filter_universe() -> list[dict]:
    """The shared classification universe with the universal-filter fields.

    The OIPD filters only narrow the ticker combobox — they never slice the
    fitted PDF — so this is the picker's pool, not a board filter.
    """
    from RevenueChaser.core.universe import classification_universe, filter_fields
    tickers = classification_universe()
    fields = filter_fields(tickers)
    return [fields[t] for t in tickers if t in fields]


def _theme_catalog() -> dict:
    """The pinned theme run's catalog for the Theme facet (store read; empty on a fresh store)."""
    from RevenueChaser.core.universe import theme_catalog
    return theme_catalog()


def render_oipd_dashboard(payload: dict) -> str:
    payload = payload or {}
    focal = (payload.get("ticker") or "").strip().upper()
    ticker = focal or "—"
    spot = payload.get("spot")
    expiries = payload.get("expiries") or []
    error = payload.get("error")
    asof = payload.get("asof")
    universe = _filter_universe()
    px = _share_price(spot)

    chrome = f"""  {filters_bar_html("rc-filters")}
  <div class="controls">
    <div class="frow pick">
      <span class="flbl" id="ticker-combo-label">Ticker</span>
      {combobox_html("ticker-combo", "ticker-combo-label", "Search ticker or name")}
    <span class="share-px" id="share-px">{px}<span class="d">Share price</span></span>
    </div>
  </div>"""

    if error:
        body = (f'<div class="err" id="err" style="display:block">{_html.escape(error)}</div>'
                '<div class="empty">Nothing to show yet.</div>')
    else:
        body = f"""
  <div class="warn" id="warn"></div>
  <div class="banner">
    <div class="stat"><div class="v acc">{_html.escape(ticker)}</div><div class="l">Ticker</div>
      <div class="d">Unusual Whales chain</div></div>
    <div class="stat"><div class="v">{px}</div><div class="l">Share price</div>
      <div class="d">last close · {_html.escape(asof or '—')}</div></div>
    <div class="stat"><div class="v">{len(expiries)}</div><div class="l">Expiries fitted</div>
      <div class="d">of {_html.escape(str(payload.get('n_contracts', 0)))} contracts</div></div>
    <div class="stat"><div class="v">{_html.escape(str(payload.get('horizon_days') or '—'))}</div><div class="l">Horizon (days)</div>
      <div class="d">r = {_html.escape(str(payload.get('risk_free')))}</div></div>
  </div>
  <div id="body">
    <div class="panel">
      <h2>Probability fan</h2>
      <div id="fan" class="chart"></div>
    </div>
    <div class="controls">
      <div class="frow"><span class="flbl">Expiry</span><div class="tabs" id="exp-tabs"></div></div>
    </div>
    <div class="panel">
      <h2 id="exp-title">—</h2>
      <div class="kpis" id="kpis"></div>
      <div class="pgrid">
        <div><div id="dist" class="chart"></div></div>
        <div><div id="smile" class="chart"></div></div>
      </div>
      <div class="qrow">
        <span class="flbl">P(S &lt; X)</span>
        <input type="number" id="qx" placeholder="price" aria-label="Price level">
        <button onclick="query()">Compute</button>
        <span class="qout" id="qout">Enter a price to read the implied odds.</span>
      </div>
      <div class="pgrid" style="margin-top:14px">
        <div class="panel" style="margin:0"><h2>Quantiles</h2><div id="qtable"></div></div>
        <div class="panel" style="margin:0"><h2>ATM Greeks (Black-76)</h2><div id="gtable"></div></div>
      </div>
      <div class="disclaimer">
        <b>Risk-neutral, not a forecast.</b> These probabilities are consistent with option prices after
        discounting — they are <i>not</i> the real-world odds of the stock ending at a price. Equity options are
        American and this board does not de-americanise them, so index products (e.g. SPX) fit the model more
        cleanly than single names. A negative <b>g-min</b> flags a butterfly-arbitrage patch in the fitted smile.
      </div>
    </div>
  </div>"""

    help_block = help_html(
        "Option prices already contain a view of the future. This board fits an "
        "<b>SVI volatility smile</b> to the listed chain at each expiry, then converts the fitted "
        "smile into a <b>risk-neutral probability distribution</b> (Breeden–Litzenberger / Gatheral). "
        "The <b>fan</b> shows the 5–95% and 25–75% price bands across maturities; the <b>median</b> "
        "is the 50th percentile. Use the expiry tabs to inspect one distribution's PDF/CDF, its "
        "fitted smile, and tail probabilities. All figures are <b>risk-neutral</b> — a pricing "
        "convention, not a real-world forecast. <b>Fetch</b> pulls the picked ticker's option chain "
        "from Unusual Whales; <b>Refresh</b> re-fits the stored chain with no network call.")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Implied Probability — {_html.escape(ticker)}</title>
<script src="{PLOTLY_CDN_URL}"></script>
<style>{CSS}</style>
{FILTERS_CSS}
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>Implied Probability</h1>
      <div class="sub">What the options market implies about where {_html.escape(ticker)} can be at each expiry</div>
    </div>
    <div class="sync">Last sync: {_html.escape(_friendly(asof))}</div>
  </div>
  {provider_health_html(payload.get("provider_health"))}
  {help_block}
  {recorded_sessions_html("module_oipd")}
{chrome}
{body}
</div>
<script>{FILTERS_JS}</script>
<script>
var PAYLOAD = __PAYLOAD__;
var DATA = __UNIVERSE__;
var META = __META__;
var DEFAULT_TICKER = __DEFAULT_TICKER__;
var IDX = 0;
{JS}
init();
</script>
</body>
</html>
"""
    # One pass, so a data value that spells another placeholder is never
    # re-substituted; every embedded JSON has <, > and & escaped so a string cannot
    # close the <script> block (M1: the ticker arrives from a GET link).
    subs = {"__UNIVERSE__": script_json(universe),
            "__META__": script_json(filters_meta("oipd", earnings_mode="filter", themes=_theme_catalog())),
            "__DEFAULT_TICKER__": script_json(focal),
            "__PAYLOAD__": script_json(payload)}
    return re.sub("|".join(map(re.escape, subs)), lambda m: subs[m.group(0)], html)


