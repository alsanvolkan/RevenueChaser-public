"""SEC Form 13F dashboard — true total institutional ownership (all filers).

Renders a sortable, sector-filterable table of per-ticker quarterly totals
sourced from SEC 13F bulk data (includes BlackRock/Vanguard/State Street).

Inputs:
  ``totals`` = ``{ticker: {period: {shares, value, holders, new_holders,
                 exited_holders, top10_pct, hhi, buy_flow, sell_flow}}}``
  ``meta``  = ``{ticker: {"name": str, "sector": str}}``
  ``enrichment`` = ``{ticker: {price, shares_outstanding, split_factor, float_shares}}`` (optional): the snapshot
                   inputs, ``enrich.load_inputs`` (#375 F2: ``ownership_snapshot`` and D01; #732: the split factor
                   per quarter end from the prices store).

Every share count on the board is on today's share basis (#732): a quarter's shares and flows are divided by that
quarter end's split factor and its close multiplied by it, so a split reads as what it is, not as buying. The board
shows the last ``WINDOW_QUARTERS`` settled quarters (owner, 12 Nov 2026), plus a quarter still arriving after them.
"""
from RevenueChaser.core.reporting_common import (FILTERS_CSS, FILTERS_JS, PAYLOAD_JS, THEME_CSS,
                                                 THEME_JS, THEME_SCALE, THEME_TOKENS, filters_bar_html,
                                                 filters_meta, help_html, notice_html, provider_health_html,
                                                 recorded_sessions_html, watchlist_html, write_board)
import math
from datetime import datetime
from pathlib import Path

from RevenueChaser.core import universe
from RevenueChaser.core.config import OUTPUT_DIR, PLOTLY_CDN_URL
from RevenueChaser.core.gics_map import UNCLASSIFIED, canonical_sector, sector_or_unclassified
from RevenueChaser.core.html_safe import esc, fill, script_json
from RevenueChaser.module_sec_13f import engine


def _row_sector(raw) -> str:
    """The sector label of a meta row: a Morningstar name, an asset class (crypto, indices) or ``Unclassified``.

    A row stored before the rename (``Information Technology``, ``Health Care``, ``Financials``) reads as its
    Morningstar sector, so it gets the chip of the new sector list instead of a label no chip matches.
    """
    label = sector_or_unclassified(raw)
    known = canonical_sector(label)
    return label if known == UNCLASSIFIED else known


def _kit_rows(entries: list[dict]) -> tuple[list[dict], str | None]:
    """The filter-kit rows (K1 contract) for every ticker on the board, and a notice when the read failed.

    A failed ``filter_fields`` read never fails a render: the rows go out with ticker and name only (KF default).
    """
    names = {e["ticker"]: (e.get("name") or "").strip() or e["ticker"] for e in entries}
    try:
        fields = universe.filter_fields(list(names))
        rows = [fields.get(t) or {"ticker": t, "name": n} for t, n in names.items()]
        return rows, None
    except Exception:  # noqa: BLE001 - the board must render without the store
        return ([{"ticker": t, "name": n} for t, n in names.items()],
                "The filter fields (index, market cap, sector, theme) could not be read, so the filters "
                "have nothing to match on. The board itself is unaffected.")


def _kit_themes() -> dict:
    try:
        return universe.theme_catalog()
    except Exception:  # noqa: BLE001 - an empty catalog just leaves the Theme facet empty
        return {}


