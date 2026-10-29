"""Fundamental Analysis — dark-theme HTML renderer.

Mirrors the Swing/Hunter layout: topbar, narrative summary, banner, universal
filters (Market Cap / Earnings / sector / theme / search) plus gate tabs
(Triple / Dual+ / All), a card grid, and auditable tooltip modals.

The rows live outside the HTML (K10, #383 R3 slice 0): ``render_fundamental_board`` returns the page and its
payload, the page fetches ``/view/module_fundamentals/payload.json``; ``render_fundamental_dashboard`` is the
one-file page with the rows inlined (tests, the hostile-fixture guard).
"""
from __future__ import annotations

import html as _html

from RevenueChaser.core import universe as _universe
from RevenueChaser.core.gics_map import sector_path
from RevenueChaser.core.html_safe import ESC_JS, esc, fill, script_json
from RevenueChaser.core.reporting_common import (
    FILTERS_CSS,
    FILTERS_JS,
    PAYLOAD_JS,
    filters_bar_html,
    filters_meta,
    help_html,
    provider_health_html,
    recorded_sessions_html,
    universe_provenance_html,
    watchlist_html,
)
from RevenueChaser.core.scan_quality import no_data_hint, regime_parts
from RevenueChaser.module_fundamentals.pipeline import NON_USD_DROP

CSS = """
:root{--bg:#0a0d12;--panel:#12161e;--panel2:#1a1f2b;--border:#232a38;
--text:#d7dce5;--muted:#8a93a6;--accent:#4f9dff;--green:#2ecc71;
--red:#e74c3c;--amber:#f1c40f;--neon:#00e6a8;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5}
.wrap{max-width:100%;margin:0 auto;padding:20px 24px 60px}
.topbar{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;border-bottom:1px solid var(--border);padding-bottom:12px;margin-bottom:14px}
.topbar h1{margin:0;font-size:22px;letter-spacing:-.4px}
.topbar .sub{color:var(--muted);margin-top:2px;font-size:13px}
.summary{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:10px 14px;margin-bottom:18px;color:var(--muted);font-size:12.5px;line-height:1.7}
.summary b{color:var(--text);font-weight:600}
.summary .on{color:var(--green);font-weight:700}
.summary .off{color:var(--red);font-weight:700}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:700}
.b-sector{background:var(--panel2);color:var(--muted)}
.b-index{background:rgba(79,157,255,.14);color:var(--accent)}
.b-dist{background:rgba(231,76,60,.15);color:var(--red)}
.banner{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:18px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.stat .v{font-size:22px;font-weight:800}
.stat .l{color:var(--text);font-size:12px;font-weight:600;margin-top:2px}
.stat .d{color:var(--muted);font-size:11px}
.controls{display:flex;flex-direction:column;gap:8px;margin-bottom:16px}
.frow{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.flbl{color:var(--muted);font-size:12px;font-weight:600}
.toggles{display:flex;flex-wrap:wrap;gap:14px;align-items:center}
.tog{display:flex;align-items:center;gap:6px;color:var(--text);font-size:12.5px;cursor:pointer}
.tog input{accent-color:var(--neon);width:15px;height:15px}
.notice{background:var(--panel);border:1px solid var(--border);border-left:3px solid var(--amber);border-radius:8px;padding:8px 12px;color:var(--muted);font-size:12.5px}
.tabs{display:inline-flex;background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden;flex-wrap:wrap}
.tab{padding:0 14px;height:34px;line-height:34px;cursor:pointer;font-weight:600;font-size:13px;color:var(--muted);border-right:1px solid var(--border);user-select:none}
.tab:last-child{border-right:none}
.tab.on{background:var(--panel2);color:var(--text)}
.tab .cnt{color:var(--muted);font-size:11px;font-weight:400}
.tab.on .cnt{color:var(--neon)}
select,input[type=text],input[type=number]{background:#10151f;border:1px solid #2a3345;color:var(--text);border-radius:6px;height:34px;padding:0 10px;font:500 13px/1 inherit}
input[type=text]{flex:1;min-width:200px}
.count{color:var(--muted);font-size:12px;margin-left:auto;white-space:nowrap}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:9px}
.head{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.tick{font-size:14px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tick b{font-size:15px;letter-spacing:-.2px}
.tick .sep{color:var(--muted)}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:5px}
.m{background:var(--panel2);border-radius:8px;padding:6px 4px;text-align:center;min-width:0}
.m .k{font-size:9px;color:var(--muted);letter-spacing:.3px;text-transform:uppercase}
.m .val{font-weight:700;font-size:12px;overflow-wrap:break-word;line-height:1.15}
.gates{display:flex;gap:6px;border-top:1px solid var(--panel2);padding-top:8px}
.event{color:var(--amber);font-size:11px;line-height:1.35}
.event .when{color:var(--muted)}
.m .when{display:block;font-weight:400;font-size:9px;color:var(--muted)}
.gate{flex:1;background:var(--panel2);color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:5px 0;cursor:pointer;font-weight:700;font-size:12px}
.gate.on{color:var(--green);border-color:rgba(46,204,113,.4)}
.gate.off{color:var(--red)}
.gate.unk{color:var(--amber)}
.tags{display:flex;flex-wrap:wrap;gap:6px;min-height:18px}
.tags.empty{min-height:18px}
.tag{font-size:10.5px;padding:2px 8px;border-radius:20px;background:var(--panel2);color:var(--muted)}
.empty{color:var(--muted);padding:40px;text-align:center}
footer{color:var(--muted);font-size:11px;margin-top:28px;border-top:1px solid var(--border);padding-top:14px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}
.modal{display:none;position:fixed;inset:0;background:rgba(5,17,31,.7);z-index:50;align-items:center;justify-content:center}
.modal.open{display:flex}
.modal .box{background:var(--panel);border:1px solid var(--border);border-radius:12px;width:min(560px,92vw);max-height:80vh;overflow:auto;padding:18px 20px}
.modal h3{margin:0 0 4px;font-size:16px}
.modal .sub{color:var(--muted);font-size:12px;margin-bottom:12px}
.rule{display:flex;align-items:center;gap:10px;padding:7px 0;border-top:1px solid var(--panel2);font-size:13px}
.rule:first-of-type{border-top:none}
.mark{width:18px;text-align:center;font-weight:700}
.m-ok{color:var(--green)} .m-no{color:var(--red)} .m-unk{color:var(--muted)}
.rule .lbl{flex:1;color:var(--text)}
.rule .th{color:var(--muted);font-size:11px}
.rule .act{color:var(--text);font-variant-numeric:tabular-nums;text-align:right}
.close{margin-top:12px;width:100%}
"""


