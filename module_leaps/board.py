"""The LEAPS board (#746): far-dated contracts, every name, and how much of a day's volume opened positions.

One session at a time, read from ``option_far_contract`` (``rcdata.read.far_contracts``), the table the
``uw/far_contracts`` dataset builds: contracts 183 days or more from expiry with $50k premium or more on the day.
For each the board shows the day's volume and premium, open interest before and after, and the **opened share**:

    (next session's open interest - open interest) / the day's volume

+1 means the volume became new positions, 0 that positions changed hands, -1 that positions were closed. It is a
measurement, not a label. Until ``MIN_SESSIONS`` sessions are stored and tested the board says "Collecting" and
ranks nothing on its own: the default sort is premium.

Read-only and rendered in memory on every load: the board fetches nothing, writes nothing to ``.cache`` and reads
no old table. The design is the mock avo approved on 12 Nov 2026 (14:14 Amsterdam, "Tamam uygundur"): theme D,
cards on a phone, a table-like row from 900 px, the shared watch-list pair before the ticker, value and change
stacked in one cell, one data colour for the bar, and amber for warnings only.
"""
from __future__ import annotations

import html
import re
from datetime import date

from RevenueChaser.core.html_safe import script_json
from RevenueChaser.core.reporting_common import THEME_CSS, THEME_JS, help_html, watchlist_html
from RevenueChaser.rcdata import read

BOARD = "leaps"          # the X-RC-Panel value of its watch-list pair, as before (#730)
MIN_SESSIONS = 20
PAGE_ROWS = 100
# UW's issue_type for an index option; these roots are index products whatever the field says.
INDEX_ROOTS = frozenset({"SPX", "SPXW", "RUT", "RUTW", "VIX", "VIXW", "NDX", "NDXP", "XSP", "DJX", "OEX", "XEO"})
_OCC = re.compile(r"^(?P<root>[A-Z0-9.]{1,6})(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?P<cp>[CP])(?P<k>\d{8})$")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def product_of(ticker, issue_type) -> str:
    """``stocks``, ``etfs`` or ``index``: the board's three groups, from UW's ``issue_type``."""
    kind = str(issue_type or "").strip().lower()
    if str(ticker or "").upper() in INDEX_ROOTS or kind == "index":
        return "index"
    if kind in ("etf", "etn", "etp", "fund"):
        return "etfs"
    return "stocks"


def _strike_text(k) -> str:
    k = float(k)
    return f"{k:,.0f}" if k == int(k) else f"{k:,.2f}".rstrip("0").rstrip(".")


def _expiry_text(d: date) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def partner_label(symbol) -> str:
    """``NVDA271217C00100000`` -> ``C100 Dec 2027``: a roll partner, short enough for a chip."""
    m = _OCC.match(str(symbol or ""))
    if not m:
        return str(symbol or "")
    k = int(m["k"]) / 1000
    return f"{m['cp']}{_strike_text(k)} {_MONTHS[int(m['m']) - 1]} 20{m['y']}"


def _num(v):
    return None if v is None else float(v)


def _int(v):
    return None if v is None else int(v)


def row_of(r: dict) -> dict:
    """One stored contract as the page's compact row (short keys: the payload carries thousands)."""
    exp = r["expiry"]
    side = "C" if str(r.get("option_type") or "").lower().startswith("c") else "P"
    return {
        "t": r["ticker"], "sym": r["option_symbol"], "side": side,
        "con": f"{'Call' if side == 'C' else 'Put'} {_strike_text(r['strike'])} · {_expiry_text(exp)}",
        "dte": _int(r.get("days_to_expiry")), "prod": product_of(r["ticker"], r.get("issue_type")),
        "prem": _num(r.get("premium")), "vol": _int(r.get("volume")),
        "oi": _int(r.get("open_interest")), "next": _int(r.get("oi_next")), "chg": _int(r.get("oi_change")),
        "st": r.get("oi_status") or "pending", "share": _num(r.get("opened_share")),
        "ask": _num(r.get("ask_share")), "multi": _num(r.get("multileg_share")),
        "out": bool(r.get("out_of_range")), "roll": partner_label(r["roll_partner"]) if r.get("roll") else None,
    }