# The board's own rules, on the shared theme's tokens (#761): ``THEME_CSS`` defines every ``var(--…)`` here, so the
# board follows the shell's light or dark choice instead of carrying a palette of its own. The aliases on ``:root``
# give the shared filter kit and history box (which read ``--panel``, ``--text``, ``--input`` …) the same tokens.
CSS = """<style>
:root{--panel:var(--card);--panel2:var(--card-2);--text:var(--fg);--input:var(--input-bg);--input-b:var(--input-border);
  --neon:var(--pos);--rcf-pop-bg:var(--card)}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font-family:var(--font-sans);padding:var(--sp-5);font-size:13px;
  font-variant-numeric:tabular-nums}
.header{margin-bottom:var(--sp-4);padding-bottom:var(--sp-4);border-bottom:1px solid var(--border)}
.header h1{color:var(--bright);font-size:var(--fs-lg);font-weight:600;letter-spacing:-.01em}
.header .meta{color:var(--muted);font-size:var(--fs-sm);margin-top:var(--sp-1)}
.board-search{margin:10px 0}
.board-search input{background:var(--input-bg);color:var(--fg);border:1px solid var(--input-border);padding:0 12px;
  height:36px;border-radius:var(--r-sm);font:inherit;font-size:var(--fs-sm);width:260px;max-width:100%}
.board-search input:focus{outline:2px solid var(--focus);outline-offset:1px;border-color:var(--focus)}
details.types{background:var(--card);border:1px solid var(--border);border-radius:var(--r-md);margin:0 0 12px}
details.types>summary{cursor:pointer;min-height:var(--touch);display:flex;align-items:center;padding:0 var(--sp-4);
  color:var(--fg);font-weight:600;font-size:var(--fs-base);list-style-position:inside}
details.types>summary:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
.types-body{padding:0 var(--sp-4) var(--sp-4)}
.types-pick{display:flex;align-items:center;gap:var(--sp-2);flex-wrap:wrap;margin-bottom:var(--sp-2)}
.types-pick label{color:var(--muted);font-size:var(--fs-sm)}
.types-pick select{background:var(--input-bg);color:var(--fg);border:1px solid var(--input-border);
  border-radius:var(--r-sm);font:inherit;font-size:var(--fs-sm);height:var(--touch);padding:0 10px;min-width:200px;
  max-width:100%}
.types-pick select:focus{outline:2px solid var(--focus);outline-offset:1px}
#type-note{color:var(--muted);font-size:var(--fs-sm);line-height:var(--lh-body);margin-top:var(--sp-2)}
#type-chart{width:100%;min-height:360px}
.kit-count{color:var(--muted);font-size:var(--fs-sm);margin:8px 0 12px;min-height:1.2em}
.kit-hide{display:none}
table{width:100%;border-collapse:collapse;font-size:12px;background:var(--card);border:1px solid var(--border);
  border-radius:var(--r-md);overflow:hidden}
th{text-align:right;padding:8px;color:var(--muted);font-size:var(--fs-xs);font-weight:600;letter-spacing:.03em;
  border-bottom:1px solid var(--border);background:var(--card-2);cursor:pointer;white-space:nowrap}
th:hover{color:var(--fg)}
th.group{cursor:default;color:var(--muted);text-align:center;text-transform:uppercase;letter-spacing:.06em}
th.sort-asc::after,.chg-sort.sort-asc::after{content:' ▲';color:var(--accent)}
th.sort-desc::after,.chg-sort.sort-desc::after{content:' ▼';color:var(--accent)}
.chg-sort{display:block;margin:2px 0 0 auto;padding:0;border:0;background:none;color:var(--muted);font:inherit;
  font-size:10.5px;font-weight:500;cursor:pointer}
.chg-sort:hover{color:var(--fg)}
td .chg{display:block;font-size:var(--fs-xs);line-height:1.3;margin-top:1px}
th.left,td.left{text-align:left}
td{padding:8px;border-bottom:1px solid var(--border);white-space:nowrap;text-align:right}
td[data-col="sector"]{white-space:normal;width:100px;min-width:100px;max-width:110px;color:var(--muted);font-size:var(--fs-xs);line-height:1.3}
tbody tr:hover{background:var(--hover)}
tr[data-ticker]{cursor:pointer}
.ticker{color:var(--bright);font-weight:600}
.name{color:var(--muted);font-size:var(--fs-xs);font-weight:400;max-width:160px;overflow:hidden;text-overflow:ellipsis}
td[data-col="ticker"] .tk{display:flex;align-items:center;gap:var(--sp-2);white-space:nowrap}
td[data-col="ticker"] .who{min-width:0}
td[data-col="ticker"] .tline{display:flex;align-items:center}
td[data-col="ticker"] .break-mark{margin:-12px -10px -12px -6px}
@media (pointer:fine){td[data-col="ticker"] .break-mark{width:28px;height:28px;margin:0 0 0 2px}}
/* The "add to list" bookmark (#786 step 5). The glyph comes from the shared component (#770, #796), so the board
   only sizes it: 28 px with a mouse, 44 px on a touch screen. */
td[data-col="ticker"] .rc-watch{margin:0;flex:none}
td[data-col="ticker"] .rc-watch button{min-width:28px;min-height:28px;height:28px;padding:0 4px;border-radius:var(--r-sm)}
td[data-col="ticker"] .rc-watch button svg{width:14px;height:14px}
@media (pointer:coarse){td[data-col="ticker"] .rc-watch button{min-width:44px;min-height:44px;height:44px}
  td[data-col="ticker"] .rc-watch button svg{width:18px;height:18px}}
.pos{color:var(--pos)}.neg{color:var(--neg)}.zero{color:var(--muted)}
.empty{padding:40px;color:var(--muted)}
#modal{position:fixed;inset:0;background:var(--scrim);display:none;align-items:center;justify-content:center;z-index:100}
#modal.open{display:flex}
#modal-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r-lg);box-shadow:var(--elev-2);
  width:min(900px,94vw);max-height:92vh;overflow:auto;padding:var(--sp-4)}
#modal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
#modal-title{color:var(--bright);font-size:var(--fs-md);font-weight:600}
#modal-close{width:44px;height:44px;background:transparent;border:0;color:var(--muted);font-size:20px;cursor:pointer}
#chart{min-height:380px}
.spin{display:block;width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;
  animation:r .7s linear infinite;margin:40px auto}
@keyframes r{to{transform:rotate(360deg)}}
.status{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.status span{display:inline-flex;align-items:center;min-height:28px;padding:0 10px;border-radius:var(--r-pill);
  font-size:var(--fs-sm);font-weight:600}
.status .closed{background:var(--pos-soft);color:var(--pos)}
.status .waiting{background:var(--amber-soft);color:var(--amber)}
.break-mark{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;margin:-12px 0 -12px 2px;
  border:0;border-radius:22px;background:transparent;color:var(--amber);cursor:pointer;vertical-align:middle}
.break-mark:hover,.break-mark:focus-visible{background:var(--amber-soft);outline:none}
#breaks{position:fixed;inset:0;background:var(--scrim);display:none;align-items:center;justify-content:center;z-index:110}
#breaks.open{display:flex}
#breaks-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r-lg);box-shadow:var(--elev-2);
  width:min(440px,94vw);max-height:86vh;overflow:auto;padding:var(--sp-4)}
#breaks-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
#breaks-title{color:var(--bright);font-size:15px;font-weight:600}
#breaks-close{width:44px;height:44px;background:transparent;border:0;color:var(--muted);font-size:20px;cursor:pointer}
#breaks-note{color:var(--muted);font-size:var(--fs-sm);margin:4px 0 12px;line-height:1.5}
#breaks-list{list-style:none;display:flex;flex-direction:column;gap:8px}
#breaks-list li{background:var(--amber-soft);border-radius:var(--r-sm);padding:10px 12px;font-size:13px;line-height:1.5;
  white-space:normal}
#breaks-list b{color:var(--bright)}
.warn-box{background:var(--amber-soft);border:1px solid var(--amber);color:var(--fg);border-radius:var(--r-sm);
  padding:12px 16px;margin-bottom:14px;font-size:13px;line-height:1.55}
.warn-box b{color:var(--amber)}
.legend-note{color:var(--muted);font-size:var(--fs-xs);margin-top:8px;line-height:1.5}
.legend-note b{color:var(--fg)}
.warn-mark{color:var(--amber)}
/* Phone (412 px, avo's realme GT2 Pro): each ticker is a full-width card, no sideways scroll. The header goes;
   every cell carries its column name (data-label). The quarters sit in a 3-wide grid (shares over Δ%), the
   snapshot as label / value lines. */
@media (max-width: 640px){
  body{padding:var(--sp-3)}
  .board-search input{width:100%}
  table{display:block;background:none;border:0;overflow:visible}
  thead{display:none}
  tbody{display:flex;flex-direction:column;gap:var(--sp-3)}
  tbody tr[data-ticker]{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0 var(--sp-3);background:var(--card);
    border:1px solid var(--border);border-radius:var(--r-md);padding:var(--sp-3);box-shadow:var(--elev-1)}
  tbody tr[data-ticker]:hover{background:var(--card)}
  td{display:block;border:0;padding:var(--sp-1) 0;text-align:left;white-space:normal;min-width:0}
  td[data-col="ticker"]{grid-column:1/-1;padding-bottom:var(--sp-1)}
  td[data-col="ticker"] .name{max-width:none}
  td[data-col="sector"]{grid-column:1/-1;color:var(--muted);font-size:var(--fs-xs);padding:0 0 var(--sp-2);
    border-bottom:1px solid var(--border);margin-bottom:var(--sp-1)}
  td[data-label]::before{content:attr(data-label);display:block;color:var(--muted);font-size:10.5px;font-weight:600;
    letter-spacing:.03em;text-transform:uppercase}
  td.q{font-size:13px}
  tbody tr.detail{display:block;margin-top:calc(-1 * var(--sp-3))}
  tbody tr.detail td{display:block;border:1px solid var(--border);border-top:0;border-radius:0 0 var(--r-md) var(--r-md)}
  tr.detail .dwrap,tr.detail .dcharts{grid-template-columns:minmax(0,1fr)}
  tbody tr.open{border-radius:var(--r-md) var(--r-md) 0 0}
  tbody tr.open[data-ticker],tbody tr.open[data-ticker]:hover{background:var(--hover)}
  tbody tr.open td{background:none}
  td[data-col="value"],td[data-col="value"]~td{display:flex;justify-content:space-between;align-items:baseline;
    grid-column:span 2;padding:3px 0;font-size:12.5px}
  td[data-col="value"],td[data-col="value"]+td{border-top:1px solid var(--border);margin-top:var(--sp-2);padding-top:var(--sp-2)}
  td[data-col="value"]::before,td[data-col="value"]~td::before{display:inline;font-size:var(--fs-xs);text-transform:none;
    letter-spacing:0;font-weight:500}
}
tr.open td{background:var(--hover)}
tr.detail td{padding:0;background:var(--card-2);white-space:normal;text-align:left;cursor:default}
tr.detail:hover{background:none}
.dwrap{display:grid;grid-template-columns:minmax(0,2fr) minmax(220px,1fr);gap:var(--sp-4);padding:var(--sp-4);align-items:start}
.dcharts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--sp-3);align-items:start}
.mini{background:var(--card);border:1px solid var(--border);border-radius:var(--r-md);padding:var(--sp-2) var(--sp-3);min-width:0}
.mini-head{display:flex;justify-content:space-between;align-items:center;gap:var(--sp-2)}
.mini-title{color:var(--fg);font-size:var(--fs-sm);font-weight:600}
.mini-exp{background:none;border:0;color:var(--accent);font:inherit;font-size:var(--fs-xs);cursor:pointer;min-height:28px;padding:0 4px}
.mini-exp:hover{text-decoration:underline}
.mini-plot{height:170px}
.dstats{display:grid;grid-template-columns:auto auto;gap:4px var(--sp-3);margin:0;font-size:var(--fs-sm)}
.dstats dt{color:var(--muted)}
.dstats dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
.dbreaks{margin-top:var(--sp-3);background:var(--amber-soft);border-radius:var(--r-sm);padding:var(--sp-2) var(--sp-3);font-size:var(--fs-sm)}
.dbreaks-head{color:var(--amber);font-weight:600;margin-bottom:4px}
.dbreak{margin-top:4px}
.dbreak b{color:var(--bright)}
ul.brk{margin:2px 0 0;padding-left:18px;list-style:disc}
ul.brk ul{margin:0;padding-left:18px;list-style:circle;color:var(--muted)}
ul.brk li{margin:1px 0}
ul.brk li::marker{color:var(--amber)}
footer{margin-top:var(--sp-5);padding-top:12px;border-top:1px solid var(--border);color:var(--muted);font-size:var(--fs-xs);
  text-align:center}
</style>"""