def _fmt_int(v) -> str:
    return "0" if v is None else f"{int(v):,}"


def _theme_catalog() -> dict:
    """The pinned theme run's catalog for the Theme facet (store read; empty on a fresh store)."""
    try:
        return _universe.theme_catalog()
    except Exception:  # noqa: BLE001 - a missing or unreadable catalog never fails a render
        return {}


def _summary_html(s: dict, data_quality: dict | None = None) -> str:
    regime_cls, regime, momentum = regime_parts(s.get("regime"))
    spy_price = s.get("spy_price")
    spy_sma200 = s.get("spy_sma200")
    spy_3m = s.get("spy_3m_return")
    filtered = s.get("filtered_universe")
    passing = s.get("passing") or {}
    top_n = s.get("top_n")
    distorted = s.get("distorted_count")

    spy_part = ""
    if spy_price is not None and spy_sma200 is not None:
        spy_part = f" (SPY {spy_price:,.0f} {'&gt;' if spy_price > spy_sma200 else '&lt;'} 200DMA {spy_sma200:,.0f})"

    spy_3m_part = ""
    if spy_3m is not None:
        sign = "+" if spy_3m >= 0 else ""
        spy_3m_part = f" SPY 3mo <b>{sign}{spy_3m * 100:.1f}%</b>."

    elim = []
    if s.get("out_of_universe") is not None:
        elim.append(f"out-of-universe {_fmt_int(s.get('out_of_universe'))}")
    if s.get("data_short") is not None:
        elim.append(f"data&lt;252d {_fmt_int(s.get('data_short'))}")
    if s.get("no_edgar"):
        elim.append(f"no EDGAR {_fmt_int(s.get('no_edgar'))}")
    # The F1 data-health drops, each named (#386 review): never folded into "data<252d".
    health = s.get("health_drops") or {}
    for key, text in (("sector_excluded", "Financial Services / Real Estate"), ("edgar_fetch_failed", "EDGAR read failed"),
                      ("stale_annual", "stale annual report"), ("ro40_barrier", "Rule of 40 above 150")):
        if health.get(key):
            elim.append(f"{text} {_fmt_int(health.get(key))}")
    if s.get("hygiene") is not None:
        elim.append(f"hygiene {_fmt_int(s.get('hygiene'))}")
    elim_part = f" Eliminated: {' · '.join(elim)}." if elim else ""
    stale_n = s.get("stale_ohlc") or 0
    if stale_n or s.get("spy_stale"):
        bits = ([f"{_fmt_int(stale_n)} tickers"] if stale_n else []) + (["SPY"] if s.get("spy_stale") else [])
        elim_part += (f" Stale price cache (refetch failed, last cached bars used): "
                      f"{_html.escape(' + '.join(bits))}.")
    # The F5 blind spots (#58): a card with no amber event line is either clean or one the store cannot answer for.
    # The counters cover the shown cards (counted after top_n); a payload from before they existed has neither key.
    dq = data_quality or {}
    n_filings, n_cik = int(dq.get("no_submissions") or 0), int(dq.get("no_cik") or 0)
    n_stale = int(dq.get("stale_submissions") or 0)          # review F2: rows the daily sync stopped refreshing
    if n_filings or n_cik or n_stale:
        from . import config as _config
        shown = f" of the {_fmt_int(top_n)} shown" if top_n else ""
        parts = ([f"{_fmt_int(n_filings)} with no filings on record"] if n_filings else []) +                 ([f"{_fmt_int(n_cik)} with no CIK"] if n_cik else []) +                 ([f"{_fmt_int(n_stale)} with an 8-K list not checked in the last "
                  f"{_config.CORPORATE_EVENT_CHECK_MAX_AGE_DAYS} days"] if n_stale else [])
        elim_part += (f" No corporate-event check{shown}: {' · '.join(parts)}"
                      " (a card without an amber event line is not proven event-free for these).")
    # #46: what the extraction could not read. The measure is the unit on the money facts, never the accounting
    # standard — a non-USD filer is usually an IFRS filer, but an IFRS filer may still tag USD — so the line says
    # exactly what was counted. A MEASURED 0 is a result and is shown as 0; a 0 with no stored row carrying the
    # measure is not a result, because a stored row is never re-extracted, so the line says that instead (#102
    # review): the count fills once the #52 re-extraction has run.
    n_non_usd = int((dq.get("dropped") or {}).get(NON_USD_DROP) or 0)
    units = ", ".join(str(u) for u in (dq.get("non_usd_units") or []))
    measured = int(dq.get("non_usd_measured") or 0)
    if n_non_usd or measured:
        noun = "name reports" if n_non_usd == 1 else "names report"
        non_usd_part = (f" {_fmt_int(n_non_usd)} {noun} in a currency other than USD and could not be scored"
                        + (esc(f" ({units})") if units else "") + ".")
    else:
        non_usd_part = (" Reporting currency not measured yet: no re-extraction has run since the measure"
                        " landed, so no filer's currency has been read (#52).")
    dist_part = f" [DISTORTED] {_fmt_int(distorted)}." if distorted else ""

    return (
        '<div class="summary">'
        f'<span class="{regime_cls}">Regime {_html.escape(regime)}</span>{spy_part} — {momentum}. '
        f'{_fmt_int(filtered)} stocks in the filtered universe; '
        f'{_fmt_int(passing.get("rule_of_40"))} pass the Rule of 40. '
        'Layer 1: three quality-growth methods (Rule of 40 / Quality / Momentum) filter. '
        'Layer 2: survivors ranked by rules passed, then Rule of 40; DISTORTED last at an equal count. '
        f'Strongest <b>{_fmt_int(top_n)}</b> names.{spy_3m_part}{elim_part}{non_usd_part}{dist_part}'
        '</div>'
    )


