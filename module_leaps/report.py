"""LEAPS accumulation dashboard — standalone HTML view.

Layout: a pinned Watchlist table on top, then collapsible score tiers
(🔥 High ≥50 / ⚡ Emerging 20–49 / 💤 Quiet <20) for the market scan. Rows are a
scannable grid: [Ticker | Footprint | Flow | Net Notional | Prints | Ask % | ⚠ | ★].

Near-term earnings are an orange ⚠ tag (not a drop). The ★ button pins a name
into ``watchlist_tickers.json`` via ``POST /api/watchlist/toggle/<ticker>``.
"""
import html as _html

from RevenueChaser.core import universe
from RevenueChaser.core.html_safe import script_json
from RevenueChaser.core.reporting_common import (FILTERS_CSS, FILTERS_JS, filters_bar_html, filters_meta, help_html,
                                                 notice_html, provider_health_html, recorded_sessions_html,
                                                 write_artifact)
from datetime import datetime
from pathlib import Path

from RevenueChaser.core.config import OUTPUT_DIR
from RevenueChaser.module_leaps.engine import LABELS, UNWATCHED_CAP

TIER_META = {
    "high": ("🔥 High Conviction", "score ≥ 50"),
    "emerging": ("⚡ Emerging Flow", "score 20–49"),
    "quiet": ("💤 Quiet / Watch", "score < 20"),
}

CSS = """<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Consolas,monospace;padding:24px;font-size:13px}
.header{margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid #21262d;display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}
.header h1{color:#58a6ff;font-size:22px;letter-spacing:1px}
.header .meta{color:#8b949e;font-size:12px;margin-top:4px}
.controls{display:flex;align-items:center;gap:10px}
.controls label{color:#8b949e;font-size:12px;display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none}
.chip-label{color:#8b949e;font-size:12px;margin:0 4px 0 8px}
.controls input{accent-color:#58a6ff}
.tier{margin-bottom:20px}
.tier-head{display:flex;align-items:center;gap:10px;padding:8px 2px;border-bottom:1px solid #21262d;cursor:pointer;user-select:none}
.tier-head .t{font-size:15px;font-weight:700}
.tier-head .sub{color:#8b949e;font-size:11px}
.tier-head .n{margin-left:auto;color:#8b949e;font-size:12px}
.tier-head .caret{color:#58a6ff;transition:transform .15s}
.tier-head.collapsed .caret{transform:rotate(-90deg)}
table{width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed}
th{text-align:left;padding:7px 10px;color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:2px solid #21262d;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
td{padding:7px 10px;border-bottom:1px solid #161b22;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums}
th.num,td.num{text-align:right}
tr:hover{background:#161b22}
.tkr{color:#58a6ff;font-weight:700}
.nm{color:#8b949e;font-size:10px;font-weight:400}
.lbl{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700}
.l-acc{background:#1a3a1a;color:#3fb950;border:1px solid #238636}
.l-dist{background:#2a1515;color:#f85149;border:1px solid #da3633}
.l-quiet{background:#1a1a1a;color:#8b949e;border:1px solid #30363d}
.l-na{background:#1a1a1a;color:#484f58;border:1px solid #30363d}
.score{font-weight:700}
.pos{color:#3fb950}.neg{color:#f87171}.zero{color:#8b949e}
.warn{color:#d2991d;font-weight:700;margin-left:4px}
.pin{background:transparent;border:0;color:#484f58;font-size:15px;cursor:pointer;padding:0 4px}
.pin:hover{color:#d2991d}
.pin.on{color:#d2991d}
footer{margin-top:26px;padding-top:12px;border-top:1px solid #21262d;color:#484f58;font-size:11px;text-align:center}
.note{color:#8b949e;font-size:11px;margin-top:6px;line-height:1.6}
.legend{margin-top:22px;padding:14px 16px;border:1px solid #21262d;border-radius:8px;background:#12161c}
.legend h3{color:#c9d1d9;font-size:13px;margin-bottom:8px}
.lg-row{color:#8b949e;font-size:11px;line-height:1.6;margin:4px 0}
.lg-row b{color:#c9d1d9;font-weight:600}
.empty-state{margin:22px 0;padding:22px 18px;border:1px dashed #30363d;border-radius:8px;color:#8b949e;text-align:center;line-height:1.6}
.empty-state b{color:#c9d1d9}
tr.kit-hide{display:none}
.kit-count{color:#8b949e;font-size:12px;margin:8px 0 12px}
</style>"""