JS = """<script>
var FIELDS = {};
var RCF = null;
var SEARCH_Q = '';
var SORT_COL = __DEFAULT_SORT__;
var SORT_ASC = false;
var ROW_COUNT = __ROW_COUNT__;
var NCOLS = __NCOLS__;
var COL_LABELS = __COL_LABELS__;
var TABLE_SNAP = __TABLE_SNAP__;          // snapshot columns drawn in the table; the rest go to the row's detail panel
var DETAIL = {};                          // ticker -> [[label, text, cls, title], ...] for the detail panel          // a cell's header text, shown beside it when a phone stacks the row as a card
var BREAKS = {};
var MARK_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
  + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2 21h20L12 3z"></path>'
  + '<path d="M12 10v4"></path><path d="M12 17.5v.01"></path></svg>';
function breakBullets(lines){
  // One quarter's breaks as a bullet list: "N filers stopped filing, X shares", the largest filers nested under it.
  var ul = document.createElement('ul'); ul.className = 'brk';
  (lines || []).forEach(function(line){
    var li = document.createElement('li');
    li.appendChild(document.createTextNode(line[0]));
    if((line[1] || []).length){
      var sub = document.createElement('ul');
      line[1].forEach(function(n){ var s = document.createElement('li'); s.textContent = n; sub.appendChild(s); });
      li.appendChild(sub);
    }
    ul.appendChild(li);
  });
  return ul;
}
function openBreaks(ticker){
  // Where the ticker's filer flow breaks (#732), newest first; every string reaches the DOM as text.
  var list = document.getElementById('breaks-list');
  while(list.firstChild){ list.removeChild(list.firstChild); }
  document.getElementById('breaks-title').textContent = ticker + ': where the filer flow breaks';
  var items = BREAKS[ticker] || [];
  for(var i = 0; i < items.length; i++){
    var li = document.createElement('li');
    var b = document.createElement('b');
    b.textContent = items[i][0];
    li.appendChild(b);
    li.appendChild(breakBullets(items[i][1]));
    list.appendChild(li);
  }
  document.getElementById('breaks').classList.add('open');
  document.getElementById('breaks-close').focus();
}
function closeBreaks(){ document.getElementById('breaks').classList.remove('open'); }
function cellVal(row, col){ var el = row.querySelector('[data-col="'+col+'"]'); return el ? el.getAttribute('data-val') : ''; }
function sortTable(col, el){
  closeDetails();
  if(SORT_COL === col){ SORT_ASC = !SORT_ASC; } else { SORT_COL = col; SORT_ASC = false; }
  document.querySelectorAll('th, .chg-sort').forEach(function(th){ th.classList.remove('sort-asc','sort-desc'); });
  el.classList.add(SORT_ASC ? 'sort-asc' : 'sort-desc');
  var tbody = document.querySelector('tbody');
  var rows = Array.from(tbody.querySelectorAll('tr[data-ticker]'));
  rows.sort(function(a,b){
    var av = cellVal(a, col), bv = cellVal(b, col);
    var aNull = av === '', bNull = bv === '';
    if(aNull || bNull) return (aNull && bNull) ? 0 : (aNull ? 1 : -1);
    var an = Number(av), bn = Number(bv);
    var r = (!isNaN(an) && !isNaN(bn)) ? an - bn : (av < bv ? -1 : (av > bv ? 1 : 0));
    return SORT_ASC ? r : -r;
  });
  rows.forEach(function(r){ tbody.appendChild(r); });
  applyFilters();
}
// The figure is drawn on the page's theme (#761): its paper, text, grid and series colours are read from the tokens,
// so the chart matches the light or dark board around it. The series keep the ramp colours the figure carries (#767).
function themedFig(fig){
  var cs = getComputedStyle(document.documentElement), v = function(n){ return cs.getPropertyValue(n).trim(); };
  var lay = fig.layout || {}, grid = v('--grid'), muted = v('--muted');
  lay.paper_bgcolor = 'rgba(0,0,0,0)'; lay.plot_bgcolor = 'rgba(0,0,0,0)';
  lay.font = Object.assign({}, lay.font || {}, {color: v('--fg'), family: v('--font-sans')});
  lay.hoverlabel = {bgcolor: v('--card'), bordercolor: v('--border'), font: {color: v('--fg')}};
  Object.keys(lay).forEach(function(k){
    if(/^[xy]axis[0-9]*$/.test(k)){ lay[k] = Object.assign({}, lay[k], {gridcolor: grid, zerolinecolor: grid, linecolor: grid,
      tickfont: {color: muted}}); }
  });
  (lay.annotations || []).forEach(function(a){ a.font = Object.assign({}, a.font || {}, {color: muted}); });
  fig.layout = lay; return fig;
}
// The row's detail panel (avo 12 Nov 2026): two small charts, one y-axis each (shares as bars, holders as a line),
// each with "Click to expand" to the full chart; the snapshot values the table leaves out; and the filer breaks.
// Every string reaches the DOM as text.
function closeDetails(){
  document.querySelectorAll('tr.detail').forEach(function(d){ d.parentNode.removeChild(d); });
  document.querySelectorAll('tr.open').forEach(function(r){ r.classList.remove('open'); });
}
function el(tag, cls, text){ var x = document.createElement(tag); if(cls){ x.className = cls; } if(text !== undefined && text !== null){ x.textContent = text; } return x; }
function miniChart(host, xs, ys, kind, color){
  var cs = getComputedStyle(document.documentElement), v = function(n){ return cs.getPropertyValue(n).trim(); };
  // Finance units (B, M, K) instead of Plotly's SI "G"; quarter labels short ("25 Q1") and slanted so every one fits.
  var top = Math.max.apply(null, ys.map(function(y){ return Math.abs(y || 0); }));
  var div = top >= 1e9 ? 1e9 : top >= 1e6 ? 1e6 : top >= 1e3 ? 1e3 : 1, suf = {1e9: 'B', 1e6: 'M', 1e3: 'K', 1: ''}[div];
  var raw = ys;
  ys = ys.map(function(y){ return y === null || y === undefined ? null : y / div; });
  xs = xs.map(function(x){ return String(x).replace(/^20([0-9][0-9]) /, '$1 '); });
  var tr = kind === 'bar' ? {type: 'bar', x: xs, y: ys, customdata: raw, marker: {color: color},
       hovertemplate: '%{x}: %{customdata:,.0f}<extra></extra>'}
    : {type: 'scatter', mode: 'lines+markers', x: xs, y: ys, customdata: raw, line: {color: color, width: 2},
       marker: {size: 5}, hovertemplate: '%{x}: %{customdata:,.0f}<extra></extra>'};
  Plotly.react(host, [tr], {height: 170, margin: {l: 44, r: 14, t: 6, b: 44}, paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)', showlegend: false, font: {color: v('--muted'), family: v('--font-sans'), size: 10},
    xaxis: {type: 'category', gridcolor: 'rgba(0,0,0,0)', linecolor: v('--grid'), fixedrange: true, tickangle: -40, automargin: true},
    yaxis: {gridcolor: v('--grid'), zerolinecolor: v('--grid'), fixedrange: true, ticksuffix: suf, nticks: 4}},
    {displayModeBar: false, responsive: true, staticPlot: false});
}
function toggleDetail(row){
  var t = row.getAttribute('data-ticker'), was = row.classList.contains('open');
  closeDetails();
  if(was){ return; }
  row.classList.add('open');
  var d = el('tr', 'detail'), td = el('td');
  td.setAttribute('colspan', String(NCOLS));
  var wrap = el('div', 'dwrap');
  var charts = el('div', 'dcharts');
  var boxes = [['Shares, today’s basis', 'bar', 'Shares'], ['Holders', 'line', 'Holders']].map(function(spec){
    var box = el('div', 'mini');
    var head = el('div', 'mini-head');
    head.appendChild(el('span', 'mini-title', spec[0]));
    var b = el('button', 'mini-exp', 'Click to expand');
    b.type = 'button'; b.setAttribute('data-expand', t);
    head.appendChild(b);
    box.appendChild(head);
    var plot = el('div', 'mini-plot');
    plot.appendChild(el('span', 'spin'));
    box.appendChild(plot);
    charts.appendChild(box);
    return [plot, spec];
  });
  wrap.appendChild(charts);
  var side = el('div', 'dside');
  var stats = DETAIL[t] || [];
  if(stats.length){
    var dl = el('dl', 'dstats');
    stats.forEach(function(s){
      var dt = el('dt', null, s[0]), dd = el('dd', s[2] || '', s[1]);
      if(s[3]){ dd.setAttribute('title', s[3]); }
      dl.appendChild(dt); dl.appendChild(dd);
    });
    side.appendChild(dl);
  }
  var br = BREAKS[t] || [];
  if(br.length){
    var bx = el('div', 'dbreaks');
    bx.appendChild(el('div', 'dbreaks-head', '⚠ Where the filer flow breaks'));
    br.forEach(function(item){
      var q = el('div', 'dbreak');
      q.appendChild(el('b', null, item[0]));
      q.appendChild(breakBullets(item[1]));
      bx.appendChild(q);
    });
    side.appendChild(bx);
  }
  wrap.appendChild(side);
  td.appendChild(wrap);
  d.appendChild(td);
  row.parentNode.insertBefore(d, row.nextSibling);
  fetch('/api/sec_13f/' + encodeURIComponent(t))
    .then(function(r){ return r.json(); })
    .then(function(res){
      var data = (res && res.fig && res.fig.data) || [];
      boxes.forEach(function(pair){
        var plot = pair[0], spec = pair[1];
        var xs = [], ys = [], color = null;
        data.forEach(function(tr){
          if(tr.name === spec[2] || (spec[2] === 'Holders' && tr.name === 'Holders (filed so far)')){
            (tr.x || []).forEach(function(x, i){ if(xs.indexOf(x) === -1){ xs.push(x); ys.push((tr.y || [])[i]); } });
            color = color || (tr.marker && tr.marker.color) || (tr.line && tr.line.color);
          }
        });
        plot.innerHTML = '';
        if(!xs.length){ plot.appendChild(el('div', 'empty', 'No series data.')); return; }
        miniChart(plot, xs, ys, spec[1], color || getComputedStyle(document.documentElement).getPropertyValue('--data-1').trim());
      });
    })
    .catch(function(){ boxes.forEach(function(p){ p[0].innerHTML = ''; p[0].appendChild(el('div', 'empty neg', 'Failed to load chart.')); }); });
}
function openChart(ticker){
  var modal = document.getElementById('modal');
  modal.classList.add('open');
  document.getElementById('modal-title').textContent = ticker;
  var chart = document.getElementById('chart');
  chart.innerHTML = '<span class="spin"></span>';
  fetch('/api/sec_13f/' + encodeURIComponent(ticker))
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(!d.fig){ chart.innerHTML = '<div class="empty">No series data.</div>'; return; }
      Plotly.react('chart', themedFig(d.fig), {}, {displayModeBar: true, responsive: true});
    })
    .catch(function(){ chart.innerHTML = '<div class="empty neg">Failed to load chart.</div>'; });
}
function closeModal(){ document.getElementById('modal').classList.remove('open'); }
// Top holdings by investor type (#760): the type's stacked shares per quarter, loaded when the section is opened
// and again when the type changes. The pick is remembered in this browser only, if storage allows.
var TYPE_KEY = 'rc.sec13f.type', TYPE_SEQ = 0;
function loadType(tag){
  var host = document.getElementById('type-chart'), note = document.getElementById('type-note'), seq = ++TYPE_SEQ;
  try { localStorage.setItem(TYPE_KEY, tag); } catch(e) {}
  note.textContent = '';
  // Purged before the spinner replaces it: Plotly.react on a host whose nodes were wiped draws nothing.
  Plotly.purge(host);
  host.innerHTML = '<span class="spin"></span>';
  fetch('/api/sec_13f/types/' + encodeURIComponent(tag))
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(seq !== TYPE_SEQ){ return; }           // a later pick is on its way: this answer is stale
      host.innerHTML = '';
      note.textContent = d.note || d.error || '';
      if(d.fig){ Plotly.newPlot(host, themedFig(d.fig).data, d.fig.layout, {displayModeBar: false, responsive: true}); }
    })
    .catch(function(){
      if(seq !== TYPE_SEQ){ return; }
      host.innerHTML = ''; note.textContent = 'The investor-type chart could not be loaded.';
    });
}
function applyFilters(){
  closeDetails();
  var kitOn = !!(RCF && RCF.active()), q = SEARCH_Q.toLowerCase();
  var rows = document.querySelectorAll('tbody tr[data-ticker]');
  var shown = 0, total = rows.length;
  for(var i = 0; i < rows.length; i++){
    var r = rows[i], t = r.getAttribute('data-ticker'), f = FIELDS[t], ok = true;
    if(q){
      var hay = (t + ' ' + (f && f.name ? f.name : '')).toLowerCase();
      if(hay.indexOf(q) === -1) ok = false;
    }
    if(ok && kitOn && f && !RCF.pass(f)) ok = false;
    r.style.display = ok ? '' : 'none';
    if(ok) shown++;
  }
  var line = document.getElementById('kit-count');
  if(line){ line.textContent = kitOn ? (shown + ' of ' + total + ' names') : ''; }
}
function initKit(){
  var rows = [];
  for(var k in FIELDS){ if(FIELDS.hasOwnProperty(k)) rows.push(FIELDS[k]); }
  RCF = RCFilters.mount({root: '#rc-filters', rows: rows, meta: META, onChange: applyFilters});
  applyFilters();
}
function build(p){
  // The rows come from the payload route (#429); every string reaches the DOM as text or an attribute, never markup.
  // The filter rows ride the same payload (KF #375), not the HTML, so the page stays small.
  var tbody = document.getElementById('rows');
  var frag = document.createDocumentFragment();
  var rows = (p && p.rows) || [];
  var cols = (p && p.cols) || [];
  FIELDS = (p && p.fields) || {};
  for(var i = 0; i < rows.length; i++){
    var r = rows[i];
    var tr = document.createElement('tr');
    tr.setAttribute('data-ticker', r[0]);
    var tdT = document.createElement('td');
    tdT.className = 'left';
    tdT.setAttribute('data-col', 'ticker');
    tdT.setAttribute('data-val', r[0]);
    // The watch-list pair comes first, one box split in two (avo 12 Nov 2026: icons before the ticker); then the ticker
    // with its ⚠, and the name on a small second line, so the column stays narrow.
    var tk = document.createElement('div');
    tk.className = 'tk';
    tk.insertAdjacentHTML('beforeend', rcWatchButtons(r[0]));
    tdT.appendChild(tk);
    var who = document.createElement('div');
    who.className = 'who';
    var line1 = document.createElement('div');
    line1.className = 'tline';
    var spT = document.createElement('span');
    spT.className = 'ticker';
    spT.textContent = r[0];
    line1.appendChild(spT);
    who.appendChild(line1);
    tk.appendChild(who);
    if(r[4] && r[4].length){
      BREAKS[r[0]] = r[4];
      var mk = document.createElement('button');
      mk.type = 'button';
      mk.className = 'break-mark';
      mk.setAttribute('aria-label', 'Filer breaks for ' + r[0]);
      mk.setAttribute('title', 'Filers started or stopped reporting ' + r[0] + ': open for the list');
      mk.innerHTML = MARK_SVG;
      line1.appendChild(mk);
    }
    if(r[1]){
      var spN = document.createElement('div');
      spN.className = 'name';
      spN.textContent = r[1];
      spN.setAttribute('title', r[1]);
      who.appendChild(spN);
    }
    tr.appendChild(tdT);
    var tdS = document.createElement('td');
    tdS.className = 'left';
    tdS.setAttribute('data-col', 'sector');
    tdS.setAttribute('data-val', r[2]);
    tdS.textContent = r[2];
    tr.appendChild(tdS);
    var cells = r[3] || [];
    var chg = {};
    for(var j = 0; j < cols.length; j++){ if(cols[j].indexOf('ch_') === 0){ chg[cols[j].slice(3)] = cells[j] || []; } }
    for(var j = 0; j < cols.length; j++){
      if(cols[j].indexOf('ch_') === 0) continue;          // drawn under its quarter's shares, not as its own column
      var c = cells[j] || [];
      if(cols[j].indexOf('sh_') !== 0 && TABLE_SNAP.indexOf(cols[j]) === -1){
        (DETAIL[r[0]] = DETAIL[r[0]] || []).push([COL_LABELS[cols[j]] || cols[j], c[0], c[2], c[3]]);
        continue;
      }
      var td = document.createElement('td');
      td.setAttribute('data-col', cols[j]);
      if(COL_LABELS[cols[j]]){ td.setAttribute('data-label', COL_LABELS[cols[j]]); }
      if(cols[j].indexOf('sh_') === 0){ td.className = 'q'; }
      td.setAttribute('data-val', (c[1] === null || c[1] === undefined) ? '' : String(c[1]));
      td.className = ((td.className ? td.className + ' ' : '') + (c[2] || '')).trim();
      if(c[3] !== null && c[3] !== undefined){ td.setAttribute('title', c[3]); }
      td.textContent = (c[0] === null || c[0] === undefined) ? '' : c[0];
      if(cols[j].indexOf('sh_') === 0){
        var d = chg[cols[j].slice(3)];
        if(d){
          var sp = document.createElement('span');
          sp.className = 'chg ' + (d[2] || '');
          sp.setAttribute('data-col', 'ch_' + cols[j].slice(3));
          sp.setAttribute('data-val', (d[1] === null || d[1] === undefined) ? '' : String(d[1]));
          sp.textContent = (d[0] === null || d[0] === undefined) ? '' : d[0];
          td.appendChild(sp);
        }
      }
      tr.appendChild(td);
    }
    frag.appendChild(tr);
  }
  while(tbody.firstChild){ tbody.removeChild(tbody.firstChild); }
  tbody.appendChild(frag);
  initKit();
}
function fail(e){
  var tbody = document.getElementById('rows');
  while(tbody.firstChild){ tbody.removeChild(tbody.firstChild); }
  var tr = document.createElement('tr');
  var td = document.createElement('td');
  td.className = 'empty';
  td.setAttribute('colspan', String(NCOLS));
  td.textContent = "The rows could not be loaded (" + ((e && e.message) || e) + "). Press Refresh to rebuild the board.";
  tr.appendChild(td);
  tbody.appendChild(tr);
}
function loadRows(){ rcPayload("module_sec_13f").then(build).catch(fail); }
document.addEventListener('DOMContentLoaded', function(){
  var types = document.getElementById('types'), pick = document.getElementById('type-pick');
  if(types && pick){
    try {
      var saved = localStorage.getItem(TYPE_KEY);
      if(saved && Array.prototype.some.call(pick.options, function(o){ return o.value === saved && !o.disabled; })){ pick.value = saved; }
    } catch(e) {}
    types.addEventListener('toggle', function(){ if(types.open && !types.dataset.loaded){ types.dataset.loaded = '1'; loadType(pick.value); } });
    pick.addEventListener('change', function(){ loadType(pick.value); });
  }
  var q = document.getElementById('board-q');
  if(q){
    q.addEventListener('input', function(e){ SEARCH_Q = e.target.value; applyFilters(); });
  }
  document.getElementById('rows').addEventListener('click', function(e){
    var tr = e.target.closest('tr[data-ticker]');
    if(e.target.closest('.rc-watch')){ return; }
    var exp = e.target.closest('[data-expand]');
    if(exp){ openChart(exp.getAttribute('data-expand')); return; }
    if(e.target.closest('tr.detail')){ return; }
    if(e.target.closest('.break-mark')){ if(tr){ openBreaks(tr.getAttribute('data-ticker')); } return; }
    if(tr){ toggleDetail(tr); }
  });
  document.getElementById('modal').addEventListener('click', function(e){ if(e.target === this) closeModal(); });
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('breaks').addEventListener('click', function(e){ if(e.target === this) closeBreaks(); });
  document.getElementById('breaks-close').addEventListener('click', closeBreaks);
  document.addEventListener('keydown', function(e){ if(e.key === 'Escape'){ closeModal(); closeBreaks(); } });
});
if(ROW_COUNT > 0){ loadRows(); }
</script>"""