MODULE = "module_fundamentals"


def render_fundamental_dashboard(payload: dict) -> str:
    """The one-file page: the board with its rows inlined, so it boots with no fetch (tests, the guard)."""
    return _render(payload, inline=True)[0]


def render_fundamental_board(payload: dict) -> tuple[str, dict | None]:
    """The page and its K10 payload (``{"rows": [...]}``), or ``None`` when there is no row to fetch.

    The HTML holds no row: it fetches the payload from ``/view/module_fundamentals/payload.json``. A board with
    no rows keeps its server-rendered empty / no-data message (DF111) and needs no payload file.
    """
    return _render(payload, inline=False)


def _render(payload: dict, inline: bool) -> tuple[str, dict | None]:
    payload = payload or {}
    candidates = payload.get("candidates") or []
    passing = payload.get("passing") or {}
    distorted_count = payload.get("distorted_count") or 0
    # #386 F2/F3: rows stored before v6 (a Refresh of an old Fetch) carry no rule toggles and no new keys.
    from . import config as _config
    stale_payload = payload.get("selection_version") != _config.SELECTION_VERSION

    summary = payload.get("summary") or {}
    if not summary:
        summary = {
            "regime": payload.get("regime"),
            "filtered_universe": payload.get("universe_total"),
            "passing": passing,
            "distorted_count": distorted_count,
            "top_n": len(candidates),
        }
    summary_html = _summary_html(summary, payload.get("data_quality"))
    universe_prov = universe_provenance_html(
        payload.get("universe_generated_at"), payload.get("universe_refreshed_by"),
        partial=bool(payload.get("universe_partial")))

    # The rows the shared filter bar reads (#383 C1c): a copy of each candidate plus the store's
    # ``gics`` + ``theme_ids``. A store read only, so a Refresh stays network-free; the stored payload is never mutated.
    rows = [dict(c) for c in candidates]
    fields = _universe.filter_fields([c.get("ticker") for c in rows])
    for c in rows:
        f = fields.get(c.get("ticker"))
        if f:
            c["gics"] = f["gics"]
            c["theme_ids"] = f["theme_ids"]
        else:
            c["gics"] = sector_path(c.get("sector") or "Unclassified", c.get("industry") or "")
            c["theme_ids"] = []
    meta = script_json(filters_meta("fundamentals", earnings_mode="filter",
                                    themes=_theme_catalog()))


    # The grid starts with a placeholder the client replaces; only the empty / no-data message is server-side (DF111).
    if payload.get("no_data"):
        hint = no_data_hint(str(payload.get("no_data_reason") or ""))
        cards = ('<div class="empty"><b>' + _html.escape(str(payload.get("no_data_message") or "No data."))
                 + '</b>' + (" " + _html.escape(hint) if hint else "") + '</div>')
    elif not rows:
        cards = '<div class="empty">No names matched the rules.</div>'
    else:
        cards = '<div class="empty">Loading the scan…</div>'

    data = {"rows": rows} if rows else None
    # Inline: the rows ride in the page. No rows: an empty boot, so the page keeps its message and fetches nothing.
    boot = (data or {"rows": []}) if (inline or data is None) else None     # None: the page fetches the payload

    JS = ESC_JS + PAYLOAD_JS + r"""
var MOD = __MOD__;
var BOOT = __BOOT__;
var DATA = [];
var GATES = {};
var PERIODS = {};
var ANCHORS = {};
var META = __META__;
var RULES = ["rule_of_40","quality","momentum"];
var RULE_ON = __RULE_ON__;
var RCF = null;
function pass(c){ return RCF ? RCF.pass(c) : true; }
function ruleState(c, k){
  var g = (c.gates || {})[k];
  if(!g) return "unknown";
  if(k === "rule_of_40"){
    var r = (g.rules || [])[0];
    var p = (r === undefined || r === null) ? null : r.passed;     // true / false / null (#386 F2)
    return p === true ? "pass" : (p === false ? "fail" : "unknown");
  }
  if(g.passed) return "pass";
  var rs = g.rules || [];
  for(var i = 0; i < rs.length; i++){
    if(rs[i].passed === null || rs[i].passed === undefined) return "unknown";   // an input is missing (#151)
  }
  return "fail";
}
function rulePasses(c){
  for(var i = 0; i < RULES.length; i++){
    var k = RULES[i];
    // Card 3 (avo): a card shows when it PASSES every ticked rule; an unknown is not a pass (#386 F2).
    if(RULE_ON[k] && ruleState(c, k) !== "pass") return false;
  }
  return true;
}
function setRule(k, on){ RULE_ON[k] = !!on; render(); }
function usd(v){
  if(v === null || v === undefined) return "N/A";
  var a = Math.abs(v), s = v < 0 ? "-" : "";
  if(a >= 1e9) return s + "$" + (a/1e9).toFixed(2) + "B";
  if(a >= 1e6) return s + "$" + (a/1e6).toFixed(1) + "M";
  if(a >= 1e3) return s + "$" + (a/1e3).toFixed(1) + "K";
  return s + "$" + Math.round(a);
}
function fmtNum(v, d){ return (v == null) ? "N/A" : v.toFixed(d == null ? 2 : d); }
function fmtPct(v){ return (v == null) ? "N/A" : (v*100).toFixed(1) + "%"; }
function cell(k, v){ return '<div class="m"><div class="k">'+k+'</div><div class="val">'+v+'</div></div>'; }
function cardHtml(c){
  var t = rcEsc(c.ticker);
  var name = rcEsc(c.name);
  var sector = rcEsc(c.sector);
  var distTag = c.distorted ? '<span class="badge b-dist">DISTORTED</span>' : "";
  var staleTag = c.info_stale ? '<span class="badge b-dist">STALE INFO</span>' : "";
  var gates = RULES.map(function(k){
    var g = (c.gates || {})[k] || {};
    var lbl = {rule_of_40:"Ro40", quality:"Q", momentum:"M"}[k];
    var st = ruleState(c, k);
    var cls = st === "pass" ? "on" : (st === "fail" ? "off" : "unk");
    return '<button class="gate ' + cls + '" data-ticker="' + t + '" data-gate="' + k + '" onclick="openGate(this)">' + lbl + ' ' + rcEsc(g.score) + '</button>';
  }).join("");
  var themes = c.themes || [];
  var tags = themes.slice(0,3).map(function(x){ return '<span class="tag">'+rcEsc(x)+'</span>'; }).join("");
  var tagsRow = tags ? '<div class="tags">'+tags+'</div>' : '<div class="tags empty"></div>';
  var indexBadges = (c.index || []).map(function(i){ return '<span class="badge b-index">'+rcEsc(i)+'</span>'; }).join("");
  var r40 = (c.ro40 == null) ? "N/A"
          : fmtNum(c.ro40, 1) + " (" + fmtPct(c.rev_growth_ttm) + " / " + fmtPct(c.fcf_margin) + ")";
  // #386 F5: the corporate-event flag, a display-only note under the metrics (the item codes of the filer's 8-Ks).
  var ev = c.corporate_event || null;
  var evItems = (ev && ev.items) || [];
  var evLine = evItems.length
    ? '<div class="event" title="Corporate event: an 8-K item that breaks the comparability of the fundamentals">'
      + '8-K items ' + evItems.map(function(i){ return rcEsc(i); }).join(" · ")
      + (ev.last_filing_date ? ' <span class="when">' + rcEsc(ev.last_filing_date) + '</span>' : '')
      + '</div>'
    : "";
  return '<div class="card">'
    + '<div class="head">'
    + (sector ? '<span class="badge b-sector">'+sector+'</span>' : '')
    + indexBadges + distTag + staleTag + '</div>'
    + '<div class="tick"><b>' + t + '</b>'
    + (name ? '<span class="sep"> • </span>'+name : '') + '</div>'
    + rcWatchRow(c.ticker)
    + '<div class="metrics">'
    + cell("Period", rcEsc(c.period))
    + cell("Basis", rcEsc(c.ro40_basis))
    + cell("Rule of 40", r40)
    // Review F1: ROIC, gross margin and net debt are one fiscal year's (the anchor), the growth half is TTM.
    + cell("Quality", rcEsc(c.quality_score)
           + (c.anchor_period_end ? '<span class="when">FY to ' + rcEsc(c.anchor_period_end) + '</span>' : ''))
    + cell("Momentum", rcEsc(c.momentum_score))
    + cell("Price", usd(c.price))
    + cell("Mkt cap", usd(c.market_cap))
    + '</div>'
    + evLine
    + '<div class="gates">' + gates + '</div>'
    + tagsRow
    + '</div>';
}
function fmtVal(unit, v){
  if(v === null || v === undefined) return "N/A";
  if(unit === "pct") return (v*100).toFixed(1) + "%";
  if(unit === "pct_high") return (v*100).toFixed(1) + "% of high";
  if(unit === "ratio") return Number(v).toFixed(2) + "×";
  if(unit === "pp") return (v >= 0 ? "+" : "") + (v*100).toFixed(1) + " pp";
  if(unit === "pts") return Number(v).toFixed(1);
  if(unit === "bool") return v ? "Yes" : "No";
  return String(v);
}
function openGate(btn){
  var t = btn.getAttribute("data-ticker");
  var g = btn.getAttribute("data-gate");
  var gate = (GATES[t]||{})[g];
  if(!gate) return;
  var period = PERIODS[t] || "";
  // Review F1: Quality mixes two time bases; its window names both.
  if(g === "quality" && ANCHORS[t])
    period = "growth: TTM " + period + " · ROIC, gross margin, net debt: fiscal year to " + ANCHORS[t];
  document.getElementById("m-title").textContent = gate.name;
  document.getElementById("m-sub").textContent = t + " — " + gate.score + (gate.passed ? " (PASS)" : " (FAIL)") + (period ? " · " + period : "");
  var rules = gate.rules.map(function(r){
    var ok = r.passed;   // true / false / null (unknown: an input is missing)
    var cls = ok === null ? 'm-unk' : (ok ? 'm-ok' : 'm-no');
    var mark = ok === null ? '?' : (ok ? '&#10003;' : '&#10007;');
    return '<div class="rule"><span class="mark '+cls+'">'+mark+'</span>'
      + '<span class="lbl">'+rcEsc(r.label)+'</span>'
      + '<span class="th">'+rcEsc(r.threshold)+'</span>'
      + '<span class="act">'+rcEsc(fmtVal(r.unit, r.actual))+'</span></div>';
  }).join("");
  document.getElementById("m-rules").innerHTML = rules;
  document.getElementById("modal").classList.add("open");
}
function closeModal(){ document.getElementById("modal").classList.remove("open"); }
function render(){
  if(!DATA.length){ document.getElementById("count").textContent = "0 names shown"; return; }   // keep the server-side empty / no-data message (DF111)
  var q = document.getElementById("q").value.trim().toLowerCase();
  var out = DATA.filter(function(c){
    if(!rulePasses(c)) return false;
    if(!pass(c)) return false;
    if(q && (c.ticker.toLowerCase().indexOf(q) < 0 && (c.name||"").toLowerCase().indexOf(q) < 0)) return false;
    return true;
  });
  document.getElementById("count").textContent = out.length + " names shown";
  document.getElementById("grid").innerHTML = out.length
    ? out.map(cardHtml).join("")
    : '<div class="empty">No names match the current filters.</div>';
}
function init(){
  RCF = RCFilters.mount({root: "#rc-filters", rows: DATA, meta: META, onChange: function(){ render(); }});
  render();
}
function boot(d){
  DATA = (d && d.rows) || [];
  GATES = {};
  PERIODS = {};
  ANCHORS = {};
  DATA.forEach(function(c){
    GATES[c.ticker] = c.gates || {}; PERIODS[c.ticker] = c.period || ""; ANCHORS[c.ticker] = c.anchor_period_end || "";
  });
  init();
}
function start(){
  if(BOOT){ boot(BOOT); return; }
  document.getElementById("count").textContent = "Loading…";
  rcPayload(MOD).then(boot, function(e){
    document.getElementById("count").textContent = "The scan did not load (" + e.message + "). Press Refresh.";
  });
}
"""

    JS = fill(JS, {"__MOD__": script_json(MODULE),
                   "__BOOT__": script_json(boot),
                   "__RULE_ON__": script_json({} if stale_payload else
                                              {"rule_of_40": True, "quality": False, "momentum": False}),
                   "__META__": meta})
    # #386 F2.8/F2.9: the rule toggles a card must pass (Rule of 40 on by default); a payload scored before v6
    # renders no toggles and one notice instead.
    if stale_payload:
        rules_row = ('<div class="notice">Scored before v6 (data-health gates and the rule toggles). '
                     'Run a Fetch to rescore.</div>')
    else:
        rules_row = (
            '<div class="frow"><span class="flbl">Rules</span><div class="toggles">'
            '<label class="tog"><input type="checkbox" id="rule-rule_of_40" checked '
            'onchange="setRule(\'rule_of_40\', this.checked)"> Rule of 40</label>'
            '<label class="tog"><input type="checkbox" id="rule-quality" '
            'onchange="setRule(\'quality\', this.checked)"> Quality</label>'
            '<label class="tog"><input type="checkbox" id="rule-momentum" '
            'onchange="setRule(\'momentum\', this.checked)"> Momentum</label>'
            '</div></div>')

    help_block = help_html(
        "A quality-growth scan of US-listed stocks through three methods: the <b>Rule of 40</b> "
        "(revenue growth plus margin), <b>Quality</b> and <b>Momentum</b>. Tick the rules a card must pass; "
        "<b>DISTORTED</b> rows have figures that need a manual look. "
        "Statements come from <b>SEC EDGAR</b>, prices from <b>Yahoo</b>. <b>Fetch</b> runs the scan "
        "and stores the scores. <b>Refresh</b> shows the last Fetch's stored result with no network "
        "call and no re-scoring.")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Fundamental Analysis</title>