def payload(session_date=None) -> dict | None:
    """What the page draws for one session; ``None`` while no session is stored. An unknown ``session_date``
    falls back to the newest session rather than an empty board."""
    sessions = read.far_contract_sessions()
    if not sessions:
        return None
    days = [s["session_date"] for s in sessions]
    chosen = session_date if session_date in days else days[0]
    info = sessions[days.index(chosen)]
    rows = sorted((row_of(r) for r in read.far_contracts(chosen)),
                  key=lambda x: (-(x["prem"] or 0), x["t"], x["sym"]))
    return {
        "session": chosen.isoformat(), "stored": len(sessions), "min_sessions": MIN_SESSIONS,
        "collecting": len(sessions) < MIN_SESSIONS,
        "sessions": [{"date": s["session_date"].isoformat(), "contracts": s["contracts"]} for s in sessions],
        "counts": {"contracts": info["contracts"], "names": info["tickers"], "matched": info["matched"],
                   "pending": info["pending"], "stale": info["stale"] + info["missing"]},
        "rows": rows,
    }


def parse_session(text) -> date | None:
    try:
        return date.fromisoformat(str(text)) if text else None
    except ValueError:
        return None


HELP = help_html(
    "<p><b>Opened share</b> = (next session's open interest − open interest) ÷ the day's volume. +1: the volume "
    "became new positions. 0: positions changed hands. −1: positions were closed. Beyond ±1, open interest moved "
    "more than the day's volume (other days' trades settled into it).</p>"
    "<p>Contracts 183 days or more from expiry with $50k premium or more on the day, every name, from Unusual "
    "Whales. Open interest is read the next trading morning from 07:30 New York; <b>stale</b> means that read "
    "did not line up with this session, so no share is shown. A <b>roll</b> pairs a closing contract with an "
    "opening one of the same name and side on the same day.</p>"
    "<p>Not answered here: who traded, and whether the opener bought or sold. Until 20 sessions are stored "
    "and tested the board shows raw numbers only, with no ranking or label.</p>"
    "<p>There is no <b>Fetch</b> on this tab: the builder sweeps each session after the New York close and reads "
    "its open interest the next morning, on its own schedule. <b>Refresh</b> redraws the page from the stored "
    "table; it calls no provider.</p>")