def _f(v, digits=1, suffix="%", sign=True):
    if v is None:
        return "—"
    return f"{v:+.{digits}f}{suffix}" if sign else f"{v:.{digits}f}{suffix}"


def _fmt_shares(v):
    if v is None:
        return "—"
    v = float(v)
    if abs(v) >= 1e9:
        return f"{v / 1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:,.0f}"


def _num(v):
    return "" if v is None else v


def _sign_class(v):
    if v is None:
        return "zero"
    return "pos" if v > 0 else ("neg" if v < 0 else "zero")


def _qlabel(q):
    try:
        y, m = int(q[:4]), int(q[5:7])
        return f"{y} Q{(m - 1) // 3 + 1}"
    except (ValueError, TypeError):
        return q


#: Marks a report period whose filing deadline has not passed, in the column head and the legend (#514).
ARRIVING_MARK = "*"


def _arriving(totals: dict, quarters: list) -> set[str]:
    """The quarters still arriving: any period a cell says is not ``complete``. The flag is the filing deadline
    against today (``rcdata.read``), so it is the same answer for every ticker; a quarter no cell mentions is
    settled by default, which is what an old quarter with no row is."""
    return {q for periods in totals.values() for q in quarters
            if (cell := periods.get(q)) is not None and cell.get("complete") is False}


