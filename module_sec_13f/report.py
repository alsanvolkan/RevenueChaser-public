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


CSS = """<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font-family:'Segoe UI',Consolas,monospace;padding:var(--s5);font-size:13px}
.header{margin-bottom:var(--s4);padding-bottom:16px;border-bottom:1px solid var(--border)}
.header h1{color:var(--bright);font-size:22px;letter-spacing:1px}
.header .meta{color:var(--muted);font-size:12px;margin-top:4px}
.board-search{margin:10px 0}
.board-search input{background:var(--card);color:var(--fg);border:1px solid var(--border);padding:6px 10px;border-radius:8px;font-size:12px;width:260px}
.board-search input:focus{outline:1px solid var(--focus);border-color:var(--focus)}
.kit-count{color:var(--muted);font-size:12px;margin:8px 0 12px;min-height:1.2em}
.kit-hide{display:none}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:right;padding:var(--s2) 8px;color:var(--muted);font-size:11px;font-weight:600;letter-spacing:.3px;border-bottom:2px solid var(--border);cursor:pointer;white-space:nowrap}
th:hover{color:var(--fg)}
th.group{cursor:default;color:var(--muted);text-align:center}
th.sort-asc::after{content:' ▲';color:var(--focus)}
th.sort-desc::after{content:' ▼';color:var(--focus)}
th.left,td.left{text-align:left}
td{padding:var(--s2) 8px;border-bottom:1px solid var(--card);white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums}
tbody.grp{border-bottom:1px solid var(--card)}
tbody.grp:hover{background:var(--hover-faint);cursor:pointer}
tbody.grp td{border-bottom:0}
tbody.grp tr:first-child td{padding-top:var(--s2)}
tbody.grp tr:last-child td{padding-bottom:var(--s2)}
.rowlabel{text-align:left;color:var(--muted);font-size:11px;white-space:nowrap}
.qhead{white-space:nowrap}
.qsort,.dsort{background:none;border:0;padding:0;font:inherit;color:inherit;cursor:pointer}
.dsort{margin-left:6px;color:var(--muted);border:1px solid var(--border);border-radius:4px;
 padding:0 5px;line-height:1.5}
.qsort:hover,.dsort:hover{color:var(--fg)}
.qsort:focus-visible,.dsort:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.qsort.sort-asc::after{content:' ▲';color:var(--focus)}
.qsort.sort-desc::after{content:' ▼';color:var(--focus)}
.dsort.sort-asc,.dsort.sort-desc{color:var(--focus);border-color:var(--focus)}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.ticker{color:var(--bright);font-weight:700}
.name{color:var(--muted);font-size:11px;font-weight:400}
.pos{color:var(--pos)}.neg{color:var(--neg)}.zero{color:var(--muted)}
.empty{padding:40px;color:var(--muted)}
#modal{position:fixed;inset:0;background:var(--scrim);display:none;align-items:center;justify-content:center;z-index:100}
#modal.open{display:flex}
#modal-card{background:var(--card);border:1px solid var(--border);border-radius:10px;width:min(900px,94vw);max-height:92vh;overflow:auto;padding:var(--s4)}
#modal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
#modal-title{color:var(--bright);font-size:16px;font-weight:700}
#modal-close{background:transparent;border:0;color:var(--muted);font-size:20px;cursor:pointer}
.pane{border-top:1px solid var(--border)}
.pane-head{display:flex;align-items:center;justify-content:space-between;gap:var(--s3);padding:var(--s3) 0 var(--s2)}
.pane-title{color:var(--fg);font-size:13px}
.expand{display:inline-flex;align-items:center;gap:6px;min-height:32px;padding:0 10px;background:transparent;
 border:1px solid var(--input-border);border-radius:6px;color:var(--muted);font:inherit;font-size:12px;
 cursor:pointer;transition:color 150ms ease,border-color 150ms ease}
.expand:hover{color:var(--fg);border-color:var(--fg)}
.expand:focus-visible{outline:2px solid var(--focus);outline-offset:1px}
.plot{min-height:320px}
#modal-card[data-expanded] .plot{min-height:70vh}
#modal-card[data-expanded="shares"] .pane[data-pane="value"],
#modal-card[data-expanded="value"] .pane[data-pane="shares"]{display:none}
.spin{display:block;width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--focus);border-radius:50%;animation:r .7s linear infinite;margin:40px auto}
@keyframes r{to{transform:rotate(360deg)}}
.status{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.status span{display:inline-flex;align-items:center;min-height:28px;padding:0 10px;border-radius:14px;font-size:12px;font-weight:600}
.status .closed{background:color-mix(in oklab, var(--pos) 14%, transparent);color:var(--pos)}
.status .waiting{background:color-mix(in oklab, var(--amber) 14%, transparent);color:var(--amber)}
.break-mark{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;margin:-12px 0 -12px 2px;border:0;border-radius:22px;background:transparent;color:var(--amber);cursor:pointer;vertical-align:middle}
.break-mark:hover,.break-mark:focus-visible{background:color-mix(in oklab, var(--amber) 16%, transparent);outline:none}
#breaks{position:fixed;inset:0;background:var(--scrim);display:none;align-items:center;justify-content:center;z-index:110}
#breaks.open{display:flex}
#breaks-card{background:var(--card);border:1px solid var(--border);border-radius:10px;width:min(440px,94vw);max-height:86vh;overflow:auto;padding:var(--s4)}
#breaks-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
#breaks-title{color:var(--bright);font-size:15px;font-weight:700}
#breaks-close{width:44px;height:44px;background:transparent;border:0;color:var(--muted);font-size:20px;cursor:pointer}
#breaks-note{color:var(--muted);font-size:12px;margin:4px 0 12px;line-height:1.5}
#breaks-list{list-style:none;display:flex;flex-direction:column;gap:8px}
#breaks-list li{background:color-mix(in oklab, var(--amber) 8%, transparent);border-radius:8px;padding:10px 12px;font-size:13px;line-height:1.5;white-space:normal}
#breaks-list .bul{list-style:disc;margin:6px 0 0;padding-left:18px}
#breaks-list .bul li{background:none;border-radius:0;padding:0;margin:3px 0;font-size:13px}
#breaks-list b{color:var(--bright)}
.warn-box{background:color-mix(in oklab, var(--amber) 10%, transparent);border:1px solid var(--amber);color:var(--amber);border-radius:8px;padding:var(--s3) 16px;margin-bottom:14px;font-size:13px;line-height:1.55}
.warn-box b{color:var(--amber)}
footer{margin-top:24px;padding-top:12px;border-top:1px solid var(--border);color:var(--muted);font-size:11px;text-align:center}
</style>"""