JS = """<script>
function toggleTier(head){
  var table = head.parentElement.querySelector('table');
  head.classList.toggle('collapsed');
  if(table){ table.style.display = head.classList.contains('collapsed') ? 'none' : ''; }
}
function toggleQuiet(cb){
  var q = document.getElementById('tier-quiet');
  if(q){ q.style.display = cb.checked ? 'none' : ''; }
}
function toggleLabel(cb){
  var on = {}, chips = document.querySelectorAll('.label-chip');
  for(var i=0;i<chips.length;i++){ on[chips[i].getAttribute('data-label')] = chips[i].checked; }
  var rows = document.querySelectorAll('#tier-watchlist tbody tr');
  for(var j=0;j<rows.length;j++){
    rows[j].style.display = (on[rows[j].getAttribute('data-label')] === false) ? 'none' : '';
  }
}
// Shared filter kit (KL, #377): hides rows that fail the kit in every tier, the Watchlist too (avo 2026-10-16,
// KL card "Watchlist de"). It ANDs with the Watchlist label chips. Rows are matched to the kit's fields by data-ticker.
var RCF = null, KIT = {}, KIT_TIERS = ['watchlist','high','emerging','quiet'];
function applyKit(api){
  var on = !!(api && api.active()), shown = 0, total = 0, ids = KIT_TIERS;
  for(var i=0;i<ids.length;i++){
    var tier = document.getElementById('tier-' + ids[i]);
    if(!tier){ continue; }
    var rows = tier.querySelectorAll('tbody tr'), tShown = 0;
    for(var j=0;j<rows.length;j++){
      var f = KIT[rows[j].getAttribute('data-ticker')];
      var ok = !on || !f || api.pass(f);
      rows[j].classList.toggle('kit-hide', !ok);
      if(ok){ tShown++; }
    }
    var n = tier.querySelector('.n');
    if(n){ n.textContent = on ? (tShown + '/' + rows.length) : String(rows.length); }
    shown += tShown; total += rows.length;
  }
  var line = document.getElementById('kit-count');
  if(line){ line.textContent = on ? (shown + ' of ' + total + ' names') : ''; }
}
function initKit(){
  // The bar counts (and keeps a saved sector for) the names it filters: every rendered tier.
  var listed = {}, rows = [];
  for(var t=0;t<KIT_TIERS.length;t++){
    var trs = document.querySelectorAll('#tier-' + KIT_TIERS[t] + ' tbody tr');
    for(var j=0;j<trs.length;j++){ listed[trs[j].getAttribute('data-ticker')] = 1; }
  }
  for(var i=0;i<DATA.length;i++){
    KIT[DATA[i].ticker] = DATA[i];
    if(listed[DATA[i].ticker]){ rows.push(DATA[i]); }
  }
  RCF = RCFilters.mount({root: '#rc-filters', rows: rows, meta: META, onChange: applyKit});
  applyKit(RCF);
}
function togglePin(btn){
  var t = btn.getAttribute('data-ticker');
  fetch('/api/watchlist/toggle/' + encodeURIComponent(t), {method:'POST', headers:{'X-RC-Panel':'leaps'}})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d && d.ok){
        btn.classList.toggle('on', d.in_watchlist);
        btn.textContent = d.in_watchlist ? '★' : '☆';
      }
    })
    .catch(function(){});
}
</script>"""


def _label_cls(label: str) -> str:
    return {"Accumulating": "l-acc", "Distributing": "l-dist",
            "Quiet": "l-quiet", "N/A": "l-na"}.get(label, "l-na")