def arriving(totals: dict, quarters: list) -> set[str]:
    """The quarters still arriving: from the newest period a cell says is not ``complete`` onwards.

    Completeness is a property of the period (its filing deadline against today, ``rcdata.read``), so it is
    monotone: the quarters still arriving are the newest ones. Normalising to that suffix is what keeps this the
    one definition — an interior flag, which only a row with a deadline the store cannot read could produce, would
    otherwise reshape the middle of the board while the legend said nothing (#514 review). One function, called by
    both the entries and the page, because two routes to the same set are free to disagree.
    """
    flagged = {q for periods in totals.values() for q in quarters
               if (cell := periods.get(q)) is not None and cell.get("complete") is False}
    if not flagged:
        return set()
    return set(quarters[quarters.index(min(flagged)):])


#: The settled quarters the board shows: a rolling window, not every quarter since ``engine.START_QUARTER`` (owner,
#: 12 Nov 2026: "A son 6 çeyrek"). The chart keeps every stored quarter.
WINDOW_QUARTERS = 6


def _quarters(totals: dict) -> list:
    """The board's quarter columns: the newest ``WINDOW_QUARTERS`` settled quarters, then any still arriving."""
    qs = set()
    for periods in totals.values():
        qs.update(periods.keys())
    qs = sorted(q for q in qs if q >= engine.START_QUARTER)
    late = arriving(totals, qs)
    keep = set([q for q in qs if q not in late][-WINDOW_QUARTERS:]) | late
    return [q for q in qs if q in keep]


def _on_todays_basis(value, factor):
    """A share count (or a flow) of a quarter end on today's share basis: ÷ that day's split factor (#732)."""
    if value is None or not factor:
        return value
    return value / factor


def _breaks(raw: list, quarters: list, settled: set, factors: dict, shares_by_q: dict | None = None) -> list[dict]:
    """The filer breaks of the board's settled quarters, one entry per quarter, newest first (#732): how many filers
    started and stopped reporting the ticker, their shares on today's basis, and the largest of them by name.

    A quarter is kept only when the filers that started and stopped hold at least ``BREAK_MARK_SHARE`` of the
    ticker's 13F shares that quarter (``shares_by_q``, today's basis): a large name sees a few small filers come and
    go every quarter, and marking those put the warning on nearly every row (owner, 12 Nov 2026)."""
    by_q: dict[str, dict] = {}
    for b in raw or []:
        q = b.get("period")
        if q not in settled or q not in quarters or b.get("kind") not in ("started", "stopped"):
            continue
        e = by_q.setdefault(q, {"period": q, "started": [], "stopped": []})
        e[b["kind"]].append({"shares": _on_todays_basis(b.get("shares"), factors.get(q)),
                             "filer": b.get("filer_name") or b.get("cik") or "?"})
    for e in by_q.values():
        for kind in ("started", "stopped"):
            e[kind].sort(key=lambda f: -(f["shares"] or 0))
    if shares_by_q is not None:
        def material(e):
            total = shares_by_q.get(e["period"])
            moved = sum(f["shares"] or 0 for k in ("started", "stopped") for f in e[k])
            return bool(total) and moved >= BREAK_MARK_SHARE * total
        by_q = {q: e for q, e in by_q.items() if material(e)}
    return [by_q[q] for q in sorted(by_q, reverse=True)]


BREAK_NAMES = 3     # the largest filers named per line of the break list
BREAK_MARK_SHARE = 0.01   # a quarter's breaks are marked from 1% of the ticker's 13F shares (owner, 12 Nov 2026)


def _break_lines(e: dict) -> list[list]:
    """One quarter of the break list as bullets: ``[summary, [largest filer, ...]]``, stopped first (the shares that
    left the count), then started. The filers stay a list so a name with a comma never splits into two bullets
    (owner, 12 Nov 2026: "bullet ile goster")."""
    lines = []
    for kind, verb in (("stopped", "stopped filing"), ("started", "started filing")):
        filers = e[kind]
        if not filers:
            continue
        total = sum(f["shares"] or 0 for f in filers)
        summary = f"{len(filers)} filer{'s' if len(filers) != 1 else ''} {verb}, {_fmt_shares(total)} shares"
        named = [f"{f['filer']} ({_fmt_shares(f['shares'])})" for f in filers[:BREAK_NAMES]]
        if len(filers) > BREAK_NAMES:
            named.append(f"and {len(filers) - BREAK_NAMES} more")
        lines.append([summary, named])
    return lines


def quarter_status(totals: dict, quarters: list) -> list[tuple[str, str]]:
    """``[(css class, text)]`` for the header: the newest settled quarter's state and any quarter still arriving.

    A quarter is ``closed`` when the SEC data set covering its filing deadline is published (#732, owner: "tam
    sayılsın ve üzerinde daha değişiklik almayacak şekilde kapansın"); settled but not closed, it waits for that set.
    A store that cannot say (no cell carries ``closed``) shows nothing for it."""
    late = arriving(totals, quarters)
    settled = [q for q in quarters if q not in late]
    out = []
    if settled:
        q = settled[-1]
        flags = {cell.get("closed") for periods in totals.values() if (cell := periods.get(q)) is not None}
        if True in flags:
            out.append(("closed", f"{_qlabel(q)} closed"))
        elif False in flags:
            out.append(("waiting", f"{_qlabel(q)} settled, waits for the SEC data set"))
    for q in sorted(late):
        out.append(("waiting", f"{_qlabel(q)} still arriving"))
    return out


def _fmt_money(v):
    if v is None:
        return "—"
    v = float(v)
    if abs(v) >= 1e9:
        return f"${v / 1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:,.0f}"