_CSS = """
body{margin:0;background:var(--bg);color:var(--fg);font:var(--fs-base)/1.4 var(--font-sans);font-variant-numeric:tabular-nums}
.lp{max-width:1600px;margin:0 auto;padding:var(--sp-3);display:flex;flex-direction:column;gap:var(--sp-2);box-sizing:border-box}
.lp *{box-sizing:border-box}
.lp-muted{color:var(--muted)}
.lp-top{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap}
.lp-top h1{margin:0;font-size:var(--fs-lg);font-weight:600;color:var(--bright)}
.lp-top .sub{font-size:var(--fs-xs);color:var(--muted)}
.lp-collect{display:flex;align-items:center;gap:var(--sp-2);margin-left:auto;font-size:var(--fs-sm);color:var(--bright)}
.lp-track{position:relative;width:96px;height:6px;border-radius:var(--r-pill);background:var(--grid)}
.lp-track>i{position:absolute;top:0;bottom:0;left:0;border-radius:var(--r-pill);background:var(--data-1)}
.lp-controls{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--sp-2)}
.lp-controls>#lp-session,.lp-seg,.lp-counts{grid-column:1/-1}
.lp-pick{display:contents}
.lp select{height:var(--touch);border-radius:var(--r-sm);border:1px solid var(--input-border);background:var(--input-bg);
  color:var(--bright);padding:0 var(--sp-2);font:500 var(--fs-sm) var(--font-sans);min-width:0}
.lp-seg{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:2px;padding:2px;border-radius:var(--r-sm);
  background:var(--card-2);border:1px solid var(--border)}
.lp-seg button{min-height:40px;border:0;border-radius:var(--r-sm);background:transparent;color:var(--fg);cursor:pointer;
  font:500 var(--fs-sm) var(--font-sans)}
.lp-seg button[aria-pressed="true"]{background:var(--accent-soft);color:var(--accent);font-weight:600}
.lp-seg button span{color:var(--muted);font-weight:400;margin-left:4px;font-size:var(--fs-xs)}
.lp-counts{display:flex;flex-wrap:wrap;gap:2px var(--sp-3);font-size:var(--fs-xs);color:var(--muted)}
.lp-counts b{color:var(--bright);font-weight:600}
.lp-counts .warn b{color:var(--amber)}
.lp button:focus-visible,.lp select:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.lp-head{display:none}
.lp-rows{display:flex;flex-direction:column;gap:var(--sp-2)}
.ct{display:grid;grid-template-columns:auto minmax(0,1fr) auto;grid-template-areas:"w n p" "s s s" "x x x";
  gap:var(--sp-2);align-items:center;padding:var(--sp-2) var(--sp-3);border-radius:var(--r-md);background:var(--card);
  border:1px solid var(--border)}
.c-w{grid-area:w}.c-n{grid-area:n;min-width:0}.c-p{grid-area:p}.c-s{grid-area:s}.c-x{grid-area:x}.c-f{grid-column:1/-1}
.c-n{display:flex;flex-direction:column;min-width:0}
.c-n b,.c-p b{font-size:var(--fs-base);font-weight:600;color:var(--bright)}
.c-n span{font-size:var(--fs-xs);color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.c-p{text-align:right}
/* The watch-list pair as symbols, the way 13F draws it (avo 12 Nov 2026: "OPT ve STK cok yer kapliyor sadece sembol
   kullanarak coz"): the text is hidden, a mask icon in currentColor takes its place, the aria-label and title name
   the list. 28 px halves with a mouse, 44 px on a touch screen. */
.lp .rc-watch{margin:0;gap:0;border:1px solid var(--input-border);border-radius:var(--r-sm);overflow:hidden;flex:none}
.lp .rc-watch button{border:0;border-radius:0;min-width:28px;min-height:28px;width:28px;height:28px;padding:0;font-size:0;
  line-height:0;background:transparent;color:var(--muted);display:inline-flex;align-items:center;justify-content:center}
.lp .rc-watch button::before{content:"";width:14px;height:14px;background:currentColor;
  -webkit-mask:var(--ico) center/contain no-repeat;mask:var(--ico) center/contain no-repeat}
.lp .rc-watch button[data-rc-watch="option"]{--ico:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M13 2 4 14h7l-1 8 9-12h-7l1-8z'/%3E%3C/svg%3E")}
.lp .rc-watch button[data-rc-watch="stock"]{--ico:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 20V10M10 20V4M16 20v-7M22 20H2'/%3E%3C/svg%3E")}
@media (pointer:coarse){.lp .rc-watch button{min-width:44px;min-height:44px;width:44px;height:44px}
  .lp .rc-watch button::before{width:18px;height:18px}}
.lp .rc-watch button+button{border-left:1px solid var(--input-border)}
.lp .rc-watch button[aria-pressed="true"]{background:var(--accent);color:var(--primary-fg)}
.sh{display:flex;align-items:center;gap:var(--sp-2)}
.sh .lab{font-size:var(--fs-xs);color:var(--muted);flex:none}
.sh .val{font-size:var(--fs-base);font-weight:700;color:var(--bright);width:48px;text-align:right;flex:none}
.sh .bar{position:relative;flex:1;height:6px;border-radius:var(--r-pill);background:var(--grid)}
.sh .bar i{position:absolute;top:0;bottom:0;border-radius:var(--r-pill);background:var(--data-1)}
.sh .bar s{position:absolute;top:-3px;bottom:-3px;left:50%;width:1px;background:var(--muted)}
.note{display:flex;gap:var(--sp-2);align-items:center;padding:2px var(--sp-2);border-radius:var(--r-sm);
  font-size:var(--fs-xs);background:var(--card-2);color:var(--fg)}
.note i{width:6px;height:6px;flex:none;border-radius:var(--r-pill);background:var(--muted)}
.note.warn{background:var(--amber-soft);color:var(--amber)}
.note.warn i{background:var(--amber)}
.c-x{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--sp-2)}
.c-x div{display:flex;flex-direction:column;min-width:0}
.c-x small{font-size:var(--fs-xs);color:var(--muted)}
.c-x b{font-size:var(--fs-sm);font-weight:600;color:var(--bright);overflow-wrap:anywhere}
.c-x em{font-style:normal;font-size:var(--fs-xs);color:var(--muted);white-space:nowrap}
.c-x em:empty{display:none}
.c-f{display:flex;flex-wrap:wrap;gap:4px}
.c-f:empty{display:none}
.chip{padding:1px var(--sp-2);border-radius:var(--r-sm);font-size:var(--fs-xs);font-weight:500;background:var(--accent-soft);
  color:var(--accent)}
.chip.warn{background:var(--amber-soft);color:var(--amber)}
.lp-more,.lp-index{min-height:var(--touch);border-radius:var(--r-md);border:1px dashed var(--input-border);
  background:transparent;color:var(--fg);font:500 var(--fs-sm) var(--font-sans);padding:var(--sp-2) var(--sp-3);
  cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:var(--sp-2);text-align:left}
.lp-more[hidden],.lp-index[hidden]{display:none}
.lp-empty{padding:var(--sp-5);text-align:center;color:var(--muted);border:1px dashed var(--border);border-radius:var(--r-md)}
@media (min-width:900px){
  .lp{padding:var(--sp-4) var(--sp-5);gap:var(--sp-3)}
  .lp-controls{display:flex;flex-wrap:wrap;align-items:center;gap:var(--sp-2)}
  .lp-controls>#lp-session{min-width:240px}
  .lp-seg{display:flex}.lp-seg button{padding:0 var(--sp-3)}
  .lp-counts{margin-left:auto}
  .lp-table{border-radius:var(--r-md);background:var(--card);border:1px solid var(--border);overflow:hidden}
  .lp-head,.ct{display:grid;grid-template-columns:60px minmax(0,1.7fr) 64px minmax(0,1.3fr) minmax(0,2fr) minmax(0,1.1fr);
    grid-template-areas:"w n p s x f";gap:var(--sp-3);padding:4px var(--sp-3);align-items:center}
  .lp-head{font-size:var(--fs-xs);color:var(--muted);background:var(--card-2);border-bottom:1px solid var(--border);
    padding-top:6px;padding-bottom:6px}
  .lp-head .r{text-align:right}
  .lp-head .x{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--sp-2);text-align:right}
  .lp-rows{gap:0}
  .ct{min-height:40px;border:0;border-bottom:1px solid var(--border);border-radius:0;background:transparent}
  .ct:hover{background:var(--hover-faint)}
  .c-n{flex-direction:row;align-items:baseline;gap:var(--sp-2)}
  .ct .sh .lab{display:none}
  .c-x small{display:none}
  .c-x div{align-items:flex-end}
  .c-x b{font-weight:500}
  .c-f{grid-area:f}
}
"""

