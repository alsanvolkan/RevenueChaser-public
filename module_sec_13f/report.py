"""SEC Form 13F dashboard — true total institutional ownership (all filers).

Renders a sortable, sector-filterable table of per-ticker quarterly totals
sourced from SEC 13F bulk data (includes BlackRock/Vanguard/State Street).

Inputs:
  ``totals`` = ``{ticker: {period: {shares, value, holders, new_holders,
                 exited_holders, top10_pct, hhi, buy_flow, sell_flow, ...}}}``: since G9 the ownership store's rows
                 (``store_reads.totals``), each cell with ``complete`` (a quarter still filing says False: it is
                 shown, marked "(filing)", and left out of the snapshot, the streak and Accel)
  ``meta``  = ``{ticker: {"name": str, "sector": str}}`` (the spine, ``store_reads.meta``)
  ``enrichment`` = ``{ticker: {price, shares_outstanding, insider_pct, short_interest, days_to_cover, short_as_of}}``
                   (optional): the snapshot inputs, ``enrich.load_inputs`` (#375 F2: ``ownership_snapshot`` and D01)
                   and FINRA's short interest (``store_reads.short_interest``, G9).
"""
from RevenueChaser.core.reporting_common import (FILTERS_CSS, FILTERS_JS, PAYLOAD_JS, filters_bar_html,
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
body{background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Consolas,monospace;padding:24px;font-size:13px}
.header{margin-bottom:16px;padding-bottom:16px;border-bottom:1px solid #21262d}
.header h1{color:#3fb950;font-size:22px;letter-spacing:1px}
.header .meta{color:#8b949e;font-size:12px;margin-top:4px}
.board-search{margin:10px 0}
.board-search input{background:#161b22;color:#c9d1d9;border:1px solid #21262d;padding:6px 10px;border-radius:8px;font-size:12px;width:260px}
.board-search input:focus{outline:1px solid #58a6ff;border-color:#58a6ff}
.kit-count{color:#8b949e;font-size:12px;margin:8px 0 12px;min-height:1.2em}
.kit-hide{display:none}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:right;padding:8px 8px;color:#8b949e;font-size:11px;font-weight:600;letter-spacing:.3px;border-bottom:2px solid #21262d;cursor:pointer;white-space:nowrap}
th:hover{color:#c9d1d9}
th.group{cursor:default;color:#6e7681;text-align:center}
th.sort-asc::after{content:' ▲';color:#58a6ff}
th.sort-desc::after{content:' ▼';color:#58a6ff}
th.left,td.left{text-align:left}
td{padding:8px 8px;border-bottom:1px solid #161b22;white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums}
tr:hover{background:#161b22}
tr[data-ticker]{cursor:pointer}
.ticker{color:#F1F5F9;font-weight:700}
.name{color:#8b949e;font-size:11px;font-weight:400}
.pos{color:#34D399}.neg{color:#F87171}.zero{color:#8b949e}
.empty{padding:40px;color:#8b949e}
#modal{position:fixed;inset:0;background:rgba(5,17,31,.82);display:none;align-items:center;justify-content:center;z-index:100}
#modal.open{display:flex}
#modal-card{background:#0d1117;border:1px solid #30363d;border-radius:10px;width:min(900px,94vw);max-height:92vh;overflow:auto;padding:16px}
#modal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
#modal-title{color:#F1F5F9;font-size:16px;font-weight:700}
#modal-close{background:transparent;border:0;color:#8b949e;font-size:20px;cursor:pointer}
#chart{min-height:380px}
.spin{display:block;width:16px;height:16px;border:2px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:r .7s linear infinite;margin:40px auto}
@keyframes r{to{transform:rotate(360deg)}}
.warn-box{background:rgba(210,153,29,.10);border:1px solid #d2991d;color:#f0c674;border-radius:8px;padding:12px 16px;margin-bottom:14px;font-size:13px;line-height:1.55}
.warn-box b{color:#d2991d}
footer{margin-top:24px;padding-top:12px;border-top:1px solid #21262d;color:#484f58;font-size:11px;text-align:center}
</style>"""

JS = """<script>
var FIELDS = {};
var RCF = null;
var SEARCH_Q = '';
var SORT_COL = __DEFAULT_SORT__;
var SORT_ASC = false;
var ROW_COUNT = __ROW_COUNT__;
var NCOLS = __NCOLS__;
function cellVal(row, col){ var td = row.querySelector('td[data-col="'+col+'"]'); return td ? td.getAttribute('data-val') : ''; }
function sortTable(col, el){
  if(SORT_COL === col){ SORT_ASC = !SORT_ASC; } else { SORT_COL = col; SORT_ASC = false; }
  document.querySelectorAll('th').forEach(function(th){ th.classList.remove('sort-asc','sort-desc'); });
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
function openChart(ticker){
  var modal = document.getElementById('modal');
  modal.classList.add('open');
  document.getElementById('modal-title').textContent = ticker;
  var chart = document.getElementById('chart');
  chart.innerHTML = '<span class="spin"></span>';
  fetch('/api/sec_13f/' + encodeURIComponent(ticker))
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(!d.fig){ chart.innerHTML = '<div style="color:#8b949e">No series data.</div>'; return; }
      Plotly.react('chart', d.fig, {}, {displayModeBar: true, responsive: true});
    })
    .catch(function(){ chart.innerHTML = '<div style="color:#f85149">Failed to load chart.</div>'; });
}
function closeModal(){ document.getElementById('modal').classList.remove('open'); }
function applyFilters(){
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
    var spT = document.createElement('span');
    spT.className = 'ticker';
    spT.textContent = r[0];
    tdT.appendChild(spT);
    if(r[1]){
      tdT.appendChild(document.createTextNode(' '));
      var spN = document.createElement('span');
      spN.className = 'name';
      spN.textContent = r[1];
      tdT.appendChild(spN);
    }
    // The shared watch-list pair (PR 4): rcWatchButtons escapes, and gives nothing for a ticker the dashboard refuses.
    tdT.insertAdjacentHTML('beforeend', rcWatchButtons(r[0]));
    tr.appendChild(tdT);
    var tdS = document.createElement('td');
    tdS.className = 'left';
    tdS.setAttribute('data-col', 'sector');
    tdS.setAttribute('data-val', r[2]);
    tdS.textContent = r[2];
    tr.appendChild(tdS);
    var cells = r[3] || [];
    for(var j = 0; j < cols.length; j++){
      var c = cells[j] || [];
      var td = document.createElement('td');
      td.setAttribute('data-col', cols[j]);
      td.setAttribute('data-val', (c[1] === null || c[1] === undefined) ? '' : String(c[1]));
      td.className = c[2] || '';
      if(c[3] !== null && c[3] !== undefined){ td.setAttribute('title', c[3]); }
      td.textContent = (c[0] === null || c[0] === undefined) ? '' : c[0];
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
  var q = document.getElementById('board-q');
  if(q){
    q.addEventListener('input', function(e){ SEARCH_Q = e.target.value; applyFilters(); });
  }
  document.getElementById('rows').addEventListener('click', function(e){
    var tr = e.target.closest('tr[data-ticker]');
    if(tr){ openChart(tr.getAttribute('data-ticker')); }
  });
  document.getElementById('modal').addEventListener('click', function(e){ if(e.target === this) closeModal(); });
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.addEventListener('keydown', function(e){ if(e.key === 'Escape') closeModal(); });
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


def _quarters(totals: dict) -> list:
    qs = set()
    for periods in totals.values():
        qs.update(periods.keys())
    return sorted(q for q in qs if q >= engine.START_QUARTER)


def filing_quarters(totals: dict) -> set:
    """The quarters still filing: a served cell says ``complete`` False (its filing deadline has not passed, G9). A
    cell without the key (the old totals) counts as complete."""
    return {q for periods in totals.values() for q, cell in periods.items()
            if isinstance(cell, dict) and cell.get("complete") is False}


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
    """Shares + price (row 1) and $ value + holders (row 2) over quarters."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    qs = sorted(periods.keys())
    labels = [_qlabel(q) for q in qs]
    shares = [periods[q].get("shares") or 0 for q in qs]
    value = [periods[q].get("value") or 0 for q in qs]
    holders = [periods[q].get("holders") or 0 for q in qs]
    price = None
    if enrichment:
        px = (enrichment.get("price") or {})
        price = [px.get(q) for q in qs]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.10,
                        subplot_titles=(f"{ticker} — total shares & price", "market value ($) & holders"),
                        specs=[[{"secondary_y": True}], [{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=labels, y=shares, name="Shares", marker_color="#3fb950",
                         hovertemplate="%{y:,.0f}"), 1, 1)
    if price:
        fig.add_trace(go.Scatter(x=labels, y=price, name="Price",
                                 line=dict(color="#58a6ff", width=2)), 1, 1, secondary_y=True)
    fig.add_trace(go.Bar(x=labels, y=value, name="Value ($)", marker_color="#d2991d",
                         hovertemplate="%{y:,.0f}"), 2, 1)
    fig.add_trace(go.Scatter(x=labels, y=holders, name="Holders",
                             line=dict(color="#3fb950", width=2)), 2, 1, secondary_y=True)
    fig.update_layout(template="plotly_dark", height=620, hovermode="x unified",
                      legend=dict(orientation="h", y=1.10, x=0),
                      margin=dict(l=60, r=60, t=80, b=40))
    fig.update_xaxes(type="category")
    return fig.to_dict()


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
    ("insider", "Insider%"),
    ("short", "Short int"),
    ("price_delta", "Price Δ"),
]


def board_entries(totals: dict, meta: dict, enrichment: dict = None) -> tuple[list, list]:
    """``(quarters, entries)``: the board's quarter columns and one entry per ticker, in board order (most shares in
    the latest quarter first). The board renders these; the result history (``history.py``, #375) records them."""
    quarters = _quarters(totals)
    filing = filing_quarters(totals)
    # The snapshot, the streak and Accel read the complete quarters only: a quarter still filing (late and amended
    # filers arrive up to its deadline and after) would read as selling (G9).
    done = [i for i, q in enumerate(quarters) if q not in filing]
    enrichment = enrichment or {}
    entries = []
    for t, periods in totals.items():
        shares = []
        chgs = []
        prev = None
        last = None
        for q in quarters:
            cell = periods.get(q)
            s = cell["shares"] if cell else None
            shares.append(s)
            chgs.append(None if q == quarters[0] else _pct(s, prev))
            prev = s
            if cell and q not in filing:
                last = cell
        info = meta.get(t, {})
        enr = enrichment.get(t, {})

        streak = 0
        for k in range(len(done) - 1, 0, -1):
            i = done[k]
            if done[k - 1] != i - 1:
                break
            cur, pr = shares[i], shares[i - 1]
            if cur is not None and pr is not None and cur > pr:
                streak += 1
            else:
                break
        accel = None
        if len(done) >= 3 and done[-2] == done[-1] - 1 and done[-3] == done[-1] - 2:
            c1, c2 = chgs[done[-1]], chgs[done[-2]]
            if c1 is not None and c2 is not None:
                accel = c1 - c2

        pct_out = None
        so = enr.get("shares_outstanding")
        if so and last and last.get("shares"):
            pct_out = last["shares"] / so * 100.0

        price_delta = None
        px = enr.get("price") or {}
        prices = {q: px.get(q) for q in quarters if px.get(q) is not None}
        price = prices[max(prices)] if prices else None     # the newest quarter-end price the board has
        if len(prices) >= 2:
            pq = sorted(prices)
            fp, lp = prices[pq[0]], prices[pq[-1]]
            if fp and fp > 0:
                price_delta = (lp / fp - 1) * 100.0

        entries.append({
            "ticker": t, "name": info.get("name", ""), "sector": _row_sector(info.get("sector")),
            "shares": shares, "chgs": chgs, "last": last,
            "streak": streak, "accel": accel, "pct_out": pct_out,
            "price_delta": price_delta,
            "short_interest": enr.get("short_interest"), "days_to_cover": enr.get("days_to_cover"),
            "short_as_of": enr.get("short_as_of"),
            "insider_pct": enr.get("insider_pct"), "price": price,
            "shares_outstanding": so, "float_shares": enr.get("float_shares"),
        })

    # Board order: most shares in the newest complete quarter first (the newest quarter when none is complete).
    k = done[-1] if done else len(quarters) - 1
    entries.sort(key=lambda e: (not e["shares"] or e["shares"][k] is None, -((e["shares"] or [0])[k] or 0)))
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
        for s in e["shares"]:
            if s is None:
                cells.append(_cell("—", None, "zero"))
            else:
                cells.append(_cell(_fmt_shares(s), s, "zero", f"{s:,.0f} shares"))
        for c in e["chgs"][1:]:
            if c is None:
                cells.append(_cell("—", None, "zero"))
            else:
                cells.append(_cell(_f(c), c, _sign_class(c)))
        cells.append(_cell(_fmt_money(last.get("value")), last.get("value"), "zero"))
        cells.append(_cell(_f(e["pct_out"], 1, "%", False), e["pct_out"], "zero"))
        cells.append(_cell(str(_num(last.get("holders"))), last.get("holders"), "zero"))
        apart = [(last.get("exited_confidential"), "left holdings out in confidence"),
                 (last.get("exited_no_filing"), "filed no report this quarter")]
        apart_text = "; ".join(f"{n} exit(s) by filers that {why}" for n, why in apart if n)
        cells.append(_cell(new_exit or "—", new_exit_val, "zero", apart_text) if apart_text
                     else _cell(new_exit or "—", new_exit_val, "zero"))
        breadth = last.get("breadth")
        breadth_chg = last.get("breadth_chg")
        breadth_text = "—"
        if breadth is not None:
            breadth_text = f"{breadth:.2f}%"
            if breadth_chg is not None:
                breadth_text += f" ({breadth_chg:+.2f})"
        cells.append(_cell(breadth_text, breadth, "zero"))
        cells.append(_cell(_f(last.get("top10_pct"), 1, "%", False), last.get("top10_pct"), "zero"))
        cells.append(_cell(str(_num(last.get("hhi"))), last.get("hhi"), "zero"))
        cells.append(_cell(_fmt_shares(netflow), netflow, _sign_class(netflow)))
        cells.append(_cell(str(e["streak"]) if e["streak"] is not None else "—", e["streak"], "zero"))
        cells.append(_cell(_f(e["accel"], 1, "pp"), e["accel"], _sign_class(e["accel"])))
        cells.append(_cell(_f(e["insider_pct"], 1, "%", False), e["insider_pct"], "zero"))
        cells.append(_short_cell(e))
        cells.append(_cell(_f(e["price_delta"]), e["price_delta"], _sign_class(e["price_delta"])))
        rows.append([e["ticker"], e["name"], e["sector"], cells])
    return {"cols": cols, "rows": rows, "fields": {r["ticker"]: r for r in (fields or [])}}


def _short_cell(e: dict) -> list:
    """FINRA short interest in shares, with days to cover, as of its settlement date (G9). Before G9 the column was
    short interest as % of float; the ownership store has no float, so it is not one."""
    si = e.get("short_interest")
    if si is None:
        return _cell("—", None, "zero")
    dtc = e.get("days_to_cover")
    text = _fmt_shares(si) + (f" ({dtc:.1f}d)" if dtc is not None else "")
    as_of = e.get("short_as_of")
    return _cell(text, si, "zero", f"{si:,.0f} shares short" + (f", {dtc:.2f} days to cover" if dtc is not None
                                                               else "") + (f", settled {as_of}" if as_of else ""))


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
                            provider_health: dict | None = None, notice: str | None = None,
                            as_of: str | None = None) -> str:
    quarters, entries = board_entries(totals, meta, enrichment)
    fields, kit_notice = _kit_rows(entries)
    return _render_page(quarters, entries, fields, kit_notice, provider_health, notice,
                        filing=filing_quarters(totals), as_of=as_of)


EMPTY_TEXT = ("The ownership store holds no 13F quarter yet. It fills after the 13F first fill (the SEC's Form 13F "
              "data sets from 2025-01-01, started on the server) and the <code>ownership/serve</code> derive.")


def _render_page(quarters: list, entries: list, fields: list, kit_notice: str | None = None,
                 provider_health: dict | None = None, notice: str | None = None, filing: set | None = None,
                 as_of: str | None = None) -> str:
    filing = filing or set()
    q_labels = {q: _qlabel(q) + (" (filing)" if q in filing else "") for q in quarters}
    complete = [q for q in quarters if q not in filing]

    default_sort = f"sh_{(complete or quarters)[-1]}" if quarters else "ticker"
    n = len(quarters)
    ns = len(_SNAP_COLS)
    ncols = 2 + n + max(0, n - 1) + ns          # Ticker, Sector, shares per quarter, Δ%, the snapshot (13F-7)

    header = (
        '<tr>'
        '<th class="left" rowspan="2" data-col="ticker" onclick="sortTable(this.dataset.col,this)">Ticker</th>'
        '<th class="left" rowspan="2" data-col="sector" onclick="sortTable(this.dataset.col,this)">Sector</th>'
        f'<th class="group" colspan="{n}">Total ownership (shares) per quarter</th>'
        f'<th class="group" colspan="{max(0, n - 1)}">Δ% vs prior quarter</th>'
        f'<th class="group" colspan="{ns}">Latest snapshot</th>'
        '</tr><tr>'
        + ''.join(f'<th data-col="sh_{esc(q)}" onclick="sortTable(this.dataset.col,this)"'
                  f' class="{("sort-desc " if f"sh_{q}" == default_sort else "")}"'
                  + (' title="Still filing: the quarter\'s 13F deadline has not passed"' if q in filing else "")
                  + f'>{esc(q_labels[q])}</th>' for q in quarters)
        + ''.join(f'<th data-col="ch_{esc(q)}" onclick="sortTable(this.dataset.col,this)">{esc(q_labels[q])}</th>' for q in quarters[1:])
        + ''.join(f'<th data-col="{c}" onclick="sortTable(this.dataset.col,this)">{label}</th>' for c, label in _SNAP_COLS)
        + '</tr>'
    )

    # The rows are no longer inline markup (#429): the page loads them from the payload route and builds them
    # with ``createElement``/``textContent``, so the HTML stays chrome-and-scripts whatever the row count.
    if entries:
        rows_html = f'<tr><td colspan="{ncols}" class="empty">Loading rows…</td></tr>'
    else:
        rows_html = f'<tr><td colspan="{ncols}" class="empty">{EMPTY_TEXT}</td></tr>'

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

    short_as_of = None
    for e in entries:
        d = e.get("short_as_of")
        if d is not None and (short_as_of is None or d > short_as_of):
            short_as_of = d
    short_legend = (
        "<b>Short int</b> = FINRA consolidated short interest in shares, with days to cover in brackets "
        "(<code>finra/short_interest</code>, twice a month; settled up to "
        f"{short_as_of.isoformat() if short_as_of else '—'}, published about a week later). The store has no "
        "float yet, so it is not a % of float.")

    help_block = help_html(
        "Every fund manager with more than $100M files a <b>Form 13F</b> each quarter listing its stock "
        "holdings. This board adds them up per company: how many shares all 13F filers hold, the change "
        "against the prior quarter, new and exited holders, concentration and the streak of quarters with "
        "net buying. The legend under the table defines each column; click a row for its chart. "
        "The filings come from <b>SEC EDGAR</b> through the ownership store, which the builder fills on the "
        "server. <b>Fetch</b> asks the builder for one <code>ownership/serve</code> (the totals derived again from "
        "the stored filings, no network) and redraws the board at once with what is stored. "
        "<b>Refresh</b> redraws the board from the store with no network call. A quarter marked "
        "<b>(filing)</b> is still inside its 45-day filing window: it is shown, but the snapshot, the streak and "
        "Accel use the newest complete quarter.")
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>RevenueChaser — SEC 13F Ownership</title>
<script src="{PLOTLY_CDN_URL}"></script>
{CSS}{FILTERS_CSS}</head><body>
<div class="header"><h1>SEC 13F — Total Institutional Ownership</h1>
<div class="meta">{len(entries)} tickers · all 13F filers (incl. BlackRock/Vanguard/State Street) · quarterly · click a row for the chart</div>
{f'<div class="meta">{esc(as_of)}</div>' if as_of else ''}</div>
{provider_health_html(provider_health)}{notice_html(notice)}{notice_html(kit_notice)}
{help_block}
{recorded_sessions_html("module_sec_13f")}
{filters_bar_html("rc-filters")}
<div class="kit-count" id="kit-count" aria-live="polite"></div>
<div class="board-search"><input type="search" id="board-q" placeholder="Search ticker or name" autocomplete="off" spellcheck="false"></div>
<table><thead>{header}</thead><tbody id="rows">{rows_html}</tbody></table>
<div style="color:#8b949e;font-size:11px;margin-top:8px;line-height:1.5">
<b>Total ownership</b> = shares held by every 13F filer. <b>Δ% vs prior</b> = QoQ change in shares.
<b>Value</b> = $ market value held. <b>% Out</b> = shares ÷ shares outstanding (newest stored; empty until the
fundamentals store serves shares outstanding).
<b>New/Exit</b> = holders entered / exited vs the calendar quarter before (hover: exits by a filer that left
holdings out in confidence or filed no report, which may not be sales).
<b>Breadth</b> = share of that quarter's 13F filers holding the name (holders ÷ filers), with the change in
percentage points vs the calendar quarter before (Chen, Hong and Stein 2002: falling breadth is the signal); as of
quarter-end, filed up to 45 days later.
<b>Top10%/HHI</b> = ownership concentration.
<b>Net flow</b> = shares bought − sold (holder-level deltas). <b>Streak</b> = consecutive quarters of net accumulation.
<b>Accel</b> = Δ%(latest) − Δ%(prior). <b>Insider%</b> = newest stored snapshot. {short_legend}
<b>Price Δ</b> = close change since the first quarter end (empty until the price store holds the quarter-end
closes).
</div>
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
                            provider_health: dict | None = None, notice: str | None = None,
                            as_of: str | None = None) -> Path:
    # One ``board_entries`` pass feeds both the page and the payload (#429); filter rows ride the payload (KF).
    quarters, entries = board_entries(totals, meta, enrichment)
    fields, kit_notice = _kit_rows(entries)
    path = OUTPUT_DIR / (filename or f"sec_13f/sec_13f_latest.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _payload(quarters, entries, fields) if entries else None
    page = _render_page(quarters, entries, fields, kit_notice, provider_health, notice,
                        filing=filing_quarters(totals), as_of=as_of)
    return write_board(path, page, payload)