JS = """<script>
var FIELDS = {};
var RCF = null;
var SEARCH_Q = '';
var SORT_COL = __DEFAULT_SORT__;
var SORT_ASC = false;
var ROW_COUNT = __ROW_COUNT__;
var NCOLS = __NCOLS__;
var BREAKS = {};
var MARK_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
  + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2 21h20L12 3z"></path>'
  + '<path d="M12 10v4"></path><path d="M12 17.5v.01"></path></svg>';
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
    var lines = items[i][1] || [];
    var ul = document.createElement('ul');          // the lines read as a list, so they are one (owner, 12 Nov)
    ul.className = 'bul';
    for(var j = 0; j < lines.length; j++){
      var d = document.createElement('li');
      d.textContent = lines[j];
      ul.appendChild(d);
    }
    li.appendChild(ul);
    list.appendChild(li);
  }
  document.getElementById('breaks').classList.add('open');
  document.getElementById('breaks-close').focus();
}
function closeBreaks(){ document.getElementById('breaks').classList.remove('open'); }
function cellVal(group, col){ var td = group.querySelector('td[data-col="'+col+'"]'); return td ? td.getAttribute('data-val') : ''; }
function sortTable(col, el){
  if(SORT_COL === col){ SORT_ASC = !SORT_ASC; } else { SORT_COL = col; SORT_ASC = false; }
  document.querySelectorAll('.qsort,.dsort,th[data-col]').forEach(function(el2){
    el2.classList.remove('sort-asc','sort-desc');
  });
  el.classList.add(SORT_ASC ? 'sort-asc' : 'sort-desc');
  var table = document.getElementById('board');
  var rows = Array.from(table.querySelectorAll('tbody.grp'));
  rows.sort(function(a,b){
    var av = cellVal(a, col), bv = cellVal(b, col);
    var aNull = av === '', bNull = bv === '';
    if(aNull || bNull) return (aNull && bNull) ? 0 : (aNull ? 1 : -1);
    var an = Number(av), bn = Number(bv);
    var r = (!isNaN(an) && !isNaN(bn)) ? an - bn : (av < bv ? -1 : (av > bv ? 1 : 0));
    return SORT_ASC ? r : -r;
  });
  rows.forEach(function(r){ table.appendChild(r); });
  applyFilters();
}
var PANES = [['shares', 'chart-shares'], ['value', 'chart-value']];
function openChart(ticker){
  var modal = document.getElementById('modal');
  modal.classList.add('open');
  document.getElementById('modal-title').textContent = ticker;
  collapsePane();
  for(var i = 0; i < PANES.length; i++){
    document.getElementById(PANES[i][1]).innerHTML = '<span class="spin"></span>';
  }
  fetch('/api/sec_13f/' + encodeURIComponent(ticker))
    .then(function(r){ return r.json(); })
    .then(function(d){
      var figs = (d && d.figs) || [];
      for(var i = 0; i < PANES.length; i++){
        var el = document.getElementById(PANES[i][1]);
        if(!figs[i]){ el.innerHTML = '<div class="zero">No series data.</div>'; continue; }
        Plotly.react(PANES[i][1], figs[i], {}, {displayModeBar: true, responsive: true});
      }
    })
    .catch(function(){
      for(var i = 0; i < PANES.length; i++){
        document.getElementById(PANES[i][1]).innerHTML = '<div class="neg">Failed to load chart.</div>';
      }
    });
}
// A pane expands on its own (owner, 12 Nov 2026): the card goes wide, the other pane folds away, and the
// figure is relaid out to the taller box. Pressing it again puts both panes back.
function expandPane(pane){
  var card = document.getElementById('modal-card');
  card.setAttribute('data-expanded', pane);
  document.querySelectorAll('.expand').forEach(function(b){
    var on = b.getAttribute('data-pane-for') === pane;
    b.setAttribute('aria-expanded', on ? 'true' : 'false');
    b.querySelector('.expand-text').textContent = on ? 'Click to shrink' : 'Click to expand';
  });
  resizePanes();
}
function collapsePane(){
  var card = document.getElementById('modal-card');
  card.removeAttribute('data-expanded');
  document.querySelectorAll('.expand').forEach(function(b){
    b.setAttribute('aria-expanded', 'false');
    b.querySelector('.expand-text').textContent = 'Click to expand';
  });
  resizePanes();
}
function resizePanes(){
  if(typeof Plotly === 'undefined' || !Plotly.Plots) return;
  for(var i = 0; i < PANES.length; i++){
    var el = document.getElementById(PANES[i][1]);
    if(el && el.querySelector('.plotly')){ Plotly.Plots.resize(el); }
  }
}
function closeModal(){ collapsePane(); document.getElementById('modal').classList.remove('open'); }
function applyFilters(){
  var kitOn = !!(RCF && RCF.active()), q = SEARCH_Q.toLowerCase();
  var rows = document.querySelectorAll('tbody.grp');
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
  // One ticker is one <tbody class="grp"> of two rows (#772): shares on the first, Δ% on the second, under the
  // same quarter columns. The per-ticker cells (ticker, sector, the snapshot) span both rows.
  var table = document.getElementById('board');
  var msg = document.getElementById('rows');
  var frag = document.createDocumentFragment();
  var rows = (p && p.rows) || [];
  var cols = (p && p.cols) || [];
  FIELDS = (p && p.fields) || {};
  var nq = 0, nch = 0;
  for(var c = 0; c < cols.length; c++){
    if(cols[c].indexOf('sh_') === 0) nq++;
    else if(cols[c].indexOf('ch_') === 0) nch++;
  }
  for(var i = 0; i < rows.length; i++){
    var r = rows[i];
    var body = document.createElement('tbody');
    body.className = 'grp';
    body.setAttribute('data-ticker', r[0]);
    var trS = document.createElement('tr');
    var trD = document.createElement('tr');
    var tdT = document.createElement('td');
    tdT.className = 'left';
    tdT.setAttribute('rowspan', '2');
    tdT.setAttribute('data-col', 'ticker');
    tdT.setAttribute('data-val', r[0]);
    // The watch-list box comes before the ticker (owner, 12 Nov 2026); rcWatchButtons escapes, and gives
    // nothing for a ticker the dashboard refuses.
    tdT.insertAdjacentHTML('afterbegin', rcWatchButtons(r[0]));
    var spT = document.createElement('span');
    spT.className = 'ticker';
    spT.textContent = r[0];
    tdT.appendChild(spT);
    if(r[4] && r[4].length){
      BREAKS[r[0]] = r[4];
      var mk = document.createElement('button');
      mk.type = 'button';
      mk.className = 'break-mark';
      mk.setAttribute('aria-label', 'Filer breaks for ' + r[0]);
      mk.setAttribute('title', 'Filers started or stopped reporting ' + r[0] + ': open for the list');
      mk.innerHTML = MARK_SVG;
      tdT.appendChild(mk);
    }
    if(r[1]){
      var spN = document.createElement('span');
      spN.className = 'name';
      spN.textContent = r[1];
      tdT.appendChild(spN);
    }
    trS.appendChild(tdT);
    var tdS = document.createElement('td');
    tdS.className = 'left';
    tdS.setAttribute('rowspan', '2');
    tdS.setAttribute('data-col', 'sector');
    tdS.setAttribute('data-val', r[2]);
    tdS.textContent = r[2];
    trS.appendChild(tdS);
    trS.appendChild(rowLabel('shares'));
    trD.appendChild(rowLabel('Δ vs prior'));
    // The oldest quarter has no change to show, and the Δ row still has to line up under it. The filler goes
    // in before the loop: the DOM this runs against (tests included) has no childNodes to count afterwards.
    if(nch === nq - 1){ trD.appendChild(cell(['—', null, 'zero'], 'ch_none')); }
    var cells = r[3] || [];
    for(var j = 0; j < cols.length; j++){
      var col = cols[j];
      var td = cell(cells[j] || [], col);
      if(col.indexOf('sh_') === 0){
        trS.appendChild(td);
      } else if(col.indexOf('ch_') === 0){
        trD.appendChild(td);
      } else {
        td.setAttribute('rowspan', '2');
        trS.appendChild(td);
      }
    }
    body.appendChild(trS);
    body.appendChild(trD);
    frag.appendChild(body);
  }
  var old = table.querySelectorAll('tbody.grp');
  for(var k = 0; k < old.length; k++){ table.removeChild(old[k]); }
  while(msg.firstChild){ msg.removeChild(msg.firstChild); }
  table.appendChild(frag);
  initKit();
}
function rowLabel(text){
  var td = document.createElement('td');
  td.className = 'rowlabel';
  td.textContent = text;
  return td;
}
function cell(c, col){
  var td = document.createElement('td');
  td.setAttribute('data-col', col);
  td.setAttribute('data-val', (c[1] === null || c[1] === undefined) ? '' : String(c[1]));
  td.className = c[2] || '';
  if(c[3] !== null && c[3] !== undefined){ td.setAttribute('title', c[3]); }
  td.textContent = (c[0] === null || c[0] === undefined) ? '' : c[0];
  return td;
}
function fail(e){
  var table = document.getElementById('board');
  var groups = table.querySelectorAll('tbody.grp');
  for(var i = 0; i < groups.length; i++){ table.removeChild(groups[i]); }
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
  var q = document.getElementById('board-q');
  if(q){
    q.addEventListener('input', function(e){ SEARCH_Q = e.target.value; applyFilters(); });
  }
  document.getElementById('board').addEventListener('click', function(e){
    if(e.target.closest('.rc-watch') || e.target.closest('th')) return;
    var grp = e.target.closest('tbody.grp');
    if(e.target.closest('.break-mark')){ if(grp){ openBreaks(grp.getAttribute('data-ticker')); } return; }
    if(grp){ openChart(grp.getAttribute('data-ticker')); }
  });
  document.getElementById('modal').addEventListener('click', function(e){ if(e.target === this) closeModal(); });
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('modal-card').addEventListener('click', function(e){
    var btn = e.target.closest('.expand');
    if(!btn) return;
    var pane = btn.getAttribute('data-pane-for');
    var card = document.getElementById('modal-card');
    if(card.getAttribute('data-expanded') === pane){ collapsePane(); } else { expandPane(pane); }
  });
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


def _break_lines(e: dict) -> list[str]:
    """The lines of one quarter in the break list: stopped first (the shares that left the count), then started."""
    lines = []
    for kind, verb in (("stopped", "stopped filing"), ("started", "started filing")):
        filers = e[kind]
        if not filers:
            continue
        total = sum(f["shares"] or 0 for f in filers)
        lines.append(f"{len(filers)} filer{'s' if len(filers) != 1 else ''} {verb}, {_fmt_shares(total)} shares")
        named = ", ".join(f"{f['filer']} ({_fmt_shares(f['shares'])})" for f in filers[:BREAK_NAMES])
        more = f" and {len(filers) - BREAK_NAMES} more" if len(filers) > BREAK_NAMES else ""
        lines.append(f"Largest: {named}{more}")
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


def build_chart_figs(ticker: str, periods: dict, enrichment: dict = None) -> list[dict]:
    """Two figures: shares + price, and $ value + holders, both over quarters.

    They were one two-row subplot until #772. Each is its own figure now because each pane carries its own
    "click to expand" (owner, 12 Nov 2026), and a subplot cannot be grown on its own.

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

    top = make_subplots(specs=[[{"secondary_y": True}]])
    top.add_trace(go.Bar(x=labels, y=shares, name="Shares", marker=dict(color=THEME_SCALE["c1"], pattern=pattern),
                         hovertemplate="%{y:,.0f}"))
    if price:
        top.add_trace(go.Scatter(x=labels, y=price, name="Price",
                                 line=dict(color=THEME_SCALE["c2"], width=2)), secondary_y=True)

    bottom = make_subplots(specs=[[{"secondary_y": True}]])
    bottom.add_trace(go.Bar(x=labels, y=value, name="Value ($)",
                            marker=dict(color=THEME_SCALE["c3"], pattern=pattern),
                            hovertemplate="%{y:,.0f}"))
    n = len(qs) - len(still_arriving)           # the settled quarters come first: the arriving set is a suffix
    bottom.add_trace(go.Scatter(x=labels[:n], y=holders[:n], name="Holders",
                                line=dict(color=THEME_SCALE["c4"], width=2)), secondary_y=True)
    if still_arriving:
        start = max(n - 1, 0)
        bottom.add_trace(go.Scatter(x=labels[start:], y=holders[start:], name="Holders (filed so far)",
                                    line=dict(color=THEME_SCALE["c4"], width=2, dash="dash"),
                                    showlegend=False), secondary_y=True)
        names = ", ".join(_qlabel(q) for q in sorted(still_arriving))
        note = f"{ARRIVING_MARK} {names} still arriving: filed so far, not a drop"
        for fig in (top, bottom):
            fig.add_annotation(text=note, xref="paper", yref="paper", x=0, y=-0.14, xanchor="left",
                               yanchor="top", showarrow=False,
                               font=dict(size=12, color=THEME_TOKENS["dark"]["muted"]))
    for fig, title in ((top, f"{ticker} — total shares & price (today's share basis)"),
                       (bottom, "market value ($) & holders")):
        fig.update_layout(template="plotly_dark", height=320, hovermode="x unified", title=dict(text=title, x=0),
                          # Anchored at its bottom so a legend that wraps on a phone grows up into the margin.
                          legend=dict(orientation="h", y=1.04, yanchor="bottom", x=0),
                          margin=dict(l=60, r=60, t=86, b=70 if still_arriving else 40))
        fig.update_xaxes(type="category")
    return [top.to_dict(), bottom.to_dict()]


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