_JS = r"""
var LP = {product: 'stocks', side: 'all', sort: 'premium', shown: PAGE_ROWS};
function lpMoney(v){ if(v == null) return '–'; var a = Math.abs(v);
  if(a >= 1e9) return '$' + (v / 1e9).toFixed(1) + 'B'; if(a >= 1e6) return '$' + (v / 1e6).toFixed(1) + 'M';
  if(a >= 1e3) return '$' + Math.round(v / 1e3) + 'k'; return '$' + Math.round(v); }
function lpInt(v){ return v == null ? '–' : Math.round(v).toLocaleString('en-US'); }
function lpSigned(v){ return v == null ? '' : (v > 0 ? '+' : v < 0 ? '−' : '') + lpInt(Math.abs(v)); }
function lpPct(v){ return v == null ? '–' : Math.round(v * 100) + '%'; }
function lpShare(v){ return (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v).toFixed(2); }
function lpBar(v){ var c = Math.max(-1, Math.min(1, v)), w = Math.abs(c) * 50;
  return 'left:' + (c >= 0 ? 50 : 50 - w) + '%;width:' + w + '%'; }
var LP_NOTES = {pending: ['', 'Open interest is read the next trading morning from 07:30 New York.', 'Read next morning, 07:30 NY'],
  stale: ['warn', 'Stale: the next-morning read did not line up with this session, so no share is shown.', 'Stale: read did not match'],
  missing: ['warn', 'Missing: the contract was not in the next-morning chain, so no share is shown.', 'Missing from next chain'],
  noshare: ['', 'Open interest was read, but the day has no volume to divide by, so no share is shown.', 'No volume: no share']};
function lpCard(r){
  var e = rcEsc, mid;
  if(r.st === 'matched' && r.share != null){
    mid = '<div class="sh"><span class="lab">Opened</span><div class="bar" role="img" aria-label="Opened share ' +
      lpShare(r.share) + ' on a scale from minus 1 to plus 1"><s></s><i style="' + lpBar(r.share) + '"></i></div>' +
      '<b class="val">' + lpShare(r.share) + '</b></div>';
  } else {
    var n = LP_NOTES[r.st === 'matched' ? 'noshare' : r.st] || LP_NOTES.pending;
    mid = '<div class="note ' + n[0] + '" title="' + e(n[1]) + '"><i aria-hidden="true"></i><span>' + e(n[2]) +
      '</span></div>';
  }
  var oiSub = r.st === 'matched' && r.chg != null ? lpSigned(r.chg) : (r.st === 'pending' ? 'next morning' : '?');
  var stats = [['Volume', lpInt(r.vol), ''], ['Open int.', r.st === 'matched' && r.next != null ? lpInt(r.next) : lpInt(r.oi), oiSub,
                'Open interest before: ' + lpInt(r.oi)],
               ['At ask', lpPct(r.ask), ''], ['Multi-leg', lpPct(r.multi), '']];
  var flags = (r.out ? '<span class="chip warn">Beyond ±1</span>' : '') +
              (r.roll ? '<span class="chip">Roll ↔ ' + e(r.roll) + '</span>' : '');
  return '<article class="ct" data-sym="' + e(r.sym) + '"><div class="c-w">' + rcWatchButtons(r.t) + '</div>' +
    '<div class="c-n"><b>' + e(r.t) + '</b><span>' + e(r.con) + '</span></div>' +
    '<div class="c-p" title="Premium"><b>' + lpMoney(r.prem) + '</b></div><div class="c-s">' + mid + '</div>' +
    '<div class="c-x">' + stats.map(function(s){ return '<div' + (s[3] ? ' title="' + e(s[3]) + '"' : '') +
      '><small>' + s[0] + '</small><b>' + e(s[1]) +
      '</b><em>' + e(s[2]) + '</em></div>'; }).join('') + '</div>' + (flags ? '<div class="c-f">' + flags + '</div>' : '') +
    '</article>';
}
var LP_SORTS = {
  premium: function(r){ return -(r.prem || 0); },
  share: function(r){ return r.share == null ? Infinity : -r.share; },
  volume: function(r){ return -(r.vol || 0); },
  change: function(r){ return r.chg == null ? Infinity : -Math.abs(r.chg); }
};
function lpVisible(){
  var key = LP_SORTS[LP.sort] || LP_SORTS.premium;
  return LP_DATA.rows.filter(function(r){
    return r.prod === LP.product && (LP.side === 'all' || r.side === LP.side);
  }).map(function(r, i){ return [key(r), i, r]; }).sort(function(a, b){
    return a[0] - b[0] || a[1] - b[1]; }).map(function(x){ return x[2]; });
}
function lpDraw(){
  var rows = lpVisible(), box = document.getElementById('lp-rows');
  box.innerHTML = rows.length ? rows.slice(0, LP.shown).map(lpCard).join('')
    : '<div class="lp-empty">No contract in this group for this session.</div>';
  var more = document.getElementById('lp-more');
  more.hidden = rows.length <= LP.shown;
  more.querySelector('span').textContent = 'Show more · ' + Math.min(rows.length, LP.shown) + ' of ' + rows.length;
  var idx = document.getElementById('lp-index');
  idx.hidden = LP.product === 'index' || !LP_COUNT.index;
  document.querySelectorAll('.lp-seg button').forEach(function(b){
    b.setAttribute('aria-pressed', b.getAttribute('data-product') === LP.product ? 'true' : 'false'); });
  rcWatchSync(box);
}
var LP_COUNT = {stocks: 0, etfs: 0, index: 0};
LP_DATA.rows.forEach(function(r){ LP_COUNT[r.prod] = (LP_COUNT[r.prod] || 0) + 1; });
document.querySelectorAll('.lp-seg button').forEach(function(b){
  b.querySelector('span').textContent = lpInt(LP_COUNT[b.getAttribute('data-product')] || 0);
  b.addEventListener('click', function(){ LP.product = b.getAttribute('data-product'); LP.shown = PAGE_ROWS; lpDraw(); });
});
document.getElementById('lp-index').querySelector('b').textContent = lpInt(LP_COUNT.index);
document.getElementById('lp-index').addEventListener('click', function(){ LP.product = 'index'; LP.shown = PAGE_ROWS; lpDraw(); });
document.getElementById('lp-side').addEventListener('change', function(ev){ LP.side = ev.target.value; LP.shown = PAGE_ROWS; lpDraw(); });
document.getElementById('lp-sort').addEventListener('change', function(ev){ LP.sort = ev.target.value; lpDraw(); });
document.getElementById('lp-more').addEventListener('click', function(){ LP.shown += PAGE_ROWS; lpDraw(); });
document.getElementById('lp-session').addEventListener('change', function(ev){
  if(ev.target.value && ev.target.value !== LP_DATA.session) location.search = '?session=' + encodeURIComponent(ev.target.value); });
if(!LP_COUNT.stocks){ LP.product = LP_COUNT.etfs ? 'etfs' : (LP_COUNT.index ? 'index' : 'stocks'); }
lpDraw();
"""