def _money(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e9:
        return f"{sign}${a / 1e9:.1f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:.0f}K"
    return f"{sign}${a:,.0f}"


def _pct(v) -> str:
    if v is None:
        return "—"
    return f"{v:.0%}"


def _num(v) -> str:
    return "—" if v is None else f"{v:,}"


def _row(snapshot: dict, pinned: bool) -> str:
    t = snapshot.get("ticker", "")
    name = (snapshot.get("name", "") or "").strip()
    label = snapshot.get("label", "N/A")
    footprint = snapshot.get("footprint_score")
    flow = snapshot.get("flow_score")
    net = snapshot.get("net_notional")
    freq = snapshot.get("frequency")
    ask = snapshot.get("ask_share")
    near = snapshot.get("near_term_event")
    net_cls = "pos" if (net or 0) > 0 else ("neg" if (net or 0) < 0 else "zero")
    warn = '<span class="warn" title="earnings within 7 days">⚠</span>' if near else ""
    # Ticker, name and label arrive from UW / meta strings: escape for text and
    # attributes alike (togglePin reads data-ticker, so no value sits inside JS).
    lbl_cls = _label_cls(label)
    data_label = _html.escape(str(label), quote=True)     # the label filter chips read this back (L2)
    t, name, label = (_html.escape(str(v), quote=True) for v in (t, name, label))
    footprint = _html.escape("—" if footprint is None else str(footprint), quote=True)
    flow = _html.escape("—" if flow is None else str(flow), quote=True)
    star = (f'<button class="pin on" data-ticker="{t}" onclick="togglePin(this)" '
            f'title="remove from watchlist">★</button>' if pinned else
            f'<button class="pin" data-ticker="{t}" onclick="togglePin(this)" '
            f'title="add to watchlist">☆</button>')
    nm = f'<div class="nm">{name}</div>' if name else ""
    return (
        f'<tr data-label="{data_label}" data-ticker="{t}">'
        f'<td><div class="tkr">{t}</div>{nm}</td>'
        f'<td class="num"><span class="score">{footprint}</span></td>'
        f'<td class="num"><span class="score">{flow}</span></td>'
        f'<td class="num {net_cls}">{_money(net)}</td>'
        f'<td class="num">{_num(freq)}</td>'
        f'<td class="num">{_pct(ask)}</td>'
        f'<td><span class="lbl {lbl_cls}">{label}</span>{warn}</td>'
        f'<td>{star}</td>'
        f'</tr>'
    )


_COL_HINTS = {
    "Footprint": "Watchlist names only: the far-dated open-interest and IV footprint — far-call OI growth, call share, ask-side sweep share and IV-rank slope, 0–100. — when the name has no stored OI snapshot.",
    "Flow": "min-max(0.4·prints + 0.6·log10(notional+1)) × delta-concentration multiplier, capped 0–100. Relative to today's other names (not a probability). Tiers sort on this for the market scan.",
    "Net Notional": "call premium − put premium across the far-dated large prints (signed $). Green = net buying, red = net selling.",
    "Prints": "count of far-dated (DTE ≥ 183) large (≥ $50k) prints in the 5-day lookback.",
    "Ask %": "premium-weighted share executed on the ask (buyer lifting offers) vs bid (seller hitting bids). High = aggressive buying.",
}


_COLGROUP = ('<colgroup>'
             '<col style="width:22%">'   # Ticker (+ name)
             '<col style="width:9%">'    # Footprint
             '<col style="width:8%">'    # Flow
             '<col style="width:13%">'   # Net Notional
             '<col style="width:8%">'    # Prints
             '<col style="width:8%">'    # Ask %
             '<col style="width:16%">'   # Signal
             '<col style="width:7%">'    # pin
             '</colgroup>')


def _tier(tier_id: str, title: str, sub: str, rows: list[dict], pinned: set, open_: bool = True) -> str:
    cls = " collapsed" if not open_ else ""
    display = "table" if open_ else "none"
    head = (
        '<tr><th>Ticker</th>'
        f'<th class="num" title="{_COL_HINTS["Footprint"]}">Footprint</th>'
        f'<th class="num" title="{_COL_HINTS["Flow"]}">Flow</th>'
        f'<th class="num" title="{_COL_HINTS["Net Notional"]}">Net Notional</th>'
        f'<th class="num" title="{_COL_HINTS["Prints"]}">Prints</th>'
        f'<th class="num" title="{_COL_HINTS["Ask %"]}">Ask %</th>'
        '<th>Signal</th><th></th></tr>'
    )
    body = "".join(_row(s, s["ticker"] in pinned) for s in rows)
    return (
        f'<div class="tier" id="tier-{tier_id}">'
        f'<div class="tier-head{cls}" onclick="toggleTier(this)">'
        f'<span class="caret">▼</span><span class="t">{title}</span>'
        f'<span class="sub">{sub}</span><span class="n" data-total="{len(rows)}">{len(rows)}</span></div>'
        f'<table style="display:{display}">{_COLGROUP}<thead>{head}</thead><tbody>{body}</tbody></table>'
        f'</div>'
    )


def _kit_rows(snaps: list[dict]) -> tuple[list[dict], str | None]:
    """The filter-kit rows (K1 contract) for every ticker on the board, and a notice when the read failed.

    A failed ``filter_fields`` read never fails a render: the rows go out with ticker and name only (KL default 6).
    """
    names = {}
    for s in snaps:
        t = str(s.get("ticker") or "")
        if t:
            names[t] = (s.get("name") or "").strip() or t
    try:
        fields = universe.filter_fields(list(names))
        rows = [fields.get(t) or {"ticker": t, "name": n} for t, n in names.items()]
        return rows, None
    except Exception:  # noqa: BLE001 - the board must render without the store
        return ([{"ticker": t, "name": n} for t, n in names.items()],
                "The filter fields (index, market cap, sector, theme) could not be read, so the filters "
                "have nothing to match on. The scan itself is unaffected.")


def _kit_themes() -> dict:
    try:
        return universe.theme_catalog()
    except Exception:  # noqa: BLE001 - an empty catalog just leaves the Theme facet empty
        return {}


def render_leaps_dashboard(watched: list[dict], unwatched: list[dict],
                           filename: str = None, watchlist: set = None,
                           provider_health: dict | None = None, notice: str | None = None) -> Path:
    # regenerable artifact -> .cache/ (OUTPUT_DIR); .store/ holds raw.duckdb only
    path = OUTPUT_DIR / (filename or "leaps_latest.html")

    wl = watchlist or {s.get("ticker") for s in watched}
    watched = sorted(watched, key=lambda s: -(s.get("score", 0) or 0))
    unwatched = sorted(unwatched, key=lambda s: -(s.get("score", 0) or 0))

    high = [s for s in unwatched if (s.get("score", 0) or 0) >= 50]
    emerging = [s for s in unwatched if 20 <= (s.get("score", 0) or 0) < 50]
    quiet = [s for s in unwatched if (s.get("score", 0) or 0) < 20]

    body = f'<div class="header"><div><h1>LEAPS ACCUMULATION RADAR</h1>'
    body += f'<div class="meta">generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} | far-dated prints (DTE ≥ 183, prem ≥ $50k) · tiered ITM 0.55–0.85 / OTM 0.15–0.40 delta · context only</div>'
    body += ('<div class="meta">Refresh daily after market close — OI &amp; IV are end-of-day snapshots, '
             'so the far-dated accumulation footprint builds over days/quarters, not minutes. '
             '<b>Exploratory</b>: the label is classified from the recorded daily sessions, and its quarter '
             'growth stays blank until about a quarter of them is stored.</div></div>')
    body += ('<div class="controls"><label><input type="checkbox" id="quiet-toggle" '
             'onchange="toggleQuiet(this)"> Hide quiet (&lt;20)</label>')
    # Board-local label filter chips (#377 L2): the watchlist now lists every pinned name, so the
    # labels are filtered here, on the board, instead of being dropped in the scan. All on by default.
    body += '<span class="chip-label">Watchlist labels:</span>'
    for lbl in LABELS:
        esc = _html.escape(lbl, quote=True)
        body += (f'<label><input type="checkbox" class="label-chip" data-label="{esc}" '
                 f'checked onchange="toggleLabel(this)"> {esc}</label>')
    body += '</div></div>'
    body += (f'<div class="note">The market scan lists at most {UNWATCHED_CAP} unwatched names (the highest '
             'flow scores); pinned watchlist names are always listed. The filter bar hides rows in every tier, the '
             'watchlist too, while a filter is on.</div>')
    kit_rows, kit_notice = _kit_rows(list(watched) + list(unwatched))
    body += provider_health_html(provider_health)
    body += notice_html(notice)
    body += notice_html(kit_notice)
    body += filters_bar_html("rc-filters")
    body += '<div class="kit-count" id="kit-count" aria-live="polite"></div>'
    body += help_html(
        "Far-dated option flow: large prints (premium $50k or more) on contracts at least 183 days out, "
        "a sign of someone building a long-term position. Your <b>watchlist</b> names are pinned on top; "
        "the rest of the market scan is split by <b>Flow</b> score into High, Emerging and Quiet. "
        "<b>How to read</b> at the bottom explains each column. The footprint label is <b>exploratory</b>: "
        "it is classified from the recorded daily sessions, so read it as context and expect the quarter growth "
        "to stay blank until about a quarter of sessions is stored. <b>Fetch</b> scans the last five days of "
        "flow on <b>Unusual Whales</b> and stores the prints and watchlist footprints. <b>Refresh</b> "
        "rebuilds the board from the stored prints and footprints with no network call.")
    body += recorded_sessions_html("module_leaps")

    if not watched and not unwatched:
        body += ('<div class="empty-state"><b>No LEAPS data yet.</b><br>No far-dated flow prints '
                 'in the last 5 days and no watchlist footprint stored. Press <b>Fetch</b> to scan '
                 'the market tape (needs the Unusual Whales key).</div>')
    else:
        body += _tier("watchlist", "📌 Watchlist", "pinned names", watched, wl, open_=True)
        body += _tier("high", *TIER_META["high"], high, wl, open_=True)
        body += _tier("emerging", *TIER_META["emerging"], emerging, wl, open_=True)
        body += _tier("quiet", *TIER_META["quiet"], quiet, wl, open_=True)

    body += ('<div class="legend"><h3>How to read</h3>'
             '<div class="lg-row"><b>Footprint</b> — watchlist names only: a 0–100 score of the far-dated '
             'open-interest and IV footprint (far-call OI growth, call share, ask-side sweep share, '
             'IV-rank slope). — when no OI snapshot is stored.</div>'
             '<div class="lg-row"><b>Exploratory signal</b> — the Accumulating / Distributing / Quiet label is '
             'classified from the recorded daily sessions: the quarter baseline is the last session at or before '
             'quarter end, and growth counts only the expiries present in both sessions, so a new listing or an '
             'expiry rolling past 270 days is not growth. An all-<b>bid</b> flow (under 50% of the far-dated print '
             'premium on the <b>ask</b>) is not Accumulating. Read it as context; a Quiet name may simply have '
             'less than a quarter of history.</div>'
             '<div class="lg-row"><b>Flow</b> — a 0–100 relative ranking of far-dated accumulation '
             'vs today\'s other names (not a probability). min-max(0.4·prints + 0.6·log10(notional+1)) '
             '× a delta-concentration multiplier: +20% when ≥50% of far-dated premium is ITM (Δ 0.55–0.85) '
             'calls, +10% when ≥30% is OTM (Δ 0.15–0.40) calls. Capped at 100. The market-scan tiers '
             'sort on it.</div>'
             '<div class="lg-row"><b>Net Notional</b> — call premium − put premium across the far-dated '
             'large prints (signed $). Green = net buying pressure; red = net selling.</div>'
             '<div class="lg-row"><b>Prints</b> — count of far-dated (DTE ≥ 183) large (≥ $50k) prints '
             'over the 5-day lookback (persistence / frequency).</div>'
             '<div class="lg-row"><b>Ask %</b> — premium-weighted share of that flow executed on the ask '
             '(buyer lifting offers) vs the bid (seller hitting bids). 100% = all aggressive buying.</div>'
             '<div class="lg-row"><b>⚠</b> = earnings within 7 days (tagged, not dropped) · '
             '<b>★</b> = pin/unpin from the watchlist.</div></div>')
    body += '<footer>RevenueChaser LEAPS radar — Unusual Whales API — far-dated flow prints + delta</footer>'

    kit_meta = filters_meta("leaps", facets=["index", "mc", "gics", "theme"], themes=_kit_themes())
    kit_js = (f"<script>var DATA = {script_json(kit_rows)};\nvar META = {script_json(kit_meta)};\n"
              f"{FILTERS_JS}\ndocument.addEventListener('DOMContentLoaded', initKit);</script>")
    html = (f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>LEAPS Accumulation Radar</title>'
            f'{CSS}{FILTERS_CSS}</head><body>{body}{JS}{kit_js}</body></html>')
    path.parent.mkdir(parents=True, exist_ok=True)
    write_artifact(path, html)
    return path
