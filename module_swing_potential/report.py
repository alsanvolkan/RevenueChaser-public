"""Swing Screener — dark-theme HTML renderer.

Mirrors the Asymmetric Growth Hunter layout: topbar, a banner of stat cards, a
narrative summary line, market-cap/sector/theme/multiplier filters, a card grid,
and an auditable gate modal. No client-side technical recomputation.

The rows live outside the HTML (K10, #383 R3 slice 0): ``render_swing_board`` returns the page and its payload,
the page fetches ``/view/module_swing_potential/payload.json``; ``render_swing_dashboard`` is the one-file page
with the rows inlined (tests, the hostile-fixture guard).
"""
from __future__ import annotations

import html as _html

from RevenueChaser.core import funnel as funnel_mod
from RevenueChaser.core import universe as _universe
from RevenueChaser.core.gics_map import sector_path
from RevenueChaser.core.html_safe import ESC_JS, fill, script_json
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
from RevenueChaser.module_swing_potential import config
from RevenueChaser.module_swing_potential import settings as swing_settings
from RevenueChaser.module_swing_potential.screeners import (
    evaluate_direction,
    evaluate_minervini,
    evaluate_qullamaggie,
    evaluate_weinstein,
)

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
.banner{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:18px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.stat .v{font-size:22px;font-weight:800}
.stat .l{color:var(--text);font-size:12px;font-weight:600;margin-top:2px}
.stat .d{color:var(--muted);font-size:11px}
details.rules{background:var(--panel);border:1px solid var(--border);border-radius:10px;margin-bottom:16px}
details.rules summary{cursor:pointer;padding:12px 16px;font-weight:700;color:var(--accent)}
.rules-body{padding:0 16px 16px;color:var(--muted)}
.rules-body table{width:100%;border-collapse:collapse;font-size:12.5px}
.rules-body th,.rules-body td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--panel2)}
.rules-body th{color:var(--text);text-transform:uppercase;font-size:10.5px;letter-spacing:.4px}
.controls{display:flex;flex-direction:column;gap:8px;margin-bottom:16px}
.frow{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.flbl{color:var(--muted);font-size:12px;font-weight:600}
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
.qscore{margin-left:auto;background:var(--panel2);color:var(--neon);border-radius:6px;padding:1px 8px;font-weight:800;font-size:13px}
.tick{font-size:14px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tick b{font-size:15px;letter-spacing:-.2px}
.tick .sep{color:var(--muted)}
.tick .focus{color:var(--accent)}
.px{display:flex;align-items:baseline;gap:10px}
.px .big{font-size:22px;font-weight:800;letter-spacing:-.4px}
.px .chg{font-size:12.5px;font-weight:700}
.px .chg em{font-style:normal;font-weight:500;color:var(--muted);font-size:10px;margin-left:2px}
.px .chg.up{color:var(--green)}
.px .chg.flat{color:var(--amber)}
.px .chg.dn{color:var(--red)}
.px .cap{margin-left:auto;font-size:12px;color:var(--muted)}
.px .cap b{color:var(--text);font-weight:600}
.rrbar{display:flex;height:8px;border-radius:4px;overflow:hidden}
.rrbar .risk{background:var(--red)}
.rrbar .reward{background:var(--green);flex:1}
.rrlabels{position:relative;height:16px;font-size:10.5px;color:var(--muted);font-variant-numeric:tabular-nums;margin-bottom:3px}
.rrlabels .s{position:absolute;left:0}
.rrlabels .t{position:absolute;right:0}
.rrlabels .e{position:absolute;transform:translateX(-50%);color:var(--text);font-weight:700}
.rrblock{margin-top:10px;padding-top:10px;border-top:1px solid var(--panel2);display:flex;flex-direction:column;gap:4px}
.rrslider{display:flex;align-items:center;gap:8px}
.rrslider input[type=range]{flex:1;accent-color:var(--neon);min-width:0}
.rrcap{font-size:11px;color:var(--muted);white-space:nowrap}
.rrcap b{color:var(--accent);font-weight:700}
.gates{display:flex;gap:6px;border-top:1px solid var(--panel2);padding-top:8px}
.gate{flex:1;background:var(--panel2);color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:5px 0;cursor:pointer;font-weight:700;font-size:12px}
.gate.on{color:var(--green);border-color:rgba(46,204,113,.4)}
.gate.off{color:var(--red)}
.tags{display:flex;flex-wrap:wrap;gap:6px;min-height:18px}
.tags.empty{min-height:18px}
.tag{font-size:10.5px;padding:2px 8px;border-radius:20px;background:var(--panel2);color:var(--muted)}
.mchip{background:rgba(0,230,168,.14);color:var(--neon)}
.dok{color:var(--green)} .dno{color:var(--red)} .dunk{color:var(--muted)}
.methods{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.methods label{color:var(--muted);font-size:12px;font-weight:600;display:flex;gap:4px;align-items:center;cursor:pointer}
.notice{background:rgba(241,196,15,.10);border:1px solid rgba(241,196,15,.35);color:var(--amber);border-radius:8px;padding:8px 12px;font-size:12.5px;margin-bottom:12px}
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


def _theme_catalog() -> dict:
    """The pinned theme run's catalog for the Theme facet (store read; empty on a fresh store)."""
    try:
        return _universe.theme_catalog()
    except Exception:  # noqa: BLE001 - a missing or unreadable catalog never fails a render
        return {}


def _fmt_int(v) -> str:
    return "0" if v is None else f"{int(v):,}"


def _summary_html(s: dict) -> str:
    regime_cls, regime, momentum = regime_parts(s.get("regime"))
    spy_price = s.get("spy_price")
    spy_sma200 = s.get("spy_sma200")
    spy_3m = s.get("spy_3m_return")
    filtered = s.get("filtered_universe")
    minervini = (s.get("passing") or {}).get("minervini")
    top_n = s.get("top_n")

    spy_part = ""
    if spy_price is not None and spy_sma200 is not None:
        spy_part = f" (SPY {spy_price:,.0f} {'&gt;' if spy_price > spy_sma200 else '&lt;'} 200DMA {spy_sma200:,.0f})"

    spy_3m_part = ""
    if spy_3m is not None:
        sign = "+" if spy_3m >= 0 else ""
        spy_3m_part = f" SPY 3mo <b>{sign}{spy_3m * 100:.1f}%</b>."

    stale_part = ""
    stale_n = s.get("stale_ohlc") or 0
    if stale_n or s.get("spy_stale"):
        bits = ([f"{_fmt_int(stale_n)} tickers"] if stale_n else []) + (["SPY"] if s.get("spy_stale") else [])
        stale_part += (f" Stale price cache (refetch failed, last cached bars used): "
                       f"{_html.escape(' + '.join(bits))}.")

    return (
        '<div class="summary">'
        f'<span class="{regime_cls}">Regime {_html.escape(regime)}</span>{spy_part} — {momentum}. '
        f'{_fmt_int(filtered)} stocks in the filtered universe; {_fmt_int(minervini)} pass the Minervini gate. '
        'Layer 1: three momentum traders (Minervini Trend / Weinstein Stage 2 / Qullamaggie breakout) filter. '
        'Layer 2: survivors ranked by the 40/30/30 quality. '
        f'Strongest <b>{_fmt_int(top_n)}</b> LONG.{spy_3m_part}{stale_part}'
        '</div>'
    )


def _run_settings(payload: dict):
    """The settings the payload's run scored with (Swing review finding 2, PR 6); the store's for a payload
    written before runs carried them."""
    return swing_settings.from_payload(payload.get("settings")) or swing_settings.effective()


def _pct(x: float) -> str:
    return f"{round(x * 100, 2):g}%"


def _weights(e) -> str:
    """The momentum tie-break's weights as shares of their sum (``ranking.composite`` normalises them)."""
    parts = ((config.W_12_1, "JT 12-1"), (e.w_prox, "52-week proximity"), (e.w_cons, "consistency"))
    total = sum(w for w, _ in parts) or 1.0
    return " + ".join(f"{round(w / total * 100):g}% {name}" for w, name in parts)


def _gate_spec(e) -> dict:
    """The static gate text, per rule id, once (#384 SW-S2).

    Built from the screeners with an empty indicator set: ``label`` / ``threshold`` / ``unit`` are static, only
    ``actual`` / ``passed`` vary per candidate. The report embeds this as ``GATE_SPEC`` so a candidate's ``gates``
    carries no repeated text. The threshold text follows the run's settings ``e`` (#384 SW-S4, ``_run_settings``),
    so a saved override is named on the card rather than the default it replaced, and a save after the Fetch does
    not rewrite it.
    """
    gates = (evaluate_minervini({}, None, eff=e), evaluate_weinstein({}, eff=e),
             evaluate_qullamaggie({}, eff=e), evaluate_direction({}))
    names = {"minervini": "Minervini Trend", "weinstein": "Weinstein Stage",
             "qullamaggie": "Qullamaggie", "direction": "Direction"}
    rules: dict = {}
    for gate in gates:
        for rule in gate["rules"]:
            rules[rule["id"]] = {"label": rule["label"], "threshold": rule["threshold"], "unit": rule["unit"]}
    return {"gates": names, "rules": rules}


MODULE = "module_swing_potential"


def render_swing_dashboard(payload: dict) -> str:
    """The one-file page: the board with its rows inlined, so it boots with no fetch (tests, the guard)."""
    return _render(payload, inline=True)[0]


def render_swing_board(payload: dict) -> tuple[str, dict | None]:
    """The page and its K10 payload (``{"rows": [...]}``), or ``None`` when there is no row to fetch.

    The HTML holds no row: it fetches the payload from ``/view/module_swing_potential/payload.json``. A board with
    no rows keeps its server-rendered empty / no-data message (DF111) and needs no payload file.
    """
    return _render(payload, inline=False)


def _render(payload: dict, inline: bool) -> tuple[str, dict | None]:
    payload = payload or {}
    candidates = payload.get("candidates") or []
    passing = payload.get("passing") or {}
    avg_quality = payload.get("avg_quality")
    summary = payload.get("summary") or {}
    if not summary:
        summary = {
            "regime": payload.get("regime"),
            "filtered_universe": payload.get("universe_total"),
            "passing": payload.get("passing") or {},
            "top_n": len(candidates),
        }
    summary_html = _summary_html(summary)
    # SW-S3: the C16 funnel the payload carries replaces the old "Eliminated" line (an old payload has none).
    funnel_html = funnel_mod.funnel_panel_html(funnel_mod.from_dict(payload.get("funnel_steps")),
                                               panel_id="swing-funnel")
    # The stored payload's scoring version decides the method toggles (#384 SW-S1): a Fetch before v4 has none.
    current_version = payload.get("selection_version") == config.SELECTION_VERSION
    run = _run_settings(payload)
    universe_prov = universe_provenance_html(
        payload.get("universe_generated_at"), payload.get("universe_refreshed_by"),
        partial=bool(payload.get("universe_partial")))
    names_from = "identity spine" if _universe.universe_source() == _universe.SPINE else "old universe store"

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
    meta = script_json(filters_meta("swing", earnings_mode="filter",
                                    themes=_theme_catalog()))


    # The grid starts with a placeholder the client replaces; only the empty / no-data message is server-side (DF111).
    if payload.get("no_data"):
        hint = no_data_hint(str(payload.get("no_data_reason") or ""))
        cards = ('<div class="empty"><b>' + _html.escape(str(payload.get("no_data_message") or "No data."))
                 + '</b>' + (" " + _html.escape(hint) if hint else "") + '</div>')
    elif not rows:
        cards = '<div class="empty">No LONG momentum setups matched the screeners. Scan a broader universe and retry.</div>'
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
var GATE_SPEC = __GATE_SPEC__;
var METHODS_ON = __METHODS_ON__;
var METHODS = {minervini: true, weinstein: false, qullamaggie: false};
var METHOD_LABEL = {minervini: "Minervini", weinstein: "Weinstein", qullamaggie: "Qullamaggie"};
var META = __META__;
var RCF = null;
function pass(c){ return RCF ? RCF.pass(c) : true; }
function usd(v){
  if(v === null || v === undefined) return "N/A";
  var a = Math.abs(v), s = v < 0 ? "-" : "";
  if(a >= 1e9) return s + "$" + (a/1e9).toFixed(2) + "B";
  if(a >= 1e6) return s + "$" + (a/1e6).toFixed(1) + "M";
  if(a >= 1e3) return s + "$" + (a/1e3).toFixed(1) + "K";
  return s + "$" + Math.round(a);
}
function chgClass(v){
  if(v == null) return "";
  if(v > 0.02) return "up";
  if(v < -0.02) return "dn";
  return "flat";
}
function methodList(c){
  if(c && c.methods instanceof Array) return c.methods;
  var g = (c && c.gates) || {}, out = [];
  ["minervini","weinstein","qullamaggie"].forEach(function(k){ if(g[k] && g[k].passed) out.push(k); });
  return out;
}
function passMethods(c){
  if(!METHODS_ON) return true;                              // an old payload has no method toggles (#384 SW-S1)
  var ticked = ["minervini","weinstein","qullamaggie"].filter(function(k){ return METHODS[k]; });
  if(!ticked.length) return true;                           // none ticked: every candidate shows
  if(!(c && c.methods instanceof Array)) return true;       // unknown: never hide a row without the key
  return ticked.every(function(k){ return c.methods.indexOf(k) >= 0; });
}
function setMethod(el){
  METHODS[el.getAttribute("data-method")] = !!el.checked;
  render();
}
function fmtVal(unit, v){
  if(v === null || v === undefined) return "N/A";
  if(unit === "price") return "$" + Number(v).toFixed(2);
  if(unit === "pct") return (v*100).toFixed(1) + "%";
  if(unit === "pct_high") return (v*100).toFixed(1) + "% of high";
  if(unit === "ratio") return Number(v).toFixed(2) + "×";
  if(unit === "rating") return Number(v).toFixed(1);
  if(unit === "diff_price") return (v>=0?"+":"") + "$" + Number(v).toFixed(2);
  if(unit === "diff_pct") return (v>=0?"+":"") + (v*100).toFixed(1) + "%";
  return String(v);
}
function cardHtml(c){
  var t = rcEsc(c.ticker);
  var name = rcEsc(c.name);
  var sector = rcEsc(c.sector);
  var chg1w = c.change_1w;
  var chg1wTxt = (chg1w == null) ? "N/A" : ((chg1w >= 0 ? "+" : "") + (chg1w*100).toFixed(1) + "%");
  var chg1m = c.change_1m;
  var chg1mTxt = (chg1m == null) ? "N/A" : ((chg1m >= 0 ? "+" : "") + (chg1m*100).toFixed(1) + "%");
  var rsTxt = (c.rs_rating == null) ? "—" : "+" + Math.round(c.rs_rating);
  var rr = (c.rr == null) ? null : c.rr;                    // a null R:R shows "n/a" and no slider (#384 SW-S2, DF81)
  var qscoreTxt = (c.quality == null) ? "—" : Math.round(c.quality);

  var entry = c.entry, stop = c.stop, target = c.target, price = c.price;
  var riskFrac = 0.5;
  if(rr != null) riskFrac = 1/(1+rr);
  if(entry != null && stop != null && target != null && target !== stop){ riskFrac = (entry - stop)/(target - stop); }
  riskFrac = Math.max(0, Math.min(1, riskFrac));
  var riskPos = (riskFrac*100).toFixed(1) + "%";
  var atrPct = (c.atr != null && price) ? (c.atr/price*100).toFixed(1) + "%" : "N/A";
  var stopDistPct = (entry != null && stop != null && entry) ? ((entry - stop)/entry*100).toFixed(1) + "%" : "N/A";
  var rrTxt = (rr == null) ? "n/a" : rr.toFixed(1);

  var gates = ["minervini","weinstein","qullamaggie"].map(function(k){
    var g = c.gates[k];
    var lbl = {minervini:"M", weinstein:"W", qullamaggie:"Q"}[k];
    return '<button class="gate ' + (g.passed ? "on" : "off") + '" data-ticker="' + t + '" data-gate="' + k + '" onclick="openGate(this)">' + lbl + ' ' + rcEsc(g.score) + '</button>';
  }).join("");
  var chips = methodList(c).map(function(k){ return '<span class="tag mchip">'+rcEsc(METHOD_LABEL[k]||k)+'</span>'; }).join("");
  var dirOk = (c.direction_ok != null) ? c.direction_ok : ((c.gates && c.gates.direction) ? c.gates.direction.passed : null);
  var dirCls = dirOk === true ? "dok" : (dirOk === false ? "dno" : "dunk");
  var dirTxt = dirOk === true ? "Direction ✓" : (dirOk === false ? "Direction ✗" : "Direction ?");
  var dirChip = '<span class="tag '+dirCls+'">'+dirTxt+'</span>';
  var themes = c.themes || [];
  var tags = themes.slice(0,3).map(function(x){ return '<span class="tag">'+rcEsc(x)+'</span>'; }).join("");
  var tagsRow = tags ? '<div class="tags">'+tags+'</div>' : '';
  var chipsRow = '<div class="tags">'+chips+dirChip+'</div>';
  var indexBadges = (c.index || []).map(function(i){ return '<span class="badge b-index">'+rcEsc(i)+'</span>'; }).join("");
  var slider = (rr != null && price != null && stop != null)
    ? '<input type="range" min="1" max="5" step="0.1" value="' + rr.toFixed(1) + '" data-ticker="' + t + '" oninput="setRR(this.dataset.ticker, this.value)">'
    : "";
  return '<div class="card">'
    + '<div class="head">'
    + (sector ? '<span class="badge b-sector">'+sector+'</span>' : '')
    + indexBadges
    + '<span class="qscore" title="Quality (40/30/30)">' + qscoreTxt + '</span></div>'
    + '<div class="tick"><b>' + t + '</b>'
    + (name ? '<span class="sep"> • </span>'+name : '') + '</div>'
    + rcWatchRow(c.ticker)
    + '<div class="px"><span class="big">$' + (price != null ? price.toFixed(2) : "N/A") + '</span>'
    + '<span class="chg ' + chgClass(chg1w) + '">' + chg1wTxt + '<em>1W</em></span>'
    + '<span class="chg ' + chgClass(chg1m) + '">' + chg1mTxt + '<em>1M</em></span>'
    + '<span class="cap">Cap <b>' + usd(c.market_cap) + '</b></span></div>'
    + '<div class="rrblock">'
    + '<div class="rrlabels">'
    + '<span class="s">Stop $' + (stop != null ? stop.toFixed(2) : "N/A") + '</span>'
    + '<span class="e" id="entry-'+t+'" style="left:' + riskPos + '">Entry $' + (entry != null ? entry.toFixed(2) : "N/A") + '</span>'
    + '<span class="t" id="target-'+t+'">Target $' + (target != null ? target.toFixed(2) : "N/A") + '</span></div>'
    + '<div class="rrbar"><div class="risk" id="riskseg-'+t+'" style="width:' + riskPos + '"></div><div class="reward"></div></div>'
    + '<div class="rrslider">' + slider
    + '<span class="rrcap" id="rrcap-'+t+'">(R:R <b>' + rrTxt + '</b> · RS ' + rsTxt + ' · ATR ' + atrPct + ' · Stop ' + stopDistPct + ')</span></div>'
    + '</div>'
    + tagsRow
    + chipsRow
    + '<div class="gates">'+gates+'</div>'
    + '</div>';
}
function setRR(t, val){
  var c = DATA.filter(function(x){ return x.ticker===t; })[0];
  if(!c || c.entry == null || c.stop == null) return;
  var R = parseFloat(val);
  var risk = c.entry - c.stop;
  var target = c.entry + R*risk;
  var frac = 1/(1+R);
  var pos = (frac*100).toFixed(1) + "%";
  var rsTxt = (c.rs_rating == null) ? "—" : "+" + Math.round(c.rs_rating);
  var atrPct = (c.atr != null && c.price) ? (c.atr/c.price*100).toFixed(1) + "%" : "N/A";
  var stopDistPct = c.entry ? ((c.entry - c.stop)/c.entry*100).toFixed(1) + "%" : "N/A";
  document.getElementById("target-"+t).textContent = "Target $" + target.toFixed(2);
  document.getElementById("entry-"+t).style.left = pos;
  document.getElementById("riskseg-"+t).style.width = pos;
  document.getElementById("rrcap-"+t).innerHTML = "(R:R <b>" + R.toFixed(1) + "</b> · RS " + rsTxt
    + " · ATR " + atrPct + " · Stop " + stopDistPct + ")";
}
function openGate(btn){
  var t = btn.getAttribute("data-ticker");
  var g = btn.getAttribute("data-gate");
  var gate = (GATES[t]||{})[g];
  if(!gate) return;
  var spec = GATE_SPEC.rules || {};
  document.getElementById("m-title").textContent = (GATE_SPEC.gates||{})[g] || gate.name || g;
  document.getElementById("m-sub").textContent = t + " — " + gate.score + (gate.passed ? " (PASS)" : " (FAIL)");
  var rules = gate.rules.map(function(r){
    var meta = spec[r.id] || {};
    var ok = r.passed;   // true / false / null (unknown: an input is missing)
    var cls = ok === null ? 'm-unk' : (ok ? 'm-ok' : 'm-no');
    var mark = ok === null ? '?' : (ok ? '&#10003;' : '&#10007;');
    return '<div class="rule"><span class="mark '+cls+'">'+mark+'</span>'
      + '<span class="lbl">'+rcEsc(meta.label || r.label || r.id || "")+'</span>'
      + '<span class="th">'+rcEsc(meta.threshold || r.threshold || "")+'</span>'
      + '<span class="act">'+rcEsc(fmtVal(meta.unit || r.unit, r.actual))+'</span></div>';
  }).join("");
  document.getElementById("m-rules").innerHTML = rules;
  document.getElementById("modal").classList.add("open");
}
function closeModal(){ document.getElementById("modal").classList.remove("open"); }
function render(){
  if(!DATA.length){ document.getElementById("count").textContent = "0 setups shown"; return; }   // keep the server-side empty / no-data message (DF111)
  var q = document.getElementById("q").value.trim().toLowerCase();
  var out = DATA.filter(function(c){
    if(!passMethods(c)) return false;
    if(!pass(c)) return false;
    if(q && (c.ticker.toLowerCase().indexOf(q) < 0 && (c.name||"").toLowerCase().indexOf(q) < 0)) return false;
    return true;
  });
  document.getElementById("count").textContent = out.length + " setups shown";
  document.getElementById("grid").innerHTML = out.length
    ? out.map(cardHtml).join("")
    : '<div class="empty">No setups match the current filters.</div>';
}
function init(){
  RCF = RCFilters.mount({root: "#rc-filters", rows: DATA, meta: META, onChange: function(){ render(); }});
  render();
}
function boot(d){
  DATA = (d && d.rows) || [];
  GATES = {};
  DATA.forEach(function(c){ GATES[c.ticker] = c.gates || {}; });
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
                   "__GATE_SPEC__": script_json(_gate_spec(run)),
                   "__METHODS_ON__": script_json(current_version),
                   "__META__": meta})

    help_block = help_html(
        "A long-side momentum scan of US-listed stocks through three swing-trading setups: "
        "<b>Minervini</b> (the 8-point trend template), <b>Weinstein</b> (stage 2 advance) and "
        "<b>Qullamaggie</b> (breakout). The tiles count the names passing each one; options flow is not "
        "part of the score. Prices and company data come from <b>Yahoo</b>. <b>Fetch</b> runs the scan "
        "and stores the scores. <b>Refresh</b> shows the last Fetch's stored result with no network "
        "call and no re-scoring.")
    if current_version:
        methods_controls = (
            '<div class="frow methods" id="method-toggles">'
            '<span class="flbl">Methods</span>'
            '<label><input type="checkbox" data-method="minervini" checked onchange="setMethod(this)"> Minervini</label>'
            '<label><input type="checkbox" data-method="weinstein" onchange="setMethod(this)"> Weinstein</label>'
            '<label><input type="checkbox" data-method="qullamaggie" onchange="setMethod(this)"> Qullamaggie</label>'
            '</div>')
        notice_html = ""
    else:
        methods_controls = ""
        notice_html = ('<div class="notice" id="old-version">'
                       + _html.escape("Scored before v4 (40/30/30 quality and the direction rule). "
                                      "Run a Fetch to rescore.") + '</div>')
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Swing Screener</title>
<style>{CSS}</style>
{FILTERS_CSS}
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>Swing Screener</h1>
      <div class="sub">Minervini · Weinstein · Qullamaggie — swing momentum setups (LONG). A 1–3 month horizon; momentum is not a probability.</div>
      {universe_prov}
    </div>
  </div>

  {provider_health_html(payload.get("provider_health"))}
  {help_block}
  {recorded_sessions_html("module_swing_potential")}

  {summary_html}
  {funnel_html}
  {notice_html}

  <div class="banner">
    <div class="stat"><div class="v">{payload.get("universe_total") or 0}</div><div class="l">Scanned Universe</div><div class="d">US-listed · all caps</div></div>
    <div class="stat"><div class="v">{passing.get("minervini") or 0}</div><div class="l">Minervini</div><div class="d">7 price rules · RS ranks</div></div>
    <div class="stat"><div class="v">{passing.get("weinstein") or 0}</div><div class="l">Weinstein</div><div class="d">5/5 stage 2</div></div>
    <div class="stat"><div class="v">{passing.get("qullamaggie") or 0}</div><div class="l">Qullamaggie</div><div class="d">5/6 breakout</div></div>
    <div class="stat"><div class="v">{avg_quality if avg_quality is not None else "—"}</div><div class="l">Avg Quality</div><div class="d">40/30/30 quality</div></div>
  </div>

  <details class="rules">
    <summary>Methodology / Rules</summary>
    <div class="rules-body">
      <table>
        <tr><th>Screener</th><th>Criteria</th><th>Scoring</th></tr>
        <tr><td><b>Minervini Trend</b></td>
          <td>Price above 150 &amp; 200-day MA · 150&gt;200-day MA · 200-day MA rising · 50&gt;150-day MA · price above 50-day MA · ≥{_pct(config.ABOVE_52W_LOW - 1)} above 52-week low · within {_pct(1 - config.WITHIN_52W_HIGH)} of 52-week high · RS rating ≥ {run.rs_min:g}</td>
          <td>7/7 price rules required; RS rating ≥ {run.rs_min:g} ranks, does not decide</td></tr>
        <tr><td><b>Weinstein Stage</b></td>
          <td>Price above 30-week MA · 30-week MA rising · overhead breakout (≥{_pct(run.breakout_high_pct)} of 52w high) · volume ≥{run.vol_confirm:g}× average · 3-month RS &gt; SPY</td>
          <td>5/5 required</td></tr>
        <tr><td><b>Qullamaggie</b></td>
          <td>Preceding move ≥{_pct(run.move_min)} (1–3 mo) · price above 10 &amp; 20-day EMA · 10&gt;20-day EMA · EMAs rising · ADR ≥{_pct(run.adr_min)}</td>
          <td>5 of 6 required</td></tr>
        <tr><td><b>Quality</b></td>
          <td>Quality = 40 × Minervini rules passed/8 + 30 × Weinstein/5 + 30 × Qullamaggie/6; momentum ({_weights(run)}) breaks ties</td>
          <td>0–100; ranks the board</td></tr>
        <tr><td><b>Direction</b></td>
          <td>Direction (required): 50-day MA ≥ 200-day MA and 3-month return ≥ 0</td>
          <td>Every card must pass</td></tr>
        <tr><td><b>Levels</b></td>
          <td>Entry = current price · Stop = entry − {run.stop_atr:g}×ATR({config.ATR_PERIOD}) · Target = entry + {run.target_r:g}×risk (R:R {run.target_r:.1f})</td>
          <td>Stop/Entry/Target on bar</td></tr>
      </table>
    </div>
  </details>

  <div class="controls">
    {methods_controls}
    <div class="frow">
      <input type="text" id="q" placeholder="Search ticker / company" oninput="render()" />
      <span class="count" id="count"></span>
    </div>
  </div>

  {filters_bar_html("rc-filters")}

  <div class="grid" id="grid">{cards}</div>

  <footer>
    <span>Sources: {names_from} (names) · daily bars (Unusual Whales, yfinance fallback) · Yahoo Finance (company info, market caps) · Unusual Whales (flow — manual only). For information only, not financial advice.</span>
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
{watchlist_html("swing_potential")}
<script>
{JS}
document.getElementById("modal").addEventListener("click", function(e){{ if(e.target === this) closeModal(); }});
start();
</script>
</body>
</html>""", (None if inline else data)