def _session_label(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')[d.weekday()]} {d.day} {_MONTHS[d.month - 1]} {d.year}"


def render_html(data: dict) -> str:
    """The whole board for one session's ``payload``."""
    e = html.escape
    c = data["counts"]
    options = "".join(
        f'<option value="{e(s["date"])}"{" selected" if s["date"] == data["session"] else ""}>'
        f'{e(_session_label(s["date"]))} · {s["contracts"]:,} contracts</option>' for s in data["sessions"])
    stored, need = data["stored"], data["min_sessions"]
    if data["collecting"]:
        state = (f'<div class="lp-collect" title="Raw numbers only until {need} sessions are stored and tested.">'
                 f'<b>Collecting</b><span class="lp-track" role="img" aria-label="{stored} of {need} sessions stored">'
                 f'<i style="width:{min(100, stored * 100 // need)}%"></i></span><span>{stored} / {need} sessions</span>'
                 f'</div>')
    else:
        state = f'<div class="lp-collect lp-muted">{stored} sessions stored.</div>'
    counts = "".join(
        f'<span{cls}><b>{n:,}</b> {label}</span>'
        for n, label, cls in ((c["contracts"], "contracts", ""), (c["names"], "names", ""),
                              (c["matched"], "matched", ""), (c["pending"], "waiting for open interest", ""),
                              (c["stale"], "stale or missing", ' class="warn"' if c["stale"] else ""))
        if label != "waiting for open interest" or n)
    # While collecting nothing is ranked by the measure itself, so the opened-share sort comes with the 20th session.
    share_sort = "" if data["collecting"] else '<option value="share">Sort: opened share</option>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LEAPS</title><script>{THEME_JS}</script><style>{THEME_CSS}{_CSS}</style></head>
<body><main class="lp">
<header class="lp-top"><h1>LEAPS</h1><span class="sub">183+ days · $50k+ premium · every name</span>{state}</header>
<div class="lp-controls">
<select id="lp-session" aria-label="Session">{options}</select>
<div class="lp-seg" role="group" aria-label="Product">
<button type="button" data-product="stocks" aria-pressed="true">Stocks<span></span></button>
<button type="button" data-product="etfs" aria-pressed="false">ETFs<span></span></button>
<button type="button" data-product="index" aria-pressed="false">Index<span></span></button></div>
<div class="lp-pick"><select id="lp-side" aria-label="Side"><option value="all">Calls and puts</option>
<option value="C">Calls</option><option value="P">Puts</option></select>
<select id="lp-sort" aria-label="Sort"><option value="premium">Sort: premium</option>
{share_sort}<option value="volume">Sort: volume</option>
<option value="change">Sort: open interest change</option></select></div>
<div class="lp-counts">{counts}</div>
</div>
<div class="lp-table"><div class="lp-head" aria-hidden="true"><span>Watch</span><span>Contract</span>
<span class="r">Premium</span><span>Opened share</span><span class="x"><span>Volume</span><span>Open int.</span>
<span>At ask</span><span>Multi-leg</span></span>
<span>Flags</span></div><div class="lp-rows" id="lp-rows"></div></div>
<button type="button" class="lp-more" id="lp-more" hidden><span></span><span aria-hidden="true">›</span></button>
<button type="button" class="lp-index" id="lp-index" hidden><span>Index products (SPX, SPXW, RUT, VIX)</span>
<span class="lp-muted"><b></b> ›</span></button>
{HELP}
</main>
{watchlist_html(BOARD)}
<script>
var PAGE_ROWS = {PAGE_ROWS};
var LP_DATA = {script_json(data)};
{_JS}</script></body></html>"""