def build_chart_fig(ticker: str, periods: dict, enrichment: dict = None) -> dict:
    """Shares + price (row 1) and $ value + holders (row 2) over quarters.

    A quarter still arriving (#514) is drawn as what it is, a part of itself (#521): its bars are hatched, its
    label carries the board's mark, the holders line goes dashed into it, and a note names it."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    qs = sorted(periods.keys())
    still_arriving = arriving({ticker: periods}, qs)
    labels = [_qlabel(q) + (ARRIVING_MARK if q in still_arriving else "") for q in qs]
    pattern = dict(shape=["/" if q in still_arriving else "" for q in qs], fgopacity=0.6)
    factors = (enrichment or {}).get("split_factor") or {}
    shares = [_on_todays_basis(periods[q].get("shares") or 0, factors.get(q)) for q in qs]
    value = [periods[q].get("value") or 0 for q in qs]
    holders = [periods[q].get("holders") or 0 for q in qs]
    price = None
    if enrichment:
        px = (enrichment.get("price") or {})
        price = [px.get(q) * factors.get(q, 1.0) if px.get(q) is not None else None for q in qs]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.10,
                        subplot_titles=(f"{ticker} — total shares & price (today's share basis)", "market value ($) & holders"),
                        specs=[[{"secondary_y": True}], [{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=labels, y=shares, name="Shares", marker=dict(color=THEME_SCALE["c1"], pattern=pattern),
                         hovertemplate="%{y:,.0f}"), 1, 1)
    if price:
        fig.add_trace(go.Scatter(x=labels, y=price, name="Price",
                                 line=dict(color=THEME_SCALE["c2"], width=2)), 1, 1, secondary_y=True)
    fig.add_trace(go.Bar(x=labels, y=value, name="Value ($)", marker=dict(color=THEME_SCALE["c3"], pattern=pattern),
                         hovertemplate="%{y:,.0f}"), 2, 1)
    n = len(qs) - len(still_arriving)           # the settled quarters come first: the arriving set is a suffix
    fig.add_trace(go.Scatter(x=labels[:n], y=holders[:n], name="Holders",
                             line=dict(color=THEME_SCALE["c4"], width=2)), 2, 1, secondary_y=True)
    if still_arriving:
        start = max(n - 1, 0)
        fig.add_trace(go.Scatter(x=labels[start:], y=holders[start:], name="Holders (filed so far)",
                                 line=dict(color=THEME_SCALE["c4"], width=2, dash="dash"), showlegend=False), 2, 1, secondary_y=True)
        names = ", ".join(_qlabel(q) for q in sorted(still_arriving))
        fig.add_annotation(text=f"{ARRIVING_MARK} {names} still arriving: filed so far, not a drop",
                           xref="paper", yref="paper", x=0, y=-0.08, xanchor="left", yanchor="top",
                           showarrow=False, font=dict(size=12, color=THEME_TOKENS["dark"]["muted"]))
    fig.update_layout(template="plotly_dark", height=620, hovermode="x unified",
                      # Anchored at its bottom so a legend that wraps on a phone grows up into the margin, not over the title.
                      legend=dict(orientation="h", y=1.08, yanchor="bottom", x=0),
                      margin=dict(l=60, r=60, t=110, b=60 if still_arriving else 40))
    fig.update_xaxes(type="category")
    return fig.to_dict()


TYPE_SERIES = 6          # the categorical ramp has six hues (``--c1``..``--c6``); a seventh name is "Other"
OTHER_LABEL = "Other"


def build_type_fig(label: str, rows: list[dict], still_arriving=frozenset()) -> tuple[dict | None, str]:
    """``(figure, note)`` for one investor type (#760): a stacked area of each quarter's share (%) of the type's 13F
    portfolio, for the ``TYPE_SERIES`` tickers with the largest share in the newest quarter (then in any quarter,
    when the newest names fewer), and every other name as "Other", so each quarter adds up to 100. A ticker outside
    the type's stored top (``TYPE_TOP``) in a quarter is part of that quarter's rest, so it draws 0 there. The
    colour follows the ticker (the ramp in the order the names are ranked), never its place in one quarter. A
    quarter ``still_arriving`` carries the board's mark and the note names it. None when the type has no rows."""
    import plotly.graph_objects as go
    qs = sorted({r["period"] for r in rows})
    if not qs:
        return None, f"No 13F filer of type {label} in the store yet."
    share: dict[str, dict[str, float]] = {}
    for r in rows:
        if r.get("ticker"):
            share.setdefault(r["ticker"], {})[r["period"]] = float(r.get("share") or 0)
    newest = qs[-1]
    names = sorted(share, key=lambda t: (-share[t].get(newest, 0.0), -max(share[t].values()), t))[:TYPE_SERIES]
    labels = [_qlabel(q) + (ARRIVING_MARK if q in still_arriving else "") for q in qs]
    fig = go.Figure()
    for i, t in enumerate(names):
        fig.add_trace(go.Scatter(x=labels, y=[share[t].get(q, 0.0) for q in qs], name=t, mode="lines",
                                 stackgroup="one", line=dict(width=1.5, color=THEME_SCALE[f"c{i + 1}"]),
                                 hovertemplate=f"{t}: %{{y:.1f}}%<extra></extra>"))
    rest = [max(100.0 - sum(share[t].get(q, 0.0) for t in names), 0.0) for q in qs]
    fig.add_trace(go.Scatter(x=labels, y=rest, name=OTHER_LABEL, mode="lines", stackgroup="one",
                             line=dict(width=1, color="rgba(128,128,128,0.55)"), fillcolor="rgba(128,128,128,0.22)",
                             hovertemplate=f"{OTHER_LABEL}: %{{y:.1f}}%<extra></extra>"))
    # The legend sits under the plot: seven names wrap on a phone, and above it they would cover the plot.
    fig.update_layout(height=400, hovermode="x unified", showlegend=True,
                      legend=dict(orientation="h", y=-0.22, yanchor="top", x=0),
                      margin=dict(l=48, r=16, t=16, b=40))
    fig.update_xaxes(type="category", tickangle=-40, automargin=True)
    fig.update_yaxes(range=[0, 100], ticksuffix="%", nticks=6)
    last = next((r for r in rows if r["period"] == newest), {})
    filers, value, snap = last.get("type_filers"), last.get("type_value"), last.get("snapshot_date")
    note = (f"{label}: {filers or 0:,} filers, {_fmt_money(value)} in mapped stocks in {_qlabel(newest)}. "
            "Share of the type's 13F portfolio per quarter; names outside its top ten that quarter are in Other."
            + (f" Types are Unusual Whales' institution tags as of {snap}, applied to every quarter." if snap else "")
            + (f" {ARRIVING_MARK} {', '.join(_qlabel(q) for q in sorted(still_arriving))}: still arriving, filed "
               "so far." if still_arriving else ""))
    return fig.to_dict(), note


#: The snapshot columns the table keeps (avo 12 Nov 2026: the board must fit his 1187 px laptop with no sideways
#: scroll). The other snapshot values ride the payload as before and are shown in the row's detail panel.
_TABLE_SNAP = ("value", "pct_out", "netflow", "streak")

_SNAP_COLS = [
    ("value", "Value ($)"),
    ("pct_out", "% Out"),
    ("holders", "Holders"),
    ("new_exit", "New/Exit"),
    ("breadth", "Breadth"),
    ("top10", "Top10%"),
    ("hhi", "HHI"),
    ("netflow", "Net flow"),
    ("streak", "Streak"),
    ("accel", "Accel"),
    ("price_delta", "Price Δ"),
]


def board_entries(totals: dict, meta: dict, enrichment: dict = None) -> tuple[list, list]:
    """``(quarters, entries)``: the board's quarter columns and one entry per ticker, in board order (most shares in
    the latest quarter first). The board renders these; the result history (``history.py``, #375) records them."""
    quarters = _quarters(totals)
    still_arriving = arriving(totals, quarters)
    enrichment = enrichment or {}
    entries = []
    for t, periods in totals.items():
        factors = (enrichment.get(t) or {}).get("split_factor") or {}
        filed = []
        shares = []
        chgs = []
        prev = None
        last = None
        last_period = None
        for i, q in enumerate(quarters):
            cell = periods.get(q)
            s_filed = cell["shares"] if cell else None
            filed.append(s_filed)
            s = _on_todays_basis(s_filed, factors.get(q))
            shares.append(s)
            # No change against a quarter still arriving, in either direction: it holds a part of itself, so the
            # comparison is a measurement of how much of it has been filed (#514).
            # No change against a quarter still arriving, in either direction: it holds a part of itself. The
            # "out of" half is a tautology while the set is a suffix; it is kept as cheap defence.
            chgs.append(None if i == 0 or q in still_arriving or quarters[i - 1] in still_arriving
                        else _pct(s, prev))
            prev = s
            if cell and q not in still_arriving:
                # The snapshot block (holders, breadth, value, the flows) is the last *settled* quarter's, for the
                # same reason. Its share counts go on today's basis like the columns (#732).
                f = factors.get(q)
                last = {**cell, **{k: _on_todays_basis(cell.get(k), f) for k in ("shares", "buy_flow", "sell_flow")}}
                last_period = q
        info = meta.get(t, {})
        enr = enrichment.get(t, {})

        # Over the settled quarters only: a quarter still arriving is below the one before it by construction, so
        # it would end every streak and poison the acceleration (#514).
        kept = [i for i, q in enumerate(quarters) if q not in still_arriving]
        s_shares = [shares[i] for i in kept]
        s_chgs = [chgs[i] for i in kept]
        streak = 0
        for i in range(len(s_shares) - 1, 0, -1):
            cur, pr = s_shares[i], s_shares[i - 1]
            if cur is not None and pr is not None and cur > pr:
                streak += 1
            else:
                break
        accel = None
        if len(s_chgs) >= 3 and s_chgs[-1] is not None and s_chgs[-2] is not None:
            accel = s_chgs[-1] - s_chgs[-2]

        pct_out = None
        so = enr.get("shares_outstanding")
        if so and last and last.get("shares"):
            pct_out = last["shares"] / so * 100.0

        price_delta = None
        px = enr.get("price") or {}
        # The close on the same basis as the shares: × the quarter end's split factor (#732).
        prices = {q: px[q] * factors.get(q, 1.0) for q in quarters if px.get(q) is not None}
        price = prices[max(prices)] if prices else None     # the newest quarter-end price the board has
        if len(prices) >= 2:
            pq = sorted(prices)
            fp, lp = prices[pq[0]], prices[pq[-1]]
            if fp and fp > 0:
                price_delta = (lp / fp - 1) * 100.0

        settled_qs = {q for q in quarters if q not in still_arriving}
        entries.append({
            "ticker": t, "name": info.get("name", ""), "sector": _row_sector(info.get("sector")),
            "breaks": _breaks(enr.get("breaks"), quarters, settled_qs, factors, dict(zip(quarters, shares))),
            "shares": shares, "chgs": chgs, "last": last, "last_period": last_period,
            "streak": streak, "accel": accel, "pct_out": pct_out,
            "price_delta": price_delta, "price": price,
            # Not shown since #732 (owner, 12 Nov 2026); kept on the entry for the result history only.
            "short_pct": enr.get("short_pct"), "short_pct_as_of": enr.get("short_pct_as_of"),
            "insider_pct": enr.get("insider_pct"),
            "shares_filed": filed, "split_factor": {q: factors[q] for q in quarters if q in factors},
            "shares_outstanding": so, "float_shares": enr.get("float_shares"),
        })

    order = max((i for i, q in enumerate(quarters) if q not in still_arriving), default=len(quarters) - 1)
    entries.sort(key=lambda e: (e["shares"][order] is None, -(e["shares"][order] or 0)))
    return quarters, entries


def _val(v):
    """The ``data-val`` as a JSON value: the number itself, or ``None`` (JSON null) where the old markup wrote ``""``.

    A NaN or infinite float is ``None`` too: ``JSON.parse`` refuses them, so one would make the whole payload
    unreadable; such a cell sorts with the missing numbers.
    """
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    return v


def _cell(text, val, cls, title=None):
    """One payload cell: ``[text, val, cls]`` (plus ``title`` when given). ``val`` is JSON-ready; ``text`` is raw
    (the page writes it with ``textContent``, so it is never markup)."""
    return [text, _val(val), cls, title] if title is not None else [text, _val(val), cls]


def _payload(quarters: list, entries: list, fields: dict | None = None) -> dict:
    """``{"cols", "rows", "fields"}`` for the payload route (#429, KF #375): the cells as data plus filter rows.

    The filter rows ride the payload, not the HTML, so a 14k-row board stays small.
    """
    by_field = {r["ticker"]: r for r in (fields or [])}
    cols = ([f"sh_{q}" for q in quarters] + [f"ch_{q}" for q in quarters[1:]]
            + [c for c, _ in _SNAP_COLS])
    rows = []
    for e in entries:
        last = e["last"] or {}
        netflow = None
        if last.get("buy_flow") is not None:
            netflow = (last.get("buy_flow") or 0) - (last.get("sell_flow") or 0)
        new_exit = None
        if last.get("new_holders") is not None:
            new_exit = f'+{last["new_holders"]}/−{last["exited_holders"]}'
        new_exit_val = ((last.get("new_holders") or 0) - (last.get("exited_holders") or 0)
                        if last.get("new_holders") is not None else None)
        cells = []
        for q, s, s_filed in zip(quarters, e["shares"], e.get("shares_filed") or e["shares"]):
            if s is None:
                cells.append(_cell("—", None, "zero"))
            elif s_filed is not None and s_filed != s:
                cells.append(_cell(_fmt_shares(s), s, "zero",
                                   f"{s:,.0f} shares on today's basis; {s_filed:,.0f} as filed, split factor "
                                   f"{e['split_factor'].get(q)}"))
            else:
                cells.append(_cell(_fmt_shares(s), s, "zero", f"{s:,.0f} shares"))
        for c in e["chgs"][1:]:
            if c is None:
                cells.append(_cell("—", None, "zero"))
            else:
                cells.append(_cell(_f(c), c, _sign_class(c)))
        # The snapshot is the last settled quarter's, which differs per row; each cell says which (#521).
        of = f"as of {_qlabel(e['last_period'])}" if e.get("last_period") else None
        cells.append(_cell(_fmt_money(last.get("value")), last.get("value"), "zero", of))
        cells.append(_cell(_f(e["pct_out"], 1, "%", False), e["pct_out"], "zero", of))
        cells.append(_cell(str(_num(last.get("holders"))), last.get("holders"), "zero", of))
        cells.append(_cell(new_exit or "—", new_exit_val, "zero", of))
        breadth = last.get("breadth")
        breadth_chg = last.get("breadth_chg")
        breadth_text = "—"
        if breadth is not None:
            breadth_text = f"{breadth:.2f}%"
            if breadth_chg is not None:
                breadth_text += f" ({breadth_chg:+.2f})"
        cells.append(_cell(breadth_text, breadth, "zero", of))
        cells.append(_cell(_f(last.get("top10_pct"), 1, "%", False), last.get("top10_pct"), "zero", of))
        cells.append(_cell(str(_num(last.get("hhi"))), last.get("hhi"), "zero", of))
        cells.append(_cell(_fmt_shares(netflow), netflow, _sign_class(netflow), of))
        cells.append(_cell(str(e["streak"]) if e["streak"] is not None else "—", e["streak"], "zero"))
        cells.append(_cell(_f(e["accel"], 1, "pp"), e["accel"], _sign_class(e["accel"])))
        cells.append(_cell(_f(e["price_delta"]), e["price_delta"], _sign_class(e["price_delta"])))
        breaks = [[_qlabel(b["period"]), _break_lines(b)] for b in e.get("breaks") or []]
        rows.append([e["ticker"], e["name"], _sector(e, by_field), cells, breaks])
    return {"cols": cols, "rows": rows, "fields": by_field}


def _sector(entry: dict, by_field: dict) -> str:
    """The row's sector: the equity spine's (``universe.filter_fields``, the same sector the Sector filter matches),
    else the board's own name/sector meta (crypto, indices and names off the spine). The meta table alone left
    every name ``Unclassified`` once it stopped being filled (owner's screenshot, 12 Nov 2026)."""
    spine = _row_sector((by_field.get(entry["ticker"]) or {}).get("sector"))
    return spine if spine != UNCLASSIFIED else entry["sector"]


def board_payload(totals: dict, meta: dict, enrichment: dict = None, fields: list | None = None) -> dict:
    """The board's rows outside its HTML (#429, K10 #383 C6a): ``{"cols", "rows", "fields"}``.

    Built from ``board_entries`` (the same one pass the page renders), so a row is the ticker, name, sector and one
    ``[text, val, cls]`` cell per column; ``val`` is the ``data-val`` the page sorts and filters on. The page loads
    it from ``GET /view/module_sec_13f/payload.json`` with ``rcPayload``. Filter rows ride the same payload (KF).
    """
    quarters, entries = board_entries(totals, meta, enrichment)
    if fields is None:
        fields, _ = _kit_rows(entries)
    return _payload(quarters, entries, fields)


def render_sec13f_dashboard(totals: dict, meta: dict, enrichment: dict = None,
                            provider_health: dict | None = None, notice: str | None = None) -> str:
    quarters, entries = board_entries(totals, meta, enrichment)
    fields, kit_notice = _kit_rows(entries)
    return _render_page(quarters, entries, fields, kit_notice, provider_health, notice,
                        arriving(totals, quarters), quarter_status(totals, quarters))


DEFAULT_TYPE = "value_investor"     # the owner's example (12 Nov 2026: UW's "Value / Energy Investors" chart)


def types_section_html(filers: dict[str, int] | None = None) -> str:
    """The collapsed "Top holdings by investor type" section (#760): a type picker and the chart host. The chart is
    fetched from ``/api/sec_13f/types/<tag>`` when the section is opened, so a closed section costs nothing. Each
    option carries its type's filers in the newest quarter (``filers``; owner's pick of 13 Nov 2026), and a type with
    none is shown disabled; with no counts at all (a store that cannot say) the options are bare."""
    from RevenueChaser.core.ownership import SEC13F_TYPES

    def option(tag: str, label: str) -> str:
        n = (filers or {}).get(tag, 0) if filers else None
        text = label if n is None else f"{label} ({n:,})"
        state = " disabled" if n == 0 else (" selected" if tag == DEFAULT_TYPE else "")
        return f'<option value="{esc(tag)}"{state}>{esc(text)}</option>'
    options = "".join(option(tag, label) for tag, label in SEC13F_TYPES)
    return ('<details class="types" id="types"><summary>Top holdings by investor type</summary>'
            '<div class="types-body"><div class="types-pick"><label for="type-pick">Investor type</label>'
            f'<select id="type-pick">{options}</select></div>'
            '<div id="type-chart"></div><div id="type-note" aria-live="polite"></div></div></details>')


def _render_page(quarters: list, entries: list, fields: list, kit_notice: str | None = None,
                 provider_health: dict | None = None, notice: str | None = None,
                 still_arriving: set[str] | frozenset[str] = frozenset(),
                 status: list[tuple[str, str]] = ()) -> str:
    q_labels = {q: _qlabel(q) + (ARRIVING_MARK if q in still_arriving else "") for q in quarters}
    settled = [q for q in quarters if q not in still_arriving]

    default_sort = f"sh_{settled[-1]}" if settled else (f"sh_{quarters[-1]}" if quarters else "ticker")
    n = len(quarters)
    table_snap = [(c, label) for c, label in _SNAP_COLS if c in _TABLE_SNAP]
    ns = len(table_snap)
    ncols = 2 + n + ns          # Ticker, Sector, one cell per quarter (shares over Δ%, avo 12 Nov 2026), the snapshot

    header = (
        '<tr>'
        '<th class="left" rowspan="2" data-col="ticker" onclick="sortTable(this.dataset.col,this)">Ticker</th>'
        '<th class="left" rowspan="2" data-col="sector" onclick="sortTable(this.dataset.col,this)">Sector</th>'
        f'<th class="group" colspan="{n}">Total ownership (shares) per quarter · Δ% vs prior below</th>'
        f'<th class="group" colspan="{ns}">Latest settled snapshot · more on click</th>'
        '</tr><tr>'
        # One header per quarter: the label sorts by shares, the small Δ% under it by the change (avo 12 Nov 2026:
        # shares and Δ% stacked in one cell, "daha az yer kaplar ve okumasi kolay olur").
        + ''.join(f'<th data-col="sh_{esc(q)}" onclick="sortTable(this.dataset.col,this)"'
                  f' class="{("sort-desc " if settled and q == settled[-1] else "")}">{esc(q_labels[q])}'
                  + (f'<button type="button" class="chg-sort" data-col="ch_{esc(q)}"'
                     f' onclick="event.stopPropagation();sortTable(this.dataset.col,this)">Δ%</button>' if i else '')
                  + '</th>'
                  for i, q in enumerate(quarters))
        + ''.join(f'<th data-col="{c}" onclick="sortTable(this.dataset.col,this)">{label}</th>' for c, label in table_snap)
        + '</tr>'
    )

    # The rows are no longer inline markup (#429): the page loads them from the payload route and builds them
    # with ``createElement``/``textContent``, so the HTML stays chrome-and-scripts whatever the row count.
    if entries:
        rows_html = f'<tr><td colspan="{ncols}" class="empty">Loading rows…</td></tr>'
    else:
        rows_html = (f'<tr><td colspan="{ncols}" class="empty">No SEC 13F data stored yet. Press Fetch to '
                     'download and ingest the newest SEC Form 13F data set.</td></tr>')

    col_labels = {**{f"sh_{q}": q_labels[q] for q in quarters}, **{c: label for c, label in _SNAP_COLS}}
    js = fill(JS, {"__DEFAULT_SORT__": script_json(default_sort),
                   "__ROW_COUNT__": script_json(len(entries)), "__NCOLS__": script_json(ncols),
                   "__COL_LABELS__": script_json(col_labels),
                   "__TABLE_SNAP__": script_json(list(_TABLE_SNAP))})
    payload_js = f"<script>{PAYLOAD_JS}</script>"

    # Shared filter kit (KF, #375 Card E): Index, Market cap, Sector, Theme. No Earnings facet.
    kit_themes = _kit_themes()
    kit_meta = filters_meta(
        "sec_13f",
        facets=["index", "mc", "gics", "theme"],
        themes=kit_themes,
        explain={"mc": "Latest market value from <code>equity_universe</code>. Pick one or more size buckets, "
                       "or type a custom range in $ millions (min ≤ cap &lt; max). Names not on the equity "
                       "spine show as <b>Unclassified</b> with no market cap, so any cap filter hides them."})
    meta_js = f"<script>var META = {script_json(kit_meta)};</script>"

    from RevenueChaser.core.ownership import sec13f_type_filers
    types_html = types_section_html(sec13f_type_filers())
    status_html = ("<div class=\"status\">" + "".join(f'<span class="{esc(c)}">{esc(t)}</span>' for c, t in status)
                   + "</div>") if status else ""

    arriving_legend = ""
    if still_arriving:
        names = [_qlabel(q) for q in sorted(still_arriving)]
        arriving_legend = (
            f"<b>{ARRIVING_MARK}</b> {', '.join(names)} {'is' if len(names) == 1 else 'are'} <b>still arriving</b>: "
            "the filing deadline has not passed, so the shares are what has been filed so far. No change, streak "
            "or acceleration is computed against it, and the snapshot columns are the last settled quarter's, named "
            "on each cell (#514).")

    help_block = help_html(
        "Every fund manager with more than $100M files a <b>Form 13F</b> each quarter listing its stock "
        "holdings. This board adds them up per company: how many shares all 13F filers hold, the change "
        "against the prior quarter, new and exited holders, concentration and the streak of quarters with "
        "net buying. The legend under the table defines each column; click a row for its chart. "
        "<b>Fetch</b> downloads the newest SEC 13F data set from <b>SEC EDGAR</b> and stores the totals; it also "
        "reads names, sectors and shares outstanding from <b>Unusual Whales</b>. "
        "<b>Refresh</b> redraws the board from the stored totals with no network call.")
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>RevenueChaser — SEC 13F Ownership</title>
<script>{THEME_JS}</script><style>{THEME_CSS}</style>
<script src="{PLOTLY_CDN_URL}"></script>
{CSS}{FILTERS_CSS}</head><body>
<div class="header"><h1>SEC 13F — Total Institutional Ownership</h1>
<div class="meta">{len(entries)} tickers · all 13F filers (incl. BlackRock/Vanguard/State Street) · quarterly · click a row for the chart</div>
{status_html}</div>
{provider_health_html(provider_health)}{notice_html(notice)}{notice_html(kit_notice)}
{help_block}
{recorded_sessions_html("module_sec_13f")}
{filters_bar_html("rc-filters")}
<div class="kit-count" id="kit-count" aria-live="polite"></div>
{types_html}
<div class="board-search"><input type="search" id="board-q" placeholder="Search ticker or name" autocomplete="off" spellcheck="false"></div>
<table><thead>{header}</thead><tbody id="rows">{rows_html}</tbody></table>
<div class="legend-note">
<b>Total ownership</b> = shares held by every 13F filer, on <b>today's share basis</b>: a quarter's count is divided
by the split factor of its quarter end, so a split is not read as buying (hover a cell for the count as filed).
The board shows the last {WINDOW_QUARTERS} settled quarters; the chart shows every stored one.
<b>Δ% vs prior</b> = QoQ change in shares, shown small under each quarter's count (click Δ% in the header to sort by it).
<b>Value</b> = $ market value held. <b>% Out</b> = shares ÷ shares outstanding (newest stored).
<b>New/Exit</b> = holders entered / exited vs prior quarter.
<b>Breadth</b> = share of that quarter's 13F filers holding the name (holders ÷ filers), with the change in
percentage points vs the name's own previous stored quarter, which is not necessarily the calendar quarter
before it; as of quarter-end, filed up to 45 days later.
<b>Top10%/HHI</b> = concentration among the 13F filers' own holdings (the top 10 filers' share of the 13F
total, and its Herfindahl index), not of all shares outstanding.
<b>Net flow</b> = shares bought − sold (holder-level deltas). <b>Streak</b> = consecutive quarters of net accumulation.
<b>Accel</b> = Δ%(latest) − Δ%(prior).
{arriving_legend}
<b>Price Δ</b> = close change from the first to the last quarter end shown, on the same share basis.
<b>Flows</b> (Δ%, Net flow, New/Exit) count only the filers that reported in both quarters; a filer that started or
stopped filing is left out, and the <b class="warn-mark">⚠</b> beside a ticker lists those breaks by quarter.
A quarter is <b>closed</b> once the SEC publishes the data set that covers its filing deadline; a closed quarter
does not change.
</div>
<div id="breaks" role="dialog" aria-modal="true" aria-labelledby="breaks-title"><div id="breaks-card">
<div id="breaks-head"><span id="breaks-title"></span><button type="button" id="breaks-close" aria-label="Close">×</button></div>
<div id="breaks-note">These filers are left out of the flow. Newest first; shares on today's basis.</div>
<ol id="breaks-list"></ol>
</div></div>
<div id="modal"><div id="modal-card">
<div id="modal-head"><span id="modal-title"></span><button id="modal-close">×</button></div>
<div id="chart"></div>
</div></div>
<footer>RevenueChaser — SEC 13F · SEC EDGAR bulk data · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</footer>
{payload_js}{meta_js}<script>{FILTERS_JS}</script>{watchlist_html("sec_13f")}{js}</body></html>"""


def _pct(cur, prev):
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / prev * 100.0


def export_sec13f_dashboard(totals: dict, meta: dict, enrichment: dict = None, filename: str = None,
                            provider_health: dict | None = None, notice: str | None = None) -> Path:
    # One ``board_entries`` pass feeds both the page and the payload (#429); filter rows ride the payload (KF).
    quarters, entries = board_entries(totals, meta, enrichment)
    fields, kit_notice = _kit_rows(entries)
    path = OUTPUT_DIR / (filename or f"sec_13f/sec_13f_latest.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _payload(quarters, entries, fields) if entries else None
    return write_board(path, _render_page(quarters, entries, fields, kit_notice, provider_health, notice,
                                         arriving(totals, quarters), quarter_status(totals, quarters)), payload)