<style>{CSS}</style>
{FILTERS_CSS}
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>Fundamental Analysis</h1>
      <div class="sub">Rule of 40 · Quality · Momentum — quality-growth scan</div>
      {universe_prov}
    </div>
  </div>

  {provider_health_html(payload.get("provider_health"))}
  {help_block}
  {recorded_sessions_html("module_fundamentals")}

  {summary_html}

  <div class="banner">
    <div class="stat"><div class="v">{payload.get("universe_total") or 0}</div><div class="l">Scanned Universe</div><div class="d">US-listed · all caps</div></div>
    <div class="stat"><div class="v">{distorted_count or 0}</div><div class="l">DISTORTED</div><div class="d">manual review</div></div>
  </div>

  <div class="controls">
    {rules_row}
    <div class="frow">
      <input type="text" id="q" placeholder="Search ticker / company" oninput="render()" />
      <span class="count" id="count"></span>
    </div>
  </div>

  {filters_bar_html("rc-filters")}

  <div class="grid" id="grid">{cards}</div>

  <footer>
    <span>Fundamentals: SEC EDGAR (10-K + 10-Q) · Price: Yahoo Finance · Unusual Whales flow manual-only (K3) · No SA confirmation. For information only, not financial advice.</span>
    <span id="flow-flag">Flow not scanned</span>
  </footer>
</div>
<div class="modal" id="modal">
  <div class="box">
    <h3 id="m-title"></h3>
    <div class="sub" id="m-sub"></div>
    <div id="m-rules"></div>
    <button class="close" onclick="closeModal()">Close</button>
  </div>
</div>
<script>{FILTERS_JS}</script>
{watchlist_html("fundamentals")}
<script>
{JS}
document.getElementById("modal").addEventListener("click", function(e){{ if(e.target === this) closeModal(); }});
start();
</script>
</body>
</html>""", (None if inline else data)