def _render_page(quarters: list, entries: list, fields: list, kit_notice: str | None = None,
                 provider_health: dict | None = None, notice: str | None = None,
                 still_arriving: set[str] | frozenset[str] = frozenset(),
                 status: list[tuple[str, str]] = ()) -> str:
    q_labels = {q: _qlabel(q) + (ARRIVING_MARK if q in still_arriving else "") for q in quarters}
    settled = [q for q in quarters if q not in still_arriving]

    default_sort = f"sh_{settled[-1]}" if settled else (f"sh_{quarters[-1]}" if quarters else "ticker")
    n = len(quarters)
    ns = len(_SNAP_COLS)
    ncols = 3 + n + ns          # Ticker, Sector, the metric label, one column per quarter, the snapshot

    # Shares and Δ% share one quarter column each (owner, 12 Nov 2026): the quarter is written once in the
    # header and the two numbers sit on two lines of the same row group, so the table is n + snapshot columns
    # wide instead of n + (n-1) + snapshot. The quarter head sorts by shares; the small Δ beside it sorts by
    # that quarter's change, which is the sort the old Δ% block carried.
    header = (
        '<tr>'
        '<th class="left" rowspan="2" data-col="ticker" onclick="sortTable(this.dataset.col,this)">Ticker</th>'
        '<th class="left" rowspan="2" data-col="sector" onclick="sortTable(this.dataset.col,this)">Sector</th>'
        '<th class="rowhead" rowspan="2"><span class="sr-only">Metric</span></th>'
        f'<th class="group" colspan="{n}">Shares held, and Δ% against the quarter before</th>'
        f'<th class="group" colspan="{ns}">Latest settled snapshot</th>'
        '</tr><tr>'
        + ''.join('<th class="qhead">'
                  f'<button type="button" class="qsort{(" sort-desc" if settled and q == settled[-1] else "")}"'
                  f' data-col="sh_{esc(q)}" onclick="sortTable(this.dataset.col,this)">{esc(q_labels[q])}</button>'
                  + ('' if i == 0 else
                     f'<button type="button" class="dsort" data-col="ch_{esc(q)}"'
                     f' title="Sort by the change into {esc(_qlabel(q))}"'
                     f' aria-label="Sort by the change into {esc(_qlabel(q))}"'
                     ' onclick="sortTable(this.dataset.col,this)">Δ</button>')
                  + '</th>'
                  for i, q in enumerate(quarters))
        + ''.join(f'<th data-col="{c}" onclick="sortTable(this.dataset.col,this)">{label}</th>' for c, label in _SNAP_COLS)
        + '</tr>'
    )

    # The rows are no longer inline markup (#429): the page loads them from the payload route and builds them
    # with ``createElement``/``textContent``, so the HTML stays chrome-and-scripts whatever the row count.
    if entries:
        rows_html = f'<tr><td colspan="{ncols}" class="empty">Loading rows…</td></tr>'
    else:
        rows_html = (f'<tr><td colspan="{ncols}" class="empty">No SEC 13F data stored yet. Press Fetch to '
                     'download and ingest the newest SEC Form 13F data set.</td></tr>')

    js = fill(JS, {"__DEFAULT_SORT__": script_json(default_sort),
                   "__ROW_COUNT__": script_json(len(entries)), "__NCOLS__": script_json(ncols)})
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
<script src="{PLOTLY_CDN_URL}"></script>
<script>{THEME_JS}</script><style>{THEME_CSS}</style>
{CSS}{FILTERS_CSS}</head><body>
<div class="header"><h1>SEC 13F — Total Institutional Ownership</h1>
<div class="meta">{len(entries)} tickers · all 13F filers (incl. BlackRock/Vanguard/State Street) · quarterly · click a row for the chart</div>
{status_html}</div>
{provider_health_html(provider_health)}{notice_html(notice)}{notice_html(kit_notice)}
{help_block}
{recorded_sessions_html("module_sec_13f")}
{filters_bar_html("rc-filters")}
<div class="kit-count" id="kit-count" aria-live="polite"></div>
<div class="board-search"><input type="search" id="board-q" placeholder="Search ticker or name" autocomplete="off" spellcheck="false"></div>
<table id="board"><thead>{header}</thead><tbody id="rows">{rows_html}</tbody></table>
<div class="legend">
<b>Total ownership</b> = shares held by every 13F filer, on <b>today's share basis</b>: a quarter's count is divided
by the split factor of its quarter end, so a split is not read as buying (hover a cell for the count as filed).
The board shows the last {WINDOW_QUARTERS} settled quarters; the chart shows every stored one.
<b>Δ% vs prior</b> = QoQ change in shares.
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
<div id="modal-head"><span id="modal-title"></span><button id="modal-close" aria-label="Close">×</button></div>
<div class="pane" data-pane="shares"><div class="pane-head"><span class="pane-title">Shares held &amp; price</span><button type="button" class="expand" data-pane-for="shares" aria-expanded="false"><span class="expand-text">Click to expand</span><svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false"><path d="M9.5 2.5H13.5V6.5M6.5 13.5H2.5V9.5M13.5 2.5l-4.6 4.6M2.5 13.5l4.6-4.6" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg></button></div>
<div id="chart-shares" class="plot"></div></div>
<div class="pane" data-pane="value"><div class="pane-head"><span class="pane-title">Market value &amp; holders</span><button type="button" class="expand" data-pane-for="value" aria-expanded="false"><span class="expand-text">Click to expand</span><svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false"><path d="M9.5 2.5H13.5V6.5M6.5 13.5H2.5V9.5M13.5 2.5l-4.6 4.6M2.5 13.5l4.6-4.6" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg></button></div>
<div id="chart-value" class="plot"></div></div>
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
