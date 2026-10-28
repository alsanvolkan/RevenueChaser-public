"""Unified HTML — grouped columns with RGBA background tones per group. Single page, all tickers."""
from RevenueChaser.core import universe
from RevenueChaser.core.reporting_common import (FILTERS_CSS, FILTERS_JS, filters_bar_html, filters_meta, help_html,
                                                 notice_html, provider_health_html, recorded_sessions_html,
                                                 watchlist_html, write_artifact)
from datetime import date, datetime, timezone
from pathlib import Path
import json
from RevenueChaser.core import rawstore
from .composite import classify_bias
from RevenueChaser.core.html_safe import ESC_JS, esc, script_json
from RevenueChaser.core.rotation_reference import ROTATION_ETFS
from RevenueChaser.core.config import (
    OUTPUT_DIR, MIN_OPTION_VOLUME, MIN_TOTAL_OI, MIN_DOLLAR_OPTION_VOLUME, RULE_WEIGHTS,
)
from RevenueChaser.core.calendar import NY

# E3 (#372 ER-C): the Coverage denominator, rendered from the config so it cannot drift from
# composite.py (the sum of the direction weights R2/R3/R7/R8 carry).
_DIRECTION_WEIGHT_SUM = sum(w for w in RULE_WEIGHTS.values() if w > 0)

# #383 C7: this page is written by its Fetch just before that Fetch is recorded, and a Refresh serves the same file.
_HISTORY_NOTE = ("This page is written by its Fetch just before that Fetch is recorded, and a Refresh shows the same "
                 "page, so the newest Fetch shows up here from the next Fetch.")

CSS = """<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Consolas,monospace;padding:10px 12px}
.header{margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid #21262d}
.header h1{color:#58a6ff;font-size:17px;letter-spacing:1px}
.header .meta{color:#8b949e;font-size:12px;margin-top:2px}
.stats{display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap}
.stat-card{background:#161b22;border:1px solid #21262d;border-radius:6px;padding:5px 10px;min-width:72px}
.stat-card .label{color:#8b949e;font-size:9px;text-transform:uppercase;letter-spacing:1px}
.stat-card .value{font-size:15px;font-weight:700;margin-top:0}
.bull .value{color:#3fb950}.bear .value{color:#f85149}.neut .value{color:#8b949e}.wl .value{color:#d2991d}
.formula{background:#161b22;border:1px solid #21262d;border-radius:6px;padding:7px 10px;margin:6px 0;font-size:12px;color:#8b949e;line-height:1.6}
.formula b{color:#58a6ff}.formula span{color:#d2991d}
.section{margin-bottom:14px}
.section-title{font-size:13px;font-weight:700;padding:5px 0;margin-bottom:4px;border-bottom:1px solid #21262d;display:flex;justify-content:space-between}
.section-desc{color:#8b949e;font-size:11px;margin-bottom:6px;line-height:1.5}
.section-desc em{color:#d2991d;font-style:normal}
.st-A{color:#3fb950}.st-B{color:#56d364}.st-C{color:#58a6ff}.st-I{color:#8b949e}
table{width:100%;min-width:0;border-collapse:collapse;font-size:10px}
.data-table{table-layout:fixed;width:100%;min-width:0}
/* The two header rows pin as one unit (the sticky thead), so the group row no
   longer slides under the column row on vertical scroll. */
table.data-table thead{position:sticky;top:0;z-index:5}
th{text-align:left;padding:3px 4px;color:#c9d1d9;font-size:9px;font-weight:600;letter-spacing:0.3px;border-bottom:1px solid #21262d;white-space:normal;overflow-wrap:anywhere;line-height:1.05}
th.group{font-size:9px;text-transform:uppercase;letter-spacing:1px;text-align:center;border-bottom:1px solid #30363d;padding:3px 4px}
table.data-table th.sticky-tick,table.data-table td.sticky-tick{position:sticky;left:0;background:#161b22}
table.data-table th.sticky-tick{z-index:6}
table.data-table td.sticky-tick{z-index:1}
table.data-table tr:hover td.sticky-tick{background:#1c2330}
table.data-table th[data-sort-key]{cursor:pointer;-webkit-user-select:none;user-select:none}
table.data-table th[data-sort-key]:hover{color:#58a6ff}
table.data-table th[data-sort-key]::after{content:"⇅";font-size:7px;opacity:.4;margin-left:2px;display:inline-block}
table.data-table th[data-sort-key][data-dir="asc"]::after{content:"▲";opacity:1;color:#3fb950}
table.data-table th[data-sort-key][data-dir="desc"]::after{content:"▼";opacity:1;color:#f85149}
details.methodology>summary{cursor:pointer;list-style:none}
details.methodology>summary::-webkit-details-marker{display:none}
td{padding:2px 4px;border-bottom:1px solid #161b22;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
tr:hover{background:#161b22}
.ticker{color:#58a6ff;font-weight:700}.star{color:#d2991d;font-size:11px}.cluster{color:#8b949e;font-size:9px}
.excl-tag{color:#f85149;font-size:10px;margin-left:4px;cursor:help}
.excluded-section .section-title{color:#f85149}
.bull{color:#3fb950}.bear{color:#f85149}.neut{color:#d2991d}
.badge{display:inline-block;padding:1px 6px;border-radius:9px;font-size:8px;font-weight:700}
.bd-A{background:#1a3a1a;color:#3fb950;border:1px solid #238636}
.bd-B{background:#1a2a1a;color:#56d364;border:1px solid #2ea043}
.bd-C{background:#1a1a2e;color:#58a6ff;border:1px solid #1f6feb}
.bd-I{background:#1a1a1a;color:#8b949e;border:1px solid #30363d}
.bd-L{background:#1a3a1a;color:#3fb950;border:1px solid #238636}
.bd-W{background:#2a2410;color:#d2991d;border:1px solid #9e6a03}
.bd-LG{background:#1a1a1a;color:#f85149;border:1px solid #da3633}
.bd-IM{background:#1a1a2e;color:#58a6ff;border:1px solid #1f6feb}
.bd-bull{background:#1a3a1a;color:#3fb950;border:1px solid #238636}
.bd-bear{background:#2a1515;color:#f85149;border:1px solid #da3633}
.bd-neutral{background:#1a1a1a;color:#8b949e;border:1px solid #30363d}
.em{color:#a5d6ff}.vol{color:#d2991d}
/* Group header tones */
.th-g1{background:rgba(88,166,255,0.10)}
.th-g2{background:rgba(63,185,80,0.08)}
.th-g3{background:rgba(210,153,29,0.08)}
.th-g4{background:rgba(139,148,158,0.08)}
.td-g1{background:rgba(88,166,255,0.03)}
.td-g2{background:rgba(63,185,80,0.03)}
.td-g3{background:rgba(210,153,29,0.03)}
.td-g4{background:rgba(139,148,158,0.03)}
.th-g5{background:rgba(163,113,247,0.08)}
.td-g5{background:rgba(163,113,247,0.03)}
.th-g6{background:rgba(248,81,73,0.06)}
.td-g6{background:rgba(248,81,73,0.02)}
.th-g7{background:rgba(56,189,248,0.08)}
.td-g7{background:rgba(56,189,248,0.03)}
.structure-cell{font-size:8px;color:#a5d6ff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:help}
.gex-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;min-width:118px;font-size:8px}
.gex-grid span{padding:1px 1px;text-align:center;background:rgba(88,166,255,.06);border:1px solid #21262d;border-radius:2px}
.gex-grid b{display:block;color:#8b949e;font-size:6px;font-weight:600;text-transform:uppercase}
.pe-cell{display:flex;gap:4px;justify-content:center}
.pe-col{font-size:8px;display:flex;flex-direction:column;align-items:center;line-height:1.1}
.pe-col i{font-style:normal;font-size:6px;color:#6e7681;text-transform:uppercase}
tr.watchlist-row{background:rgba(210,153,29,0.06)}

.why-cell{font-size:8px;text-align:left;max-width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
.why-cell:hover{white-space:normal;overflow:visible;background:#161b22;position:relative;z-index:10;max-width:200px}
.rule-score{font-size:9px;font-weight:600}
.rule-pos{color:#3fb950}.rule-neg{color:#f85149}.rule-zero{color:#8b949e}
/* TW Candle colors */
.tw-start-bull{color:#56D364}
.tw-cont-bull{color:#3FB950}
.tw-conf-bull{color:#008800;font-weight:700}
.tw-start-bear{color:#FF6B6B}
.tw-cont-bear{color:#F85149}
.tw-conf-bear{color:#BB0000;font-weight:700}
.tw-trim{color:#8B949E}
.tw-na{color:#484F58}
.tw-pct{font-size:9px;display:block;margin-top:1px}
.tw-label{font-size:10px;line-height:1.2}
.name-cell{color:#c9d1d9;font-size:10px;max-width:120px;overflow:hidden;text-overflow:ellipsis}
.beat-cell{text-align:center;font-size:0}
.beat-track{display:inline-block;width:18px;height:16px;background:#161b22;border-radius:2px;position:relative;vertical-align:bottom;overflow:hidden;border:1px solid #21262d}
.beat-fill-v{position:absolute;left:0;right:0;bottom:0;border-radius:0 0 1px 1px;transition:height .3s}
.beat-fill-v.green{background:rgba(63,185,80,0.7)}
.beat-fill-v.red{background:rgba(248,81,73,0.7)}
.beat-q-label{font-size:7px;color:#6e7681;display:block;text-align:center;margin-bottom:1px}
.beat-val{font-size:7px;display:block;text-align:center;margin-top:1px}
.beat-val.green{color:#3fb950}.beat-val.red{color:#f85149}
.eps-val{font-size:7px;display:block;text-align:center;margin-top:1px}
.eps-val.green{color:#3fb950}.eps-val.red{color:#f85149}.eps-val.gray{color:#8b949e}
.since-er-label{font-size:8px;color:#6e7681;display:block}
footer{margin-top:12px;padding-top:8px;border-top:1px solid #21262d;color:#484f58;font-size:10px;text-align:center}
/* Earnings detail drawer */
tr.detail-row td{background:#0d1117;padding:0;border-bottom:1px solid #30363d}
.drawer{background:#10141b;border-top:1px solid #30363d;border-bottom:1px solid #30363d;margin:4px 0;padding:4px 0}
.drawer-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;padding:0 6px}
.drawer-title{color:#c9d1d9;font-size:13px;font-weight:700}
.drawer-close{color:#8b949e;cursor:pointer;font-size:14px;padding:0 5px}
.drawer-close:hover{color:#f85149}
.drawer-toolbar{display:flex;align-items:center;gap:6px;margin-bottom:6px;padding:0 6px}
.dtab{background:#161b22;color:#8b949e;border:1px solid #30363d;padding:3px 8px;border-radius:5px;cursor:pointer;font-size:11px}
.dtab.active{color:#58a6ff;border-color:#1f6feb;background:#1a1a2e}
.dperiod{margin-left:auto;display:flex;gap:4px}
.dp{background:#161b22;color:#8b949e;border:1px solid #30363d;padding:2px 7px;border-radius:5px;cursor:pointer;font-size:10px}
.dp.active{color:#d2991d;border-color:#9e6a03;background:#2a2410}
.drawer-chart{width:100%;overflow-x:auto}
.drawer-metrics{display:flex;flex-wrap:wrap;gap:6px;margin-top:5px;font-size:10px;color:#8b949e}
.drawer-metrics b{color:#c9d1d9}
.drawer-metrics .pos{color:#3fb950}.drawer-metrics .neg{color:#f85149}
.drawer-cols{width:100%}
.drawer-panel{background:#0d1117;border:1px solid #21262d;border-radius:5px;padding:4px;margin-top:0;min-width:0}
.drawer-panel-title{color:#d2991d;font-size:11px;font-weight:700;margin-bottom:2px;text-transform:uppercase;letter-spacing:1px}
.decision-split{display:grid;grid-template-columns:190px 1fr;gap:8px;align-items:start}
.ruleweb{min-width:0}
.decision-audit{font-size:10px;color:#8b949e;line-height:1.4}
.decision-audit table{border-collapse:collapse;width:100%;margin-top:3px}
.decision-audit td{padding:1px 4px;vertical-align:top;border-bottom:1px solid #1c2128}
.decision-audit td.ra{color:#c9d1d9;font-weight:700;white-space:nowrap;width:1%}
.decision-audit td.rscore{width:1%;white-space:nowrap;font-weight:700}
.decision-audit .dpos{color:#3fb950}
.decision-audit .dneg{color:#f85149}
.decision-audit .rreasons{color:#6e7681}
.decision-summary{font-size:10px;color:#c9d1d9}
.decision-summary b{color:#e6edf3}
/* Decision drawer cards (user-friendly boxes) */
.dcards{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:3px}
.dcard{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:4px 8px;min-width:92px;flex:1 1 0}
.dcard .dl{color:#8b949e;font-size:8px;text-transform:uppercase;letter-spacing:.8px}
.dcard .dv{font-size:11px;font-weight:700;margin-top:2px;color:#e6edf3}
.dcard .dv.sub{font-size:9px;color:#6e7681;font-weight:400;margin-top:1px}
.rulecards{display:flex;flex-direction:column;gap:4px;margin-top:4px}
.rcard{background:#0d1117;border:1px solid #21262d;border-left:3px solid #30363d;border-radius:5px;padding:3px 8px;display:flex;gap:8px;align-items:baseline}
.rcard.pos{border-left-color:#3fb950}
.rcard.neg{border-left-color:#f85149}
.rcard.tag{border-left-color:#8b949e}
.rcard .rname{color:#c9d1d9;font-weight:700;white-space:nowrap;font-size:10px}
.rcard .rrole{color:#6e7681;font-size:8px;white-space:nowrap}
.rcard .rwhy{color:#8b949e;font-size:9px;line-height:1.3}
.rcard .rwhy .ok{color:#3fb950}
.rcard .rwhy .no{color:#f85149}
.dp-label{color:#8b949e;font-size:10px}
/* KPI row + filter chips */
.kpi-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.kpi{background:#161b22;border:1px solid #21262d;border-radius:6px;padding:5px 9px;min-width:80px}
.kpi .label{color:#8b949e;font-size:8px;text-transform:uppercase;letter-spacing:1px;display:block}
.kpi .value{font-size:13px;font-weight:700;margin-top:1px;display:block}
.filter-chips{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:8px}
.chip{background:#161b22;color:#8b949e;border:1px solid #21262d;padding:2px 9px;border-radius:12px;cursor:pointer;font-size:10px}
.chip.active{color:#58a6ff;border-color:#1f6feb;background:#1a1a2e}
.sort-bar{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:8px;align-items:center}
.sort-bar .sort-label{color:#8b949e;font-size:10px}

/* ============================================================
   Tier B Watchlist redesign — token layer (overrides legacy)
   Visual source of truth: gui-redesign/mockup/index.html
   ============================================================ */
:root{
  --bg-void:#0B0F14;--bg-primary:#10161D;--bg-secondary:#151C24;--bg-surface:#1A232C;
  --bg-elevated:#212B36;--bg-hover:#24303C;--bg-selected:#1C2A38;
  --border-subtle:#24303C;--border:#2C3A48;--border-strong:#3A4B5C;
  --text:#E8EEF4;--text-2:#A8B6C4;--text-3:#7A8A99;
  --bull:#3DDC97;--bull-bg:rgba(61,220,151,0.14);--bull-bd:rgba(61,220,151,0.35);
  --bear:#F07167;--bear-bg:rgba(240,113,103,0.14);--bear-bd:rgba(240,113,103,0.35);
  --neutral:#E8B84A;--neutral-bg:rgba(232,184,74,0.14);--neutral-bd:rgba(232,184,74,0.35);
  --info:#5BA3E8;--info-bg:rgba(91,163,232,0.14);--info-bd:rgba(91,163,232,0.35);
  --quality:#7DD3E8;--am:#E8B84A;--pm:#E08A4A;
  --font:Inter,ui-sans-serif,system-ui,'Segoe UI',sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;
  --row-h:40px;--drawer-w:480px;
  --stick-ticker:50px;--stick-name:164px;--stick-price:236px;
}
body{padding:0;background:var(--bg-void);color:var(--text);font-family:var(--font);font-size:13px}
.app{display:flex;height:100vh;overflow:hidden}
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.board{flex:1;overflow:auto;scrollbar-gutter:stable;scrollbar-width:thin;scrollbar-color:#3A4B5C var(--bg-void)}
.board::-webkit-scrollbar{width:12px;height:12px}
.board::-webkit-scrollbar-track{background:var(--bg-void)}
.board::-webkit-scrollbar-thumb{background:var(--border-strong);border-radius:8px;border:3px solid var(--bg-void)}
.board::-webkit-scrollbar-thumb:hover{background:var(--text-3)}
.board::-webkit-scrollbar-corner{background:var(--bg-void)}
.header{margin:0;padding:10px 14px 8px;border-bottom:1px solid var(--border-subtle);display:flex;align-items:flex-end;justify-content:space-between;gap:12px}
.header h1{color:var(--text);font-size:18px;letter-spacing:.02em}
.header .meta{color:var(--text-3);font-size:11px;margin-top:2px;font-family:var(--mono)}
.header .sub{color:var(--text-2);font-size:12px;margin-top:2px}
.header .sync{color:var(--text-3);font-size:11px;white-space:nowrap;font-family:var(--mono)}
.toolbar{height:48px;display:flex;align-items:center;gap:10px;padding:0 14px;background:var(--bg-primary);border-bottom:1px solid var(--border-subtle);flex-wrap:nowrap;overflow-x:auto}
.kit-count{color:var(--text-3,#8b949e);font-size:12px;margin:6px 14px 0;min-height:1.2em}
.seg{display:flex;background:var(--bg-secondary);border:1px solid var(--border);border-radius:8px;padding:2px}
.seg button{background:transparent;border:0;color:var(--text-2);padding:5px 10px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:500;font-family:inherit}
.seg button.on{background:var(--bg-elevated);color:var(--text)}
.tb-search{background:var(--bg-secondary);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:6px 10px;width:180px;font-size:12px;font-family:inherit}
.tb-search:focus{outline:1px solid var(--info)}
.chips{display:flex;gap:6px;flex-wrap:nowrap;overflow:auto}
.spacer{flex:1}
.tb-meta{color:var(--text-3);font-size:11px;font-family:var(--mono);white-space:nowrap}
.tb-kbd{color:var(--text-3);font-size:11px;white-space:nowrap}
.chip{background:var(--bg-secondary);color:var(--text-2);border:1px solid var(--border);padding:3px 10px;border-radius:999px;cursor:pointer;font-size:11px;white-space:nowrap}
.chip.active{color:var(--text);border-color:var(--info-bd);background:var(--info-bg)}
.colpick{position:relative;display:none}
.colpick>summary{list-style:none;cursor:pointer;background:var(--bg-secondary);color:var(--text-2);border:1px solid var(--border);border-radius:999px;padding:3px 10px;font-size:11px;white-space:nowrap}
.colpick>summary::-webkit-details-marker{display:none}
.colpick-body{position:absolute;top:32px;left:0;z-index:20;background:var(--bg-elevated);border:1px solid var(--border);border-radius:10px;padding:10px 12px;display:grid;grid-template-columns:1fr 1fr;gap:4px 14px;min-width:280px;box-shadow:0 8px 24px rgba(0,0,0,.45)}
.colpick-body label{display:flex;gap:6px;align-items:center;font-size:11px;color:var(--text-2);white-space:nowrap}
.radar-details{margin-top:6px}
.radar-details>summary{color:var(--text-3);font-size:11px;cursor:pointer}
.radar-details .ruleweb{margin-top:6px}
.session{display:flex;align-items:center;gap:8px;height:28px;padding:0 14px;background:var(--bg-secondary);border-bottom:1px solid var(--border-subtle);position:sticky;top:0;z-index:4}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot.am{background:var(--am)}.dot.pm{background:var(--pm)}.dot.other{background:var(--text-3)}
.session h2{margin:0;font-size:12px;font-weight:600}
.session .count{color:var(--text-3);font-size:11px}
table.data-table thead{top:28px}
th{background:var(--bg-primary);color:var(--text-3);font-size:10px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;padding:6px 10px;border-bottom:1px solid var(--border-subtle)}
th.group{background:var(--bg-primary);color:var(--text-3);font-size:10px}
tbody tr{height:var(--row-h)}
td{padding:0 10px;border-bottom:1px solid var(--border-subtle);font-size:12px;color:var(--text-2)}
tr:hover{background:var(--bg-hover)}
tr.sel{background:var(--bg-selected)}
tr.sel td{background:var(--bg-selected)}
.ticker{color:var(--text);font-weight:650;font-size:14px;letter-spacing:.02em}
.name-cell{color:var(--text-3);font-size:11px;max-width:150px}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.em,.vol{color:var(--text-2)}
table.data-table td,table.data-table th{font-variant-numeric:tabular-nums}
.sticky-tick,.sticky-name,.sticky-price{position:sticky;background:var(--bg-void)}
tr:hover .sticky-tick,tr:hover .sticky-name,tr:hover .sticky-price{background:var(--bg-hover)}
tr.sel .sticky-tick,tr.sel .sticky-name,tr.sel .sticky-price{background:var(--bg-selected)}
table.data-table th.sticky-tick,table.data-table th.sticky-name,table.data-table th.sticky-price{background:var(--bg-primary);z-index:6}
table.data-table td.sticky-tick,table.data-table td.sticky-name,table.data-table td.sticky-price{z-index:2}
table.data-table th.sticky-tick,table.data-table td.sticky-tick{left:0}
table.data-table th.sticky-name,table.data-table td.sticky-name{left:var(--stick-ticker)}
table.data-table th.sticky-price,table.data-table td.sticky-price{left:var(--stick-name)}
table.data-table th.sticky-price.sticky-off,table.data-table td.sticky-price.sticky-off{position:static}
tr:not(.sel) td.sticky-name{border-right:1px solid var(--border-subtle)}
.struct-row{display:grid;grid-template-columns:64px 1fr;gap:8px;margin-bottom:4px}
.struct-row b{color:var(--text-3);font-weight:500}
/* Badges — one tone system */
.badge{display:inline-flex;align-items:center;gap:4px;border-radius:999px;padding:2px 8px;font-size:10px;font-weight:600;letter-spacing:.03em;border:1px solid transparent;line-height:16px;white-space:nowrap}
.tone-bull{background:var(--bull-bg);color:var(--bull);border-color:var(--bull-bd)}
.tone-bear{background:var(--bear-bg);color:var(--bear);border-color:var(--bear-bd)}
.tone-neutral{background:var(--neutral-bg);color:var(--neutral);border-color:var(--neutral-bd)}
.tone-info{background:var(--info-bg);color:var(--info);border-color:var(--info-bd)}
.tone-muted{background:var(--bg-elevated);color:var(--text-2);border-color:var(--border)}
.bd-bull{background:var(--bull-bg);color:var(--bull);border:1px solid var(--bull-bd)}
.bd-bear{background:var(--bear-bg);color:var(--bear);border:1px solid var(--bear-bd)}
.bd-neutral{background:var(--bg-elevated);color:var(--text-2);border:1px solid var(--border)}
.bd-L,.bd-IM{background:var(--bull-bg);color:var(--bull);border:1px solid var(--bull-bd)}
.bd-W{background:var(--neutral-bg);color:var(--neutral);border:1px solid var(--neutral-bd)}
.bd-LG{background:var(--bear-bg);color:var(--bear);border:1px solid var(--bear-bd)}
.bd-I,.bd-C{background:var(--info-bg);color:var(--info);border:1px solid var(--info-bd)}
/* GEX profile track */
.gex-track-wrap{min-width:260px;position:relative}
.gex-cap{font-family:var(--mono);font-size:10px;margin-bottom:3px}
.gex-cap.bull{color:var(--bull)}.gex-cap.bear{color:var(--bear)}
.gex-track{position:relative;height:8px;background:var(--bg-elevated);border-radius:4px;border:1px solid var(--border)}
.gex-track b{position:absolute;top:-2px;width:2px;height:12px;border-radius:1px}
.gex-track b.spot{background:var(--text);width:3px}
.gex-track b.flip{background:var(--neutral)}
.gex-track b.call{background:var(--bear)}
.gex-track b.put{background:var(--bull)}
.gex-track b.magnet{background:var(--quality)}
.gex-labels{position:relative;height:22px;margin-top:4px}
.gex-lbl{position:absolute;top:0;display:flex;flex-direction:column;align-items:center;line-height:1.1;white-space:nowrap;font-family:var(--mono);font-size:9px;color:var(--text-2)}
.gex-lbl .k{font-weight:700}
.gex-lbl .m{color:var(--text-3);font-size:8px}
.gex-lbl.put .k{color:var(--bull)}
.gex-lbl.magnet .k{color:var(--quality)}
.gex-lbl.spot .k{color:var(--text)}
.gex-lbl.flip .k{color:var(--neutral)}
.gex-lbl.call .k{color:var(--bear)}
/* ER sparkline */
.spark{display:flex;gap:5px;align-items:flex-end}
.spark-col{display:flex;flex-direction:column;align-items:center;gap:2px}
.spark-track{height:20px;display:flex;align-items:flex-end}
.spark-track span{width:10px;border-radius:1px 1px 0 0;display:block}
.spark .up{background:var(--bull)}.spark .dn{background:var(--bear)}
.spark-pct{font-family:var(--mono);font-size:8px;line-height:1}
.spark-pct.up{color:var(--bull)}.spark-pct.dn{color:var(--bear)}.spark-pct.na{color:var(--text-3)}
/* Score meter */
.score-meter{display:flex;align-items:center;gap:6px;justify-content:center}
.score-meter b{font-family:var(--mono);font-size:12px}
.pips{display:flex;gap:2px}
.pips i{width:5px;height:10px;border-radius:1px;background:var(--border);display:block}
.pips i.on.hi{background:var(--bull)}.pips i.on.mid{background:var(--neutral)}.pips i.on.lo{background:var(--text-3)}
/* Right-side drawer */
.drawer{width:0;background:var(--bg-primary);border-left:1px solid var(--border-subtle);overflow:hidden;transition:width .18s ease;flex:0 0 auto}
.drawer.open{width:var(--drawer-w)}
.drawer-in{width:var(--drawer-w);padding:14px 16px 28px;overflow:auto;height:100%;box-sizing:border-box}
.drawer-header{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;padding:0;margin:0 0 4px}
.drawer-title{color:var(--text);font-size:18px;font-weight:650;letter-spacing:.03em}
.drawer-close{background:var(--bg-elevated);border:1px solid var(--border);color:var(--text-2);width:28px;height:28px;border-radius:6px;cursor:pointer;font-size:14px}
.drawer-close:hover{color:var(--bear)}
.drawer-sub{color:var(--text-2);font-size:12px;margin-top:2px}
.dcards{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin:14px 0}
.dcard{background:var(--bg-secondary);border:1px solid var(--border-subtle);border-radius:10px;padding:10px 12px}
.dcard .dl{color:var(--text-3);font-size:10px;letter-spacing:.06em;text-transform:uppercase}
.dcard .dv{margin-top:4px;font-weight:650;font-size:13px}
.dcard .dv.sub{color:var(--text-3);font-size:11px;font-weight:400;margin-top:4px;line-height:1.35}
.rulecards{display:flex;flex-direction:column;gap:8px;margin-top:8px}
.rcard{display:grid;grid-template-columns:28px 1fr;gap:8px;align-items:start;background:none;border:0;padding:0}
.rcard .rname{font-family:var(--mono);font-size:11px;color:var(--text-3);padding-top:2px}
.rcard .bar{height:6px;background:var(--bg-elevated);border-radius:3px;overflow:hidden;margin-bottom:3px}
.rcard .bar i{display:block;height:100%;background:var(--info)}
.rcard .bar i.fire{background:var(--bull)}
.rcard .rrole{display:none}
.rcard .rwhy{margin:0;color:var(--text-2);font-size:12px;line-height:1.35}
.rcard.pos .bar i{background:var(--bull)}.rcard.neg .bar i{background:var(--bear)}
.decision-split{display:grid;grid-template-columns:200px 1fr;gap:10px;align-items:start}
.decision-context{color:var(--text-3);font-size:11px;line-height:1.6}
.decision-context .ra{color:var(--text-3);font-weight:600}
.drawer-panel{background:var(--bg-secondary);border:1px solid var(--border-subtle);border-radius:10px;padding:10px 12px;margin-top:10px;min-width:0}
.drawer-panel-title{color:var(--text-3);font-size:10px;font-weight:700;margin-bottom:8px;text-transform:uppercase;letter-spacing:.06em}
.drawer-toolbar{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.dperiod{margin-left:auto;display:flex;gap:4px}
.dp{background:var(--bg-secondary);color:var(--text-2);border:1px solid var(--border);padding:2px 7px;border-radius:5px;cursor:pointer;font-size:10px}
.dp.active{color:var(--neutral);border-color:var(--neutral-bd);background:var(--neutral-bg)}
.drawer-chart{width:100%;overflow-x:auto}
.drawer-metrics{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px;font-size:11px;color:var(--text-3)}
.drawer-metrics b{color:var(--text-2)}
.drawer-metrics .pos{color:var(--bull)}.drawer-metrics .neg{color:var(--bear)}
.col-hidden{display:none!important}
.comfort{--row-h:52px}
.gex-on{--row-h:64px}
.comfort.gex-on{--row-h:76px}
</style>"""

def _clip(s, n: int) -> str:
    """``s`` truncated to ``n`` characters (renderers truncate there)."""
    return str(s or "")[:n]


# Redesign column model. Every column is rendered server-side; the client hides
# columns by ``data-col`` for the active view (see ``_VIEWS``). Groups drive the
# header spans. Tuple: (id, label, group, align, width_px, cell_class, mode,
# char_px, measure) — mode "fixed" uses width_px as the width.
_COLUMNS = [
    ("datetime", "Date/Time", "identity", "left", 62, "td-g1", "text", 6.0, lambda c, cal: f"{c.earnings_date.strftime('%m/%d') if c.earnings_date else '—'} {c.report_time_label or '—'}"),
    ("tradeBy", "Trade By", "identity", "left", 46, "td-g1", "text", 5.6, lambda c, cal: "MM/DD"),
    ("ticker", "Ticker", "identity", "left", 50, "td-g1 sticky-tick", "text", 6.2, lambda c, cal: (c.ticker or "") + "⚠⛔"),
    ("name", "Name", "identity", "left", 114, "td-g1 sticky-name name-cell", "text", 5.6, lambda c, cal: _clip(c.name if c.name and c.name != c.ticker else "—", 18)),
    ("sector", "Sector", "identity", "left", 84, "td-g1", "text", 5.6, lambda c, cal: _clip(c.sector or "—", 20)),
    ("rotation", "Rotation", "identity", "left", 62, "td-g1", "text", 5.6, lambda c, cal: _clip(c.rotation_quadrant or "N/A", 9) + " ↑"),
    ("cov", "Cov", "decision", "right", 40, "td-g6", "text", 5.6, lambda c, cal: "100%"),
    ("bias", "Bias", "decision", "left", 72, "td-g6", "text", 5.6, lambda c, cal: c.bias_label or "NEUTRAL"),
    ("action", "Action", "decision", "left", 92, "td-g6", "fixed", 0, None),
    ("expMove", "Exp. Move", "earnings", "right", 50, "td-g2", "text", 5.6, lambda c, cal: f"{c.expected_move_pct or 0:.1f}%"),
    ("edge", "Edge", "earnings", "right", 92, "td-g2", "fixed", 0, None),
    ("surprises", "Return since print", "earnings", "left", 130, "td-g2", "fixed", 0, None),
    ("sinceEr", "Since ER", "earnings", "left", 86, "td-g2", "fixed", 0, None),
    ("price", "Price", "market", "right", 72, "td-g4 sticky-price", "text", 5.6, lambda c, cal: c.current_price or "$000.00"),
    ("gex", "GEX profile", "gex", "left", 280, "td-g4", "fixed", 0, None),
    ("liquidity", "Liquidity", "gex", "left", 88, "td-g4", "text", 5.6, lambda c, cal: c.liquidity_label or "Tradeable"),
    ("mcap", "MCap", "gex", "right", 72, "td-g4", "text", 5.6, lambda c, cal: c.market_cap or "$000.0B"),
    ("stkVol", "Stk Vol", "gex", "right", 72, "td-g4", "text", 5.6, lambda c, cal: c.stock_volume or "000.0M"),
    ("optVol", "Opt Vol", "gex", "right", 72, "td-g4", "text", 5.6, lambda c, cal: c.option_volume or "000.0M"),
    ("oi", "OI", "gex", "right", 72, "td-g4", "text", 5.6, lambda c, cal: c.total_open_interest or "000.0K"),
    ("score", "Score (0-9)", "hunter", "center", 64, "td-g5", "text", 5.6, lambda c, cal: "9/9"),
    ("acceleration", "Acceleration", "hunter", "center", 64, "td-g5", "text", 5.6, lambda c, cal: "Yes"),
    ("owners", "Owners (AG)", "hunter", "left", 100, "td-g5", "text", 5.6, lambda c, cal: "Accumulating DIL"),
]

_COL_IDS = [c[0] for c in _COLUMNS]
_COL_GROUPS = [c[2] for c in _COLUMNS]
_COL_WIDTHS = [c[4] for c in _COLUMNS]
_COL_ALIGN = {c[0]: c[3] for c in _COLUMNS}
_COL_CLASS = {c[0]: c[5] for c in _COLUMNS}

# Group order + labels for the header row.
_GROUP_ORDER = ["identity", "decision", "earnings", "market", "gex", "hunter"]
_GROUP_LABELS = {
    "identity": "Identity", "decision": "Decision", "earnings": "Earnings Setup",
    "market": "Market", "gex": "GEX & Liquidity", "hunter": "Asymmetric Growth Hunter",
}

# Per-view visible columns (gui-redesign/config/views.json).
_VIEWS = {
    "scan": ["ticker", "name", "price", "bias", "action", "score", "acceleration", "liquidity", "rotation", "gex"],
    "earnings": ["ticker", "name", "price", "bias", "action", "expMove", "edge", "surprises", "sinceEr", "score", "cov"],
    "gex": ["ticker", "name", "price", "gex", "liquidity", "optVol", "oi", "stkVol", "mcap", "acceleration"],
    "full": list(_COL_IDS),
}
_VIEW_LABELS = {"scan": "Scan", "earnings": "Earnings", "gex": "GEX", "full": "Full"}
_STICKY_PRICE_VIEWS = ["scan", "earnings", "gex"]


def _compute_col_widths(cards: list, cal_lookup: dict = None) -> list[int]:
    """Fit each column to the longest visible text across all rows (+header)."""
    cal_lookup = cal_lookup or {}
    widths = []
    for cid, label, _grp, _align, min_px, _cls, mode, char_px, measure in _COLUMNS:
        if mode == "fixed":
            widths.append(min_px)
            continue
        # Headers may wrap, so the header only needs room for its longest token.
        header_tokens = label.replace("(", " ").replace(")", " ").replace("/", " ").split()
        longest = max((len(t) for t in header_tokens), default=1)
        for c in cards or []:
            try:
                longest = max(longest, len(str(measure(c, cal_lookup))))
            except Exception:  # noqa: BLE001 — width fitting must never break a render
                continue
        widths.append(max(min_px, int(longest * char_px) + 8))
    return widths


def _colgroup(widths: list = None) -> str:
    ws = widths or _COL_WIDTHS
    return '<colgroup>' + ''.join(
        f'<col data-col="{cid}" style="width:{w}px">' for cid, w in zip(_COL_IDS, ws)
    ) + '</colgroup>'


DETAIL_JS = "<script>" + ESC_JS + """
var CUR_DETAIL = { ticker: null, period: 8 };
function fmtPct(v) { if (v === null || v === undefined) return 'N/A'; return (v >= 0 ? '+' : '') + v.toFixed(1) + '%'; }
function fmtDollar(v) { if (v === null || v === undefined) return 'N/A'; return (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2); }
function fmtMoney(v) { if (v === null || v === undefined) return '—'; var a=Math.abs(v), s = (v>=0?'+':'-'); if (a>=1e6) return s+'$'+(a/1e6).toFixed(2)+'M'; if (a>=1e3) return s+'$'+(a/1e3).toFixed(0)+'K'; return s+'$'+a.toFixed(0); }
function closeDetail() {
  CUR_DETAIL.ticker = null;
  document.querySelectorAll('tbody tr.sel').forEach(function(r){ r.classList.remove('sel'); });
  var d = document.getElementById('drawer');
  if (d) d.classList.remove('open');
  var di = document.getElementById('drawerIn');
  if (di) di.innerHTML = '';
}
function rowFor(ticker) {
  // Compare the attribute: a selector built from the ticker throws on a quote or a backslash in it.
  var rows = document.querySelectorAll('tbody tr[data-ticker]');
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].getAttribute('data-ticker') === ticker) return rows[i];
  }
  return null;
}
function selectRow(ticker) {
  document.querySelectorAll('tbody tr.sel').forEach(function(r){ r.classList.remove('sel'); });
  var row = rowFor(ticker);
  if (row) row.classList.add('sel');
}
function toggleDetail(ticker, row) {
  var open = CUR_DETAIL.ticker === ticker;
  closeDetail();
  if (open) return;
  CUR_DETAIL.ticker = ticker; CUR_DETAIL.period = 8;
  selectRow(ticker);
  var di = document.getElementById('drawerIn');
  if (di) di.innerHTML = buildDrawer(ticker);
  var d = document.getElementById('drawer');
  if (d) d.classList.add('open');
  renderDetail();
}
function tableColWidths() {
  // Measure the rendered table's column headers so any grid tracks the actual
  // (stretched) column widths; falls back to the embedded minimums.
  try {
    var t = document.querySelector('table.data-table');
    var row = t && t.tHead && t.tHead.rows[1];   // row 0 = group header, row 1 = columns
    if (row && row.cells && row.cells.length) {
      var ws = [];
      for (var i = 0; i < row.cells.length; i++) {
        var w = row.cells[i].getBoundingClientRect().width;
        if (w > 0) ws.push(Math.round(w));
      }
      if (ws.length) return ws;
    }
  } catch (e) {}
  return (typeof COL_WIDTHS !== 'undefined' ? COL_WIDTHS : []);
}
function drawerGridStyle() {
  var cols = tableColWidths().map(function(w){ return w + 'px'; }).join(' ');
  return 'display:grid;grid-template-columns:' + cols + ';gap:4px;align-items:start';
}
function buildDrawer(ticker) {
  var row = rowFor(ticker);
  var name = row ? (row.getAttribute('data-name') || '') : '';
  var priceEl = row ? row.querySelector('td.sticky-price') : null;
  var price = priceEl ? priceEl.textContent : '';
  var sess = row ? (row.getAttribute('data-am-pm') || '') : '';
  // The watch-list pair sits here, in the drawer, not in the table's 50 px sticky ticker column (PR 4).
  var pair = (typeof rcWatchButtons === 'function') ? rcWatchButtons(ticker) : '';
  return '<div class="drawer-header"><div><h3 class="drawer-title">' + rcEsc(ticker) + '</h3>' + pair
    + '<div class="drawer-sub">' + rcEsc(name) + ' · ' + rcEsc(price) + ' · ' + rcEsc(sess) + '</div></div>'
    + '<button class="drawer-close" onclick="closeDetail()">✕</button></div>'
    + '<div class="drawer-toolbar"><span class="dp-label">History:</span>'
    + '<span class="dperiod"><button class="dp" id="dp-4" onclick="setDetailPeriod(4)">4Q</button><button class="dp active" id="dp-8" onclick="setDetailPeriod(8)">8Q</button></span>'
    + '</div>'
    + '<div class="drawer-panel"><div class="drawer-panel-title">Decision — which rule produced this action</div><div class="decision-audit" id="dr-decision"></div></div>'
    + '<div class="drawer-panel"><div class="drawer-panel-title">Market structure</div><div id="dr-structure" class="decision-context"></div></div>'
    + '<div class="drawer-panel"><div class="drawer-panel-title">Charts</div>'
    + '<div class="drawer-chart" id="dr-rev-chart"></div><div class="drawer-metrics" id="dr-rev-metrics"></div>'
    + '<div class="drawer-chart" id="dr-eps-chart"></div><div class="drawer-metrics" id="dr-eps-metrics"></div>'
    + '<div class="drawer-chart" id="dr-price-chart"></div><div class="drawer-metrics" id="dr-price-metrics"></div>'
    + '</div>';
}
function getDetailQuarters() { var qs = DETAIL_DATA[CUR_DETAIL.ticker] || []; return qs.slice(-CUR_DETAIL.period); }
function setDetailPeriod(n) {
  CUR_DETAIL.period = n;
  var b4 = document.getElementById('dp-4'), b8 = document.getElementById('dp-8');
  if (b4) b4.classList.toggle('active', n === 4);
  if (b8) b8.classList.toggle('active', n === 8);
  renderDetail();
}
function renderDetail() {
  var qs = getDetailQuarters();
  var pc = document.getElementById('dr-price-chart'), pm = document.getElementById('dr-price-metrics');
  var ec = document.getElementById('dr-eps-chart'), em = document.getElementById('dr-eps-metrics');
  var rc = document.getElementById('dr-rev-chart'), rm = document.getElementById('dr-rev-metrics');
  if (pc) pc.innerHTML = renderPriceChart(qs);
  if (pm) pm.innerHTML = renderPriceMetrics(qs);
  if (ec) ec.innerHTML = renderEpsChart(qs);
  if (em) em.innerHTML = renderEpsMetrics(qs);
  if (rc) rc.innerHTML = renderRevenueChart(qs);
  if (rm) rm.innerHTML = renderRevenueMetrics(qs);
  renderDecision();
}
function renderDecision() {
  var el = document.getElementById('dr-decision');
  if (!el) return;
  var d = DECISION_DATA[CUR_DETAIL.ticker];
  if (!d) { el.innerHTML = '<span style="color:#484f58">no decision data</span>'; return; }
  function val(v, unit) { return (v === null || v === undefined) ? '—' : v + (unit || ''); }

  // Action color by meaning.
  var act = d.action || '—';
  var actColor = '#8b949e';
  if (act === 'NO_TRADE') actColor = '#f85149';
  else if (act.indexOf('OPTIONS_BULL') === 0 || act.indexOf('SHARES_BULL') === 0) actColor = '#3fb950';
  else if (act.indexOf('OPTIONS_BEAR') === 0 || act.indexOf('SHARES_BEAR') === 0) actColor = '#f85149';
  else if (act.indexOf('WATCH') === 0) actColor = '#d2991d';

  var bias = d.bias || '—';
  var biasColor = bias.indexOf('BULL') === 0 ? '#3fb950' : (bias.indexOf('BEAR') === 0 ? '#f85149' : '#8b949e');

  var conf = d.confidence;
  var confColor = conf >= 50 ? '#3fb950' : (conf >= 25 ? '#d2991d' : '#f85149');
  var confSub = (conf >= 50 ? 'Enough rules fired' : conf >= 25 ? 'Partial coverage' : 'Thin coverage') + ' — not a hit rate';

  var s = '<div class="dcards">'
    + '<div class="dcard"><div class="dl">Action</div><div class="dv" style="color:' + actColor + '">' + rcEsc(act) + '</div>'
    +   '<div class="dv sub">' + rcEsc(d.trade_read_reason || d.trade_read || '—') + '</div></div>'
    + '<div class="dcard"><div class="dl">Direction</div><div class="dv" style="color:' + biasColor + '">' + rcEsc(bias) + '</div>'
    +   '<div class="dv sub">score ' + val(fmtSigned(d.direction)) + '</div></div>'
    + '<div class="dcard"><div class="dl">Confidence</div><div class="dv" style="color:' + confColor + '">' + val(Math.round(conf), '%') + '</div>'
    +   '<div class="dv sub">' + confSub + '</div></div>'
    + '</div>';

  s += '<div class="decision-split"><div class="rulecards" id="dr-rules"></div>'
    + '<details class="radar-details"><summary>Show radar</summary><div class="ruleweb" id="dr-web"></div></details></div>';
  el.innerHTML = s;

  // Structure card — parsed context rows (regime / levels / flow / LEAPS).
  var st = document.getElementById('dr-structure');
  if (st) {
    var ctx = [];
    if (d.structure) ctx.push('<div class="struct-row"><b>Regime</b> <span>' + rcEsc(d.structure) + (d.structure_tip ? ' — ' + rcEsc(d.structure_tip) : '') + '</span></div>');
    // A failed flow request measured nothing: its label only, never a "$0 net" beside it.
    if (d.flow_source === 'failed') ctx.push('<div class="struct-row"><b>Flow</b> <span class="tone-neutral">'
        + rcEsc(d.inst_flow_label || 'Flow unavailable (provider failure)') + '</span></div>');
    else if (d.inst_flow_label) ctx.push('<div class="struct-row"><b>Flow</b> <span>' + rcEsc(d.inst_flow_label) + ' · ' + fmtMoney(d.inst_flow_net) + ' net'
        + (d.inst_flow_prints ? ' · ' + d.inst_flow_prints + ' prints' : '')
        + (d.inst_flow_span != null ? ' · ' + d.inst_flow_span + 'h' : '') + '</span></div>');
    if (d.leaps && d.leaps !== 'N/A') {
      var leapsTxt = rcEsc(d.leaps);
      if (d.leaps_as_of) leapsTxt += ' · as of ' + rcEsc(d.leaps_as_of)
          + (d.leaps_age_days != null ? ' (' + d.leaps_age_days + ' d old)' : '');
      ctx.push('<div class="struct-row"><b>LEAPS</b> <span>' + leapsTxt + '</span></div>');
    }
    var priceParts = [];
    if (d.past_1d) priceParts.push('1D ' + rcEsc(d.past_1d));
    if (d.past_1w) priceParts.push('1W ' + rcEsc(d.past_1w));
    if (d.past_2w) priceParts.push('2W ' + rcEsc(d.past_2w));
    if (d.return_4w) priceParts.push('4W ' + rcEsc(d.return_4w));
    if (priceParts.length) ctx.push('<div class="struct-row"><b>Price</b> <span>' + priceParts.join(' · ') + '</span></div>');
    if (d.last_er_label) ctx.push('<div class="struct-row"><b>Last ER</b> <span>' + rcEsc(d.last_er_label) + '</span></div>');
    st.innerHTML = ctx.length ? ctx.join('') : '<span style="color:#484f58">No structure data.</span>';
  }
  renderRuleWeb();
  renderRuleList();
}
function renderRuleList() {
  var el = document.getElementById('dr-rules');
  if (!el) return;
  var d = DECISION_DATA[CUR_DETAIL.ticker] || {};
  var s = '';
  (d.rules || []).forEach(function(r){
    var kind = 'tag';
    var score = r.score;
    if (score !== null && score !== undefined) kind = (score > 0 ? 'pos' : (score < 0 ? 'neg' : 'tag'));
    else if (r.role && r.role.indexOf('direction') === 0) kind = 'tag';
    var why = (r.reasons && r.reasons.length) ? r.reasons[0] : '—';
    var mark = '';
    if (kind === 'pos') mark = '<span class="ok">✓</span> ';
    else if (kind === 'neg') mark = '<span class="no">✗</span> ';
    s += '<div class="rcard ' + kind + '">'
      + '<span class="rname">' + rcEsc(r.r) + '</span>'
      + '<span class="rrole">' + rcEsc(r.role || '') + '</span>'
      + '<span class="rwhy">' + mark + rcEsc(why) + '</span>'
      + '</div>';
  });
  el.innerHTML = s;
}
function renderRuleWeb() {
  var el = document.getElementById('dr-web');
  if (!el) return;
  var d = DECISION_DATA[CUR_DETAIL.ticker] || {};
  var order = ['R2', 'R3', 'R4', 'R5', 'R6', 'R7', 'R8'];
  var byName = {};
  (d.rules || []).forEach(function(r){ byName[r.r] = r; });
  var axes = order.filter(function(k){ return byName[k]; });
  if (!axes.length) { el.innerHTML = '<span style="color:#484f58">no rule data</span>'; return; }
  var W = 200, H = 200, cx = 100, cy = 100, R = 68, n = axes.length;
  function valueOf(k) {
    if (k === 'R5') return 0;                                    // tag: never a vote
    if (k === 'R6') return (d.action === 'NO_TRADE') ? -1 : 0;   // veto-only
    var sc = byName[k].score;
    if (sc === null || sc === undefined) return 0;
    return Math.max(-1, Math.min(1, sc / 100));
  }
  function xy(i, v) {
    var a = -Math.PI / 2 + i * 2 * Math.PI / n;
    var rr = R * (0.5 + 0.5 * v);   // center = -100, middle ring = 0, outer = +100
    return [cx + rr * Math.cos(a), cy + rr * Math.sin(a)];
  }
  var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:200px;height:auto;display:block">';
  [0.25, 0.5, 0.75, 1].forEach(function(f){
    s += '<circle cx="' + cx + '" cy="' + cy + '" r="' + (R * f).toFixed(1) + '" fill="none" stroke="' + (f === 0.5 ? '#3b4252' : '#21262d') + '" stroke-width="' + (f === 0.5 ? '1.2' : '0.8') + '"/>';
  });
  axes.forEach(function(k, i){
    var p = xy(i, 1);
    s += '<line x1="' + cx + '" y1="' + cy + '" x2="' + p[0].toFixed(1) + '" y2="' + p[1].toFixed(1) + '" stroke="#21262d" stroke-width="0.8"/>';
  });
  var poly = [];
  axes.forEach(function(k, i){ var p = xy(i, valueOf(k)); poly.push(p[0].toFixed(1) + ',' + p[1].toFixed(1)); });
  s += '<polygon points="' + poly.join(' ') + '" fill="rgba(88,166,255,0.18)" stroke="#58a6ff" stroke-width="1.2"/>';
  axes.forEach(function(k, i){
    var v = valueOf(k), p = xy(i, v);
    var col = v > 0 ? '#3fb950' : (v < 0 ? '#f85149' : '#8b949e');
    var lbl = k + ' ' + (k === 'R5' ? 'tag' : (k === 'R6' && d.action === 'NO_TRADE' ? 'veto' : fmtSigned(byName[k].score || 0)));
    s += '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) + '" r="2.5" fill="' + col + '"><title>' + lbl + '</title></circle>';
    var lp = xy(i, 1.16);
    var anchor = 'middle';
    if (lp[0] < cx - 8) anchor = 'end'; else if (lp[0] > cx + 8) anchor = 'start';
    s += '<text x="' + lp[0].toFixed(1) + '" y="' + (lp[1] + 3).toFixed(1) + '" fill="#8b949e" font-size="7" text-anchor="' + anchor + '">' + lbl + '</text>';
  });
  s += '</svg><div style="font-size:8px;color:#6e7681;margin-top:2px">center = negative · middle ring = neutral · outer = positive</div>';
  el.innerHTML = s;
}
function fmtSigned(v) {
  if (v === null || v === undefined) return '—';
  return (v >= 0 ? '+' : '') + v.toFixed(0);
}
function niceStep(raw) {
  if (!(raw > 0)) return 1;
  var mag = Math.pow(10, Math.floor(Math.log(raw)/Math.LN10));
  var n = raw / mag;
  var s = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
  return s;
}
function symRange(minV, maxV, padFrac) {
  var m = Math.max(Math.abs(minV), Math.abs(maxV));
  if (!(m > 0)) { m = 1; }
  var span = m * (1 + (padFrac == null ? 0.15 : padFrac));
  return { lo: -span, hi: span };
}
function yAxisLabels(lo, hi, n) {
  var step = niceStep((hi - lo) / (n || 5));
  var out = [];
  for (var v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) {
    out.push(v);
  }
  return out;
}
function drawYAxis(s, lo, hi, mL, mT, plotH, W, fmt, n) {
  var labels = yAxisLabels(lo, hi, n || 5);
  labels.forEach(function(v){
    var yy = mT + (1 - (v - lo) / (hi - lo)) * plotH;
    s += '<line x1="' + mL + '" y1="' + yy + '" x2="' + (W-16) + '" y2="' + yy + '" stroke="#1c2128" stroke-width="1"/>';
    s += '<text x="' + (mL-6) + '" y="' + (yy+3) + '" fill="#6e7681" font-size="6" text-anchor="end">' + fmt(v) + '</text>';
  });
  return s;
}
function fmtRev(v) {
  if (v === null || v === undefined) return 'N/A';
  var a = Math.abs(v);
  if (a >= 1e9) return '$' + (v/1e9).toFixed(2) + 'B';
  if (a >= 1e6) return '$' + (v/1e6).toFixed(1) + 'M';
  if (a >= 1e3) return '$' + (v/1e3).toFixed(1) + 'K';
  return '$' + v.toFixed(0);
}
function fmtEps(v) { return (v>=0?'':'') + v.toFixed(2); }
function fmtPctAxis(v) { return (v>0?'+':'') + v.toFixed(0) + '%'; }
function renderPriceChart(qs) {
  var W = 360, H = 120, mL = 34, mR = 10, mT = 10, mB = 16, n = qs.length;
  if (!n) return '<span style="color:#484f58">No completed earnings history.</span>';
  var plotW = W - mL - mR, plotH = H - mT - mB, step = plotW / n;
  var maxAbs = 1;
  qs.forEach(function(q){ if (q.implied != null) maxAbs = Math.max(maxAbs, Math.abs(q.implied)); if (q.actual != null) maxAbs = Math.max(maxAbs, Math.abs(q.actual)); });
  maxAbs *= 1.15;
  function y(v) { return mT + plotH/2 - (v / maxAbs) * (plotH/2); }
  var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block;font-family:Segoe UI,Consolas,monospace">';
  // Symmetric % y-axis about 0 (both directions, same level).
  s = drawYAxis(s, -maxAbs, maxAbs, mL, mT, plotH, W, fmtPctAxis);
  s += '<line x1="' + mL + '" y1="' + y(0) + '" x2="' + (W-mR) + '" y2="' + y(0) + '" stroke="#2b2f3a" stroke-width="1"/>';
  qs.forEach(function(q, i) {
    var x = mL + (i + 0.5) * step;
    if (q.implied != null) {
      var yTop = y(q.implied), yBot = y(-q.implied);
      s += '<rect x="' + (x-7) + '" y="' + yTop + '" width="14" height="' + (yBot-yTop) + '" rx="7" fill="rgba(139,148,158,0.16)" stroke="#6e7681" stroke-width="1" stroke-dasharray="3,3"><title>' + rcEsc(q.q) + ' (' + rcEsc(q.date) + ') — implied ±' + q.implied.toFixed(1) + '%</title></rect>';
    }
    if (q.actual != null) {
      var col = q.actual > 0 ? '#3fb950' : (q.actual < 0 ? '#f85149' : '#8b949e');
      var over = q.implied != null && Math.abs(q.actual) > Math.abs(q.implied);
      var tip = rcEsc(q.q) + ' (' + rcEsc(q.date) + ') — actual ' + fmtPct(q.actual) + (q.implied != null ? ', implied ±' + q.implied.toFixed(1) + '%' : ', implied N/A');
      s += '<circle cx="' + x + '" cy="' + y(q.actual) + '" r="3" fill="' + col + '"><title>' + tip + '</title></circle>';
      if (over) s += '<circle cx="' + x + '" cy="' + y(q.actual) + '" r="5" fill="none" stroke="' + col + '" stroke-width="1.4"><title>actual exceeded implied</title></circle>';
    }
    s += '<text x="' + x + '" y="' + (H-10) + '" fill="#8b949e" font-size="8" text-anchor="middle">' + rcEsc(q.q) + '</text>';
    if (q.implied == null) s += '<text x="' + x + '" y="' + (mT+8) + '" fill="#6e7681" font-size="8" text-anchor="middle">imp. N/A</text>';
  });
  s += '<text x="10" y="' + (mT-6) + '" fill="#8b949e" font-size="8">1D move (%)</text>';
  s += '</svg>';
  return s;
}
function renderEpsChart(qs) {
  var W = 360, H = 120, mL = 34, mR = 10, mT = 10, mB = 16, n = qs.length;
  if (!n) return '<span style="color:#484f58">No completed earnings history.</span>';
  var plotW = W - mL - mR, plotH = H - mT - mB, step = plotW / n;
  var minV = Infinity, maxV = -Infinity;
  qs.forEach(function(q){ if (q.eps_actual != null) { minV = Math.min(minV, q.eps_actual); maxV = Math.max(maxV, q.eps_actual); } if (q.eps_est != null) { minV = Math.min(minV, q.eps_est); maxV = Math.max(maxV, q.eps_est); } });
  if (minV === Infinity) { minV = 0; maxV = 1; }
  // Symmetric about 0 when both signs appear; otherwise a padded min/max.
  var rng = (minV < 0 && maxV > 0) ? symRange(minV, maxV, 0.15)
            : { lo: minV - (maxV-minV)*0.15, hi: maxV + (maxV-minV)*0.15 };
  if (rng.hi - rng.lo < 1e-9) { rng = { lo: -1, hi: 1 }; }
  function y(v) { return mT + (1 - (v - rng.lo) / (rng.hi - rng.lo)) * plotH; }
  var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block;font-family:Segoe UI,Consolas,monospace">';
  s = drawYAxis(s, rng.lo, rng.hi, mL, mT, plotH, W, fmtEps);
  if (rng.lo < 0 && rng.hi > 0) s += '<line x1="' + mL + '" y1="' + y(0) + '" x2="' + (W-mR) + '" y2="' + y(0) + '" stroke="#2b2f3a" stroke-width="1"/>';
  qs.forEach(function(q, i) {
    var x = mL + (i + 0.5) * step;
    if (q.eps_est != null) s += '<line x1="' + (x-10) + '" y1="' + y(q.eps_est) + '" x2="' + (x+10) + '" y2="' + y(q.eps_est) + '" stroke="#6e7681" stroke-width="2.5"><title>' + rcEsc(q.q) + ' consensus $' + q.eps_est.toFixed(2) + '</title></line>';
    if (q.eps_actual != null) {
      var col = q.eps_est != null ? (q.eps_actual > q.eps_est ? '#3fb950' : (q.eps_actual < q.eps_est ? '#f85149' : '#8b949e')) : '#8b949e';
      var tip = rcEsc(q.q) + ' (' + rcEsc(q.date) + ') — actual $' + q.eps_actual.toFixed(2) + (q.eps_est != null ? ', est $' + q.eps_est.toFixed(2) + ', surprise ' + fmtDollar(q.surprise_dollar) + (q.surprise_pct != null ? ' (' + fmtPct(q.surprise_pct) + ')' : '') : ', est N/A');
      s += '<circle cx="' + x + '" cy="' + y(q.eps_actual) + '" r="3" fill="' + col + '"><title>' + tip + '</title></circle>';
    }
    s += '<text x="' + x + '" y="' + (H-10) + '" fill="#8b949e" font-size="8" text-anchor="middle">' + rcEsc(q.q) + '</text>';
  });
  s += '<text x="10" y="' + (mT-6) + '" fill="#8b949e" font-size="8">EPS ($)</text>';
  s += '</svg>';
  return s;
}
function renderPriceMetrics(qs) {
  var a = qs.filter(function(q){ return q.actual != null; });
  var im = qs.filter(function(q){ return q.implied != null; });
  var both = qs.filter(function(q){ return q.actual != null && q.implied != null; });
  var over = both.filter(function(q){ return Math.abs(q.actual) > Math.abs(q.implied); });
  var avgA = a.length ? (a.reduce(function(s,q){return s+Math.abs(q.actual);},0)/a.length) : null;
  var avgI = im.length ? (im.reduce(function(s,q){return s+q.implied;},0)/im.length) : null;
  var overRate = both.length >= 2 ? (over.length/both.length*100).toFixed(0)+'%' : 'Insufficient history';
  return '<span><b>Avg actual move:</b> ' + (avgA != null ? avgA.toFixed(1)+'%' : 'N/A') + '</span>'
    + '<span><b>Avg implied move:</b> ' + (avgI != null ? avgI.toFixed(1)+'%' : 'N/A') + '</span>'
    + '<span><b>Over-implied rate:</b> ' + overRate + '</span>';
}
function renderEpsMetrics(qs) {
  var both = qs.filter(function(q){ return q.eps_actual != null && q.eps_est != null; });
  var beats = both.filter(function(q){ return q.eps_actual > q.eps_est; });
  var surps = qs.filter(function(q){ return q.surprise_pct != null; });
  var beatRate = both.length ? (beats.length/both.length*100).toFixed(0)+'%' : 'N/A';
  var avgSurp = surps.length ? (surps.reduce(function(s,q){return s+q.surprise_pct;},0)/surps.length) : null;
  var latest = null;
  for (var i=qs.length-1;i>=0;i--){ if (qs[i].eps_est != null) { latest = qs[i].eps_est; break; } }
  return '<span><b>EPS beat rate:</b> ' + beatRate + '</span>'
    + '<span><b>Avg EPS surprise:</b> ' + (avgSurp != null ? fmtPct(avgSurp) : 'N/A') + '</span>'
    + '<span><b>Latest consensus:</b> ' + (latest != null ? '$'+latest.toFixed(2) : 'N/A') + '</span>';
}
function renderRevenueChart(qs) {
  var W = 360, H = 120, mL = 40, mR = 10, mT = 10, mB = 16, n = qs.length;
  var revs = qs.filter(function(q){ return q.revenue_actual != null || q.revenue_est != null; });
  if (!revs.length) return '<span style="color:#484f58">No revenue data available.</span>';
  var plotW = W - mL - mR, plotH = H - mT - mB, step = plotW / n;
  var maxV = 1;
  revs.forEach(function(q){ if (q.revenue_actual != null) maxV = Math.max(maxV, q.revenue_actual); if (q.revenue_est != null) maxV = Math.max(maxV, q.revenue_est); });
  maxV *= 1.1;
  function y(v) { return mT + plotH - (v / maxV) * plotH; }
  var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block;font-family:Segoe UI,Consolas,monospace">';
  s = drawYAxis(s, 0, maxV, mL, mT, plotH, W, fmtRev, 5);
  qs.forEach(function(q, i) {
    var x = mL + (i + 0.5) * step;
    if (q.revenue_est != null) {
      var ey = y(q.revenue_est);
      s += '<line x1="' + (x-11) + '" y1="' + ey + '" x2="' + (x+11) + '" y2="' + ey + '" stroke="#d2991d" stroke-width="2"><title>' + rcEsc(q.q) + ' (' + rcEsc(q.date) + ') — est ' + fmtRev(q.revenue_est) + '</title></line>';
    }
    if (q.revenue_actual != null) {
      var h = (q.revenue_actual / maxV) * plotH;
      var yTop = y(q.revenue_actual);
      s += '<rect x="' + (x-10) + '" y="' + yTop + '" width="20" height="' + h + '" rx="3" fill="rgba(88,166,255,0.55)" stroke="#58a6ff" stroke-width="1"><title>' + rcEsc(q.q) + ' (' + rcEsc(q.date) + ') — revenue ' + fmtRev(q.revenue_actual) + '</title></rect>';
      s += '<text x="' + x + '" y="' + (yTop-4) + '" fill="#8b949e" font-size="7" text-anchor="middle">' + fmtRev(q.revenue_actual) + '</text>';
    } else if (q.revenue_est == null) {
      s += '<text x="' + x + '" y="' + (mT+plotH/2) + '" fill="#6e7681" font-size="8" text-anchor="middle">N/A</text>';
    }
    s += '<text x="' + x + '" y="' + (H-10) + '" fill="#8b949e" font-size="8" text-anchor="middle">' + rcEsc(q.q) + '</text>';
  });
  s += '<text x="12" y="' + (mT-6) + '" fill="#8b949e" font-size="8">Revenue ($)</text>';
  s += '</svg>';
  return s;
}
function renderRevenueMetrics(qs) {
  var revs = qs.filter(function(q){ return q.revenue_actual != null || q.revenue_est != null; });
  if (!revs.length) return '<span style="color:#484f58">Actual revenue only — no consensus data available.</span>';
  var latest = revs[revs.length-1];
  var prev = revs.length >= 2 ? revs[revs.length-2] : null;
  var yoy = null;
  if (prev && prev.revenue_actual) { yoy = (latest.revenue_actual / prev.revenue_actual - 1) * 100; }
  var anyEst = revs.some(function(q){ return q.revenue_est != null; });
  var est = latest.revenue_est != null ? fmtRev(latest.revenue_est)
            : (anyEst ? 'some quarters' : 'N/A');
  return '<span><b>Latest revenue:</b> ' + fmtRev(latest.revenue_actual) + '</span>'
    + '<span><b>Growth vs prev Q:</b> ' + (yoy != null ? fmtPct(yoy) : 'N/A') + '</span>'
    + '<span><b>Consensus:</b> ' + est + (anyEst ? '' : ' (Advanced+ feeds est)') + '</span>';
}
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('tbody').forEach(function(tbody) {
    tbody.querySelectorAll('tr[data-ticker]').forEach(function(row, i) {
      row.setAttribute('data-idx', i);
      row.style.cursor = 'pointer';
      row.addEventListener('click', function(e) {
        if (e.target.closest('.drawer')) return;
        toggleDetail(row.getAttribute('data-ticker'), row);
      });
    });
  });
  applyView('scan');
  startClock();
  window.addEventListener('resize', applySticky);
});

var STATE = { view: 'scan', density: 'compact', q: '', filters: {} };
var RCF = null, FIELDS = {};

function visibleRows() {
  return Array.prototype.filter.call(document.querySelectorAll('tbody tr[data-ticker]'), function(r){
    return r.style.display !== 'none';
  });
}
function toggleFilter(btn) {
  var f = btn.getAttribute('data-f');
  STATE.filters[f] = !STATE.filters[f];
  btn.classList.toggle('active', !!STATE.filters[f]);
  closeDetail();
  applyFilters();
}
function applyFilters() {
  var q = (STATE.q || '').toLowerCase();
  var kitOn = !!(RCF && RCF.active());
  var chipOn = false;
  for (var k in STATE.filters) { if (STATE.filters[k]) chipOn = true; }
  var filtering = kitOn || chipOn || !!q;
  var shown = 0, total = 0;
  var sections = document.querySelectorAll('.section');
  for (var s = 0; s < sections.length; s++) {
    var sec = sections[s];
    var rows = sec.querySelectorAll('tbody tr[data-ticker]');
    if (!rows.length) continue;
    var tShown = 0;
    for (var j = 0; j < rows.length; j++) {
      var row = rows[j];
      var show = true;
      var hay = (row.getAttribute('data-ticker') + ' ' + (row.getAttribute('data-name') || '')).toLowerCase();
      if (q && hay.indexOf(q) === -1) show = false;
      if (STATE.filters.score6 && !(parseInt(row.getAttribute('data-score'), 10) >= 6)) show = false;
      if (STATE.filters.accel && row.getAttribute('data-accel') !== '1') show = false;
      if (STATE.filters.bull && row.getAttribute('data-bias').indexOf('BULL') !== 0) show = false;
      if (STATE.filters.liq && row.getAttribute('data-liq') !== '1') show = false;
      if (STATE.filters.wl && row.getAttribute('data-wl') !== '1') show = false;
      if (kitOn) {
        var f = FIELDS[row.getAttribute('data-ticker')];
        if (!f || !RCF.pass(f)) show = false;
      }
      row.style.display = show ? '' : 'none';
      if (show) tShown++;
    }
    var n = sec.querySelector('.n');
    if (n) n.textContent = filtering ? (tShown + '/' + rows.length) : ('(' + rows.length + ')');
    shown += tShown;
    total += rows.length;
  }
  var line = document.getElementById('kit-count');
  if (line) line.textContent = kitOn ? (shown + ' of ' + total + ' names') : '';
}
function initKit() {
  FIELDS = {};
  for (var i = 0; i < DATA.length; i++) FIELDS[DATA[i].ticker] = DATA[i];
  RCF = RCFilters.mount({root: '#rc-filters', rows: DATA, meta: META, onChange: applyFilters});
  applyFilters();
}
function setView(v) { applyView(v); }
function applyColumnVisibility(vis) {
  document.querySelectorAll('th[data-col],td[data-col],col[data-col]').forEach(function(el){
    var on = !vis || vis.indexOf(el.getAttribute('data-col')) >= 0;
    el.classList.toggle('col-hidden', !on);
  });
  // Recompute group-header colspans from the visible columns.
  document.querySelectorAll('table.data-table').forEach(function(table){
    var counts = {};
    table.querySelectorAll('thead tr:nth-child(2) th[data-col]').forEach(function(th){
      if (!th.classList.contains('col-hidden')) {
        var g = th.getAttribute('data-group');
        counts[g] = (counts[g] || 0) + 1;
      }
    });
    table.querySelectorAll('thead tr:nth-child(1) th[data-group]').forEach(function(th){
      var g = th.getAttribute('data-group');
      var n = counts[g] || 0;
      th.colSpan = n;
      th.classList.toggle('col-hidden', n === 0);
    });
  });
}
function applyView(v) {
  STATE.view = v;
  var vis = (typeof VIEWS !== 'undefined' && VIEWS[v]) ? VIEWS[v] : null;
  applyColumnVisibility(vis);
  document.querySelectorAll('#views button').forEach(function(b){ b.classList.toggle('on', b.getAttribute('data-view') === v); });
  var pick = document.getElementById('colpick');
  if (pick) {
    pick.style.display = (v === 'full') ? '' : 'none';
    if (vis) pick.querySelectorAll('input[data-col]').forEach(function(i){ i.checked = vis.indexOf(i.getAttribute('data-col')) >= 0; });
  }
  var stick = (typeof STICKY_PRICE_VIEWS !== 'undefined') ? STICKY_PRICE_VIEWS.indexOf(v) >= 0 : true;
  document.querySelectorAll('.sticky-price').forEach(function(el){ el.classList.toggle('sticky-off', !stick); });
  var app = document.getElementById('app');
  if (app) app.classList.toggle('gex-on', !!(vis && vis.indexOf('gex') >= 0));
  applySticky();
}
function onColToggle() {
  var set = [];
  document.querySelectorAll('#colpick input[data-col]').forEach(function(i){ if (i.checked) set.push(i.getAttribute('data-col')); });
  applyColumnVisibility(set);
  applySticky();
}
function applySticky() {
  function w(cid) {
    var el = document.querySelector('table.data-table thead tr:nth-child(2) th[data-col="' + cid + '"]');
    return el ? el.getBoundingClientRect().width : 0;
  }
  var root = document.documentElement.style;
  var t = w('ticker'), n = w('name');
  if (t) root.setProperty('--stick-ticker', Math.round(t) + 'px');
  if (t && n) root.setProperty('--stick-name', Math.round(t + n) + 'px');
}
function setDensity(d) {
  STATE.density = d;
  var app = document.getElementById('app');
  if (app) app.classList.toggle('comfort', d === 'comfort');
  document.querySelectorAll('#density button').forEach(function(b){ b.classList.toggle('on', b.getAttribute('data-d') === d); });
}
function startClock() {
  var el = document.getElementById('clock');
  function tick() {
    if (!el) return;
    var d = new Date(), tz = 'local';
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'local'; } catch (e) {}
    var hh = String(d.getHours()).padStart(2, '0'), mm = String(d.getMinutes()).padStart(2, '0');
    el.textContent = 'as of ' + hh + ':' + mm + ' ' + tz;
  }
  tick();
  setInterval(tick, 30000);
}
var SORT_STATE = {};
function sortColumn(key, th) {
  closeDetail();
  var tbody = th.closest('table').querySelector('tbody');
  var rows = Array.from(tbody.querySelectorAll('tr[data-ticker]'));
  var state = SORT_STATE[key] || 'none';
  var next = state === 'desc' ? 'asc' : (state === 'asc' ? 'none' : 'desc');
  SORT_STATE = {}; SORT_STATE[key] = next;
  tbody.closest('table').querySelectorAll('th[data-sort-key]').forEach(function(h){ h.removeAttribute('data-dir'); });
  if (next !== 'none') th.setAttribute('data-dir', next);
  rows.sort(function(a, b) {
    if (next === 'none') return parseInt(a.getAttribute('data-idx')) - parseInt(b.getAttribute('data-idx'));
    var av = a.getAttribute('data-sort-' + key), bv = b.getAttribute('data-sort-' + key);
    var an = (av === null || av === '') ? null : (isNaN(parseFloat(av)) ? av : parseFloat(av));
    var bn = (bv === null || bv === '') ? null : (isNaN(parseFloat(bv)) ? bv : parseFloat(bv));
    if (an === null && bn === null) return 0;
    if (an === null) return 1;
    if (bn === null) return -1;
    var cmp;
    if (typeof an === 'number' && typeof bn === 'number') cmp = an - bn;
    else cmp = String(an).localeCompare(String(bn));
    return next === 'desc' ? -cmp : cmp;
  });
  rows.forEach(function(r) { tbody.appendChild(r); });
}
function sortByAttention() {
  closeDetail();
  document.querySelectorAll('tbody').forEach(function(tbody) {
    var rows = Array.from(tbody.querySelectorAll('tr[data-ticker]'));
    rows.sort(function(a, b) { return parseFloat(b.getAttribute('data-attn')) - parseFloat(a.getAttribute('data-attn')); });
    rows.forEach(function(r) { tbody.appendChild(r); });
  });
}
document.addEventListener('input', function(e){
  if (e.target && e.target.id === 'q') { STATE.q = e.target.value; applyFilters(); }
});
window.addEventListener('keydown', function(e) {
  if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) return;
  var vis = visibleRows();
  var i = vis.findIndex(function(r){ return r.getAttribute('data-ticker') === CUR_DETAIL.ticker; });
  if (e.key === 'j') { var nx = vis[Math.min(vis.length - 1, i + 1)]; if (nx) toggleDetail(nx.getAttribute('data-ticker'), nx); }
  else if (e.key === 'k') { var pv = vis[Math.max(0, i - 1)]; if (pv) toggleDetail(pv.getAttribute('data-ticker'), pv); }
  else if (e.key === 'Escape') { closeDetail(); }
  else if (e.key === 'Enter') { if (!CUR_DETAIL.ticker && vis[0]) toggleDetail(vis[0].getAttribute('data-ticker'), vis[0]); }
  else if (['1', '2', '3', '4'].indexOf(e.key) >= 0) { setView(['scan', 'earnings', 'gex', 'full'][parseInt(e.key, 10) - 1]); }
  else if (e.key === '/') { e.preventDefault(); var q = document.getElementById('q'); if (q) q.focus(); }
});
</script>"""


TIER_DESC = {
    "Tier A — Revenue Catalyst": "NRC &ge; 70 — <em>Strongest revenue signal.</em> Clear momentum with quality backing and consistent beats.",
    "Tier B — Watchlist": "NRC 55&ndash;69 — <em>Solid indicators.</em> Good growth, decent quality. Track for breakout.",
    "Tier C — Emerging": "NRC 40&ndash;54 — <em>Mixed signals.</em> Monitor for improvement in revisions or flow.",
    "Ignore": "NRC &lt; 40 — <em>No clear revenue catalyst.</em> May have valid non-revenue trade thesis.",
}


def _leaps_age_days(as_of: str, gen_date: date) -> int | None:
    """Whole days between a LEAPS snapshot's ``as_of`` and the page's generation date.

    ``None`` when ``as_of`` is empty or unparseable, so the drawer shows the label alone (C26).
    """
    if not as_of:
        return None
    try:
        return (gen_date - date.fromisoformat(str(as_of)[:10])).days
    except (ValueError, TypeError):
        return None


def _pc_color(val: str) -> str:
    if not val or val == "—": return "neut"
    if val.startswith("+"): return "bull"
    if val.startswith("-"): return "bear"
    return "neut"


def _rule_cell(score: float, weight: float) -> str:
    if score == 0:
        return '<span class="rule-score rule-zero">—</span>'
    cls = "rule-pos" if score > 0 else "rule-neg"
    return f'<span class="rule-score {cls}">{score:+.0f}</span>'


def _badge(tone: str, label: str, extra: str = "", icon: str = "", title: str = "") -> str:
    """Single status-badge component (tone + optional icon + text label)."""
    tip = f' title="{esc(title)}"' if title else ""
    ic = f"{icon} " if icon else ""
    cls = f"badge tone-{tone} {extra}".strip()
    return f'<span class="{cls}"{tip}>{ic}{esc(label)}</span>'


def _quadrant_badge(quadrant: str) -> str:
    if not quadrant:
        return '<span class="badge bd-I tone-info">N/A</span>'
    lead = quadrant in ("LEADING", "IMPROVING")
    arrow = "↑" if lead else "↓"
    cls_map = {"LEADING": "bd-L", "IMPROVING": "bd-IM", "WEAKENING": "bd-W", "LAGGING": "bd-LG"}
    cls = cls_map.get(quadrant, "bd-I")
    tone = "bull" if lead else ("neutral" if quadrant == "WEAKENING" else "bear")
    label = quadrant if quadrant == "IMPROVING" else (quadrant[:4] if quadrant == "LEADING" else quadrant[:7])
    tip = "Sector/theme relative strength quadrant (SPY-relative RS)."
    return f'<span class="badge {cls} tone-{tone}" title="{tip}">{esc(label)} {arrow}</span>'


def _conf_cell(confidence: float) -> str:
    """Render coverage % (not hit rate) as a meter."""
    if confidence >= 50:
        c = "#3fb950"
    elif confidence >= 25:
        c = "#d2991d"
    else:
        c = "#f85149"
    tip = ("Coverage = the weight of the direction rules that returned data (each × its own coverage) ÷ the weight of "
           "ALL four direction rules, × (1 − disagreement). A missing rule counts as 0 and lowers it. "
           "NOT a hit rate. −20% only when R3 and R7 oppose.")
    pct = max(0.0, min(100.0, confidence))
    return (f'<span class="num" style="display:inline-block;min-width:34px">'
            f'<span style="font-size:10px;font-weight:600;color:{c}" title="{tip}">{confidence:.0f}%</span>'
            f'<span style="display:block;height:3px;background:var(--bg-elevated);border-radius:2px;margin-top:2px">'
            f'<span style="display:block;height:3px;width:{pct:.0f}%;background:{c};border-radius:2px"></span></span>'
            f'</span>')


def _bias_cell(bias: str, direction: float) -> str:
    if bias.startswith("BULL"):
        cls, tone = "bd-bull", "bull"
    elif bias.startswith("BEAR"):
        cls, tone = "bd-bear", "bear"
    else:
        cls, tone = "bd-neutral", "neutral"
    tip = "Direction lean from the R2/R3/R7 + R8 votes (R5 does not vote)."
    return f'<span class="badge {cls} tone-{tone}" title="{tip}">{esc(bias)}</span>'


def _agh_points_cell(points) -> str:
    if points is None:
        return '<span class="tw-na" title="No Asymmetric Growth scan in ticker_scores">—</span>'
    try:
        n = int(points)
    except (TypeError, ValueError):
        return '<span class="tw-na">—</span>'
    color = "#3fb950" if n >= 7 else ("#d2991d" if n >= 5 else "#8b949e")
    cls = "hi" if n >= 6 else ("mid" if n >= 3 else "lo")
    pips = "".join(f'<i class="{"on " + cls if i < n else ""}"></i>' for i in range(9))
    return (f'<span class="score-meter" title="Asymmetric Growth Hunter 0–9">'
            f'<b style="color:{color}">{n}/9</b><span class="pips">{pips}</span></span>')


def _agh_accel_cell(accel) -> str:
    if accel is True:
        return _badge("bull", "Yes", icon="▲", title="Revenue growth accelerating YoY")
    if accel is False:
        return _badge("bear", "No", icon="▼", title="Revenue growth not accelerating")
    return '<span class="tw-na" title="No AGH accel flag">—</span>'


def _er_spark_cell(card) -> str:
    """Four bars for -4E..-1E with the % printed under each bar.

    Each value is the return from that print's reaction-session close to today's close
    (pipeline._compute_earnings_reactions), not a one-day move.

    Height = |value| (scaled to the max), green positive / red negative; exact
    value stays in the bar tooltip.
    """
    vals = []
    for attr in ("earn_m4_pct", "earn_m3_pct", "earn_m2_pct", "earn_m1_pct"):
        raw = getattr(card, attr, "") or ""
        try:
            vals.append(float(str(raw).replace("%", "")))
        except (TypeError, ValueError):
            vals.append(None)
    present = [abs(v) for v in vals if v is not None]
    if not present:
        return '<span style="color:#484f58">N/A</span>'
    m = max(max(present), 0.01)
    cols = []
    for v in vals:
        if v is None:
            cols.append('<span class="spark-col"><span class="spark-track">'
                        '<span style="height:3px;background:var(--border)" title="N/A"></span></span>'
                        '<span class="spark-pct na">—</span></span>')
            continue
        h = max(2, round(abs(v) / m * 20))
        cls = "up" if v >= 0 else "dn"
        cols.append(f'<span class="spark-col"><span class="spark-track">'
                    f'<span class="{cls}" style="height:{h}px" title="Return since print: {v:+.0f}%"></span></span>'
                    f'<span class="spark-pct {cls}">{v:+.0f}%</span></span>')
    return f'<span class="spark" title="Return since print (-4E..-1E)">{ "".join(cols) }</span>'


def _agh_owners_cell(owners: str, dilution: bool) -> str:
    color = {"Accumulating": "#3fb950", "Distributing": "#f85149", "Mixed": "#d2991d"}.get(owners or "", "#8b949e")
    label = owners or "—"
    dil = ' <span style="color:#f85149;font-size:8px" title="YoY share count up >20%">DIL</span>' if dilution else ""
    tip = "SEC 13F buy vs sell flow from Asymmetric Growth store"
    return f'<span style="color:{color}" title="{tip}">{esc(label)}</span>{dil}'


def _render_since_er_chart(chart_json: str) -> str:
    if not chart_json:
        return '<span style="color:#484f58">N/A</span>'
    try:
        data = json.loads(chart_json)
    except (json.JSONDecodeError, TypeError):
        return '<span style="color:#484f58">N/A</span>'

    points = data.get("points", [])
    pct = data.get("pct", 0)
    anchor = data.get("anchor", "")
    anchor_price = data.get("anchor_price", 0)
    now_label = data.get("now_label", "")

    if len(points) < 2 or anchor_price <= 0:
        return '<span style="color:#484f58">N/A</span>'

    W, H = 80, 45
    margin = 4
    plot_h = H - 20  # space for labels top and bottom
    top_margin = 10

    is_pos = pct >= 0
    color = "#3fb950" if is_pos else "#f85149"

    # Get price range, capped at ±20% from anchor
    prices = [p[1] for p in points]
    min_price = max(min(prices), anchor_price * 0.80)
    max_price = min(max(prices), anchor_price * 1.20)
    price_range = max_price - min_price
    if price_range <= 0:
        price_range = anchor_price * 0.01

    pw = (W - 2 * margin) / max(len(points) - 1, 1)

    def py(price):
        ratio = (price - min_price) / price_range
        return top_margin + plot_h - ratio * plot_h

    coords = []
    for i, (_, price) in enumerate(points):
        x = margin + i * pw
        y = py(price)
        coords.append(f"{x:.1f},{y:.1f}")

    # Baseline at anchor price
    baseline_y = py(anchor_price)

    svg = f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">'
    svg += f'<rect width="{W}" height="{H}" fill="none"/>'
    svg += f'<line x1="{margin}" y1="{baseline_y:.0f}" x2="{(W-margin):.0f}" y2="{baseline_y:.0f}" stroke="#21262d" stroke-width="0.5" stroke-dasharray="2,2"/>'
    svg += f'<polyline points="{" ".join(coords)}" fill="none" stroke="{color}" stroke-width="1.2"/>'
    # Last point dot
    svg += f'<circle cx="{margin + (len(points)-1)*pw:.1f}" cy="{py(points[-1][1]):.1f}" r="1.5" fill="{color}"/>'
    # Anchor price label left
    svg += f'<text x="{margin}" y="{(baseline_y - 2):.0f}" fill="#8b949e" font-size="6">${anchor_price:.1f}</text>'
    # Current price label right
    svg += f'<text x="{(W - margin):.0f}" y="{(py(points[-1][1]) - 2):.0f}" fill="{color}" font-size="6" text-anchor="end">${points[-1][1]:.1f}</text>'
    # % label at top
    pct_str = f"{pct:+.1f}%"
    svg += f'<text x="{W/2:.0f}" y="8" text-anchor="middle" fill="{color}" font-size="8" font-weight="700">{pct_str}</text>'
    # Anchor date label left bottom
    svg += f'<text x="{margin}" y="{H-2}" fill="#8b949e" font-size="6">{anchor}</text>'
    # Current time label right bottom
    if now_label:
        svg += f'<text x="{(W-margin):.0f}" y="{H-2}" fill="#8b949e" font-size="6" text-anchor="end">{now_label}</text>'
    svg += '</svg>'
    return f'<div style="text-align:center">{svg}</div>'


# Rule-key -> short tag used on the board (Why chips, drawer audit).
_RULE_SHORT = {
    "rule1_drivers": "R1",
    "rule2_guidance": "R2",
    "rule3_financial": "R3",
    "rule4_volatility": "R4",
    "rule5_positioning": "R5",
    "rule6_macro": "R6",
    "rule7_accounting": "R7",
    "rule8_sentiment": "R8",
}

# Phase 1 role of each rule — shown in the drawer so the read is explicit.
_RULE_ROLE = {
    "rule1_drivers": "logged (0%)",
    "rule2_guidance": "direction 20%",
    "rule3_financial": "direction 20%",
    "rule4_volatility": "vehicle 0%",
    "rule5_positioning": "tag (no vote)",
    "rule6_macro": "veto 0%",
    "rule7_accounting": "direction 15%",
    "rule8_sentiment": "momentum 10%",
}


def _action_basis(card) -> str:
    """One-line 'where does this Action come from' shown under the label."""
    a = card.final_action or ""
    d = card.direction_score or 0
    v = card.volatility_edge_score or 0
    c = card.confidence_score or 0
    if a == "NO_TRADE":
        return "R6 veto"
    if "OPTIONS" in a:
        return f"dir {d:+.0f} ≥25 · R4 {v:.0f} ≥50"
    if "SHARES" in a:
        return f"dir {d:+.0f} ≥18 · R4 {v:.0f} <50"
    if a == "WATCH_LOW_COVERAGE":
        return f"coverage {c:.0f} <50"
    if a == "WATCH":
        return "|dir| < 12"
    if a == "DIRECTIONAL_WATCH":
        return f"|dir| {abs(d):.0f} 12–18"
    return ""


def _action_tip(card) -> str:
    """Longer tooltip explaining which rule(s) produced the Action."""
    a = card.final_action or ""
    v = card.volatility_edge_score or 0
    if a == "NO_TRADE":
        return "R6 macro veto (|rate beta| > 4 + FOMC/CPI overlap) blocks this event."
    if "OPTIONS" in a:
        return (f"R4 vol-edge {v:.0f} ≥ 50 AND |dir| ≥ 25 (R2/R3/R7) "
                "-> options vehicle.")
    if "SHARES" in a:
        return (f"|dir| ≥ 18 but R4 vol-edge {v:.0f} < 50 "
                "(small EM / long gamma / no edge) -> shares or defined risk.")
    if a == "WATCH_LOW_COVERAGE":
        return "Weighted rule coverage (R2/R3/R7/R8, a missing rule counts as 0) < 50%. Coverage is not a hit rate."
    if a == "WATCH":
        return "Composite direction |dir| < 12 (R2/R3/R7) — no edge."
    if a == "DIRECTIONAL_WATCH":
        return "Directional but |dir| is 12–18 — not yet the options/shares tier."
    return ""


def _edge_cell(card) -> str:
    if not card.edge_ratio or not card.edge_median_realized:
        return '<span style="color:#484f58">N/A</span>'
    ratio = card.edge_ratio
    color = "#3fb950" if ratio > 1 else ("#f85149" if ratio < 1 else "#8b949e")
    tip = (f"{ratio:.2f}x — display only, NOT the R4 vol-edge. Median historical 1-day "
           f"move ({card.edge_median_realized:.1f}%) is {ratio:.2f}× the current implied "
           f"move ({card.expected_move_pct:.1f}%). >1.0x means realized typically exceeds implied.")
    return (f'<span style="color:{color};font-size:12px;font-weight:700" title="{tip}">{ratio:.2f}x</span>'
            f'<span class="tw-pct" style="color:#8b949e">real {card.edge_median_realized:.1f}% / imp {card.expected_move_pct:.1f}%</span>')


def _price_cell(price: str) -> str:
    if not price:
        return '<span style="color:#484f58">—</span>'
    return f'<span style="color:#a5d6ff;font-size:11px;font-weight:600">{esc(price)}</span>'


def _trade_by_iso(card) -> str:
    """ISO date of the last session a position can be opened, or '' when
    unknown. AM reports trade by the prior session's close; PM by same-day."""
    rtl = getattr(card, "report_time_label", "") or ""
    is_am = None
    if rtl == "AM":
        is_am = True
    elif rtl == "PM":
        is_am = False
    ed = getattr(card, "earnings_date", None)
    if ed is None:
        return ""
    from RevenueChaser.core.calendar import trade_by_date
    try:
        tb = trade_by_date(ed, is_am)
    except ValueError:
        return ""  # table does not cover the year — never guess
    return tb.isoformat() if tb else ""


def _trade_by_cell(card) -> str:
    """Display the trade-by date (mm/dd), with a 'by prior close' hint for AM."""
    iso = _trade_by_iso(card)
    if not iso:
        return '<span style="color:#484f58" title="Trade-by unavailable (unknown report time or NYSE calendar gap)">—</span>'
    try:
        from datetime import date as _d
        tb = _d.fromisoformat(iso)
    except ValueError:
        return '<span style="color:#484f58">—</span>'
    rtl = getattr(card, "report_time_label", "") or ""
    tip = ("AM report: enter before the previous session's close."
           if rtl == "AM" else "Enter before this day's close.")
    return f'<span style="color:#d2991d;font-size:10px" title="{tip}">{tb.strftime("%m/%d")}</span>'


def _gamma_regime_tip(card) -> str:
    flip = card.gex_gamma_flip.replace("$", "") if card.gex_gamma_flip else None
    if not flip:
        return "gamma regime unknown (no flip level)"
    if "Negative gamma regime" in (card.gex_structure or ""):
        return f"negative gamma — spot below flip ${flip}: dealers amplify moves (more volatility) till flip is reclaimed"
    return f"positive gamma — spot above flip ${flip}: dealers dampen moves (range-bound) till flip is tested"


def _liquidity_cell(card) -> str:
    lbl = card.liquidity_label or "—"
    # Liquidity is a quality/info signal — never green.
    tone = {"Excellent": "info", "Tradeable": "muted", "Thin": "bear"}.get(lbl, "muted")
    if lbl == "N/A":
        tip = "No UW screener row for this name in this run: liquidity unknown, not scored as thin."
    else:
        tip = "Options tradability from OptVol + OI (Excellent / Tradeable / Thin)."
    return _badge(tone, lbl, title=tip)


def _to_sort_float(val):
    if val is None or val in ("", "—", "N/A"):
        return None
    s = str(val).replace("$", "").replace("%", "").replace(",", "").strip()
    if s in ("", "—", "N/A", "None"):
        return None
    mult = 1.0
    if s.endswith("T"):
        mult = 1e12
    elif s.endswith("B"):
        mult = 1e9
    elif s.endswith("M"):
        mult = 1e6
    elif s.endswith("K"):
        mult = 1e3
    if mult != 1.0:
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def _hard_block_icon(card) -> str:
    try:
        badges = json.loads(card.badges_json or "[]")
    except (json.JSONDecodeError, TypeError):
        badges = []
    hard = [b for b in badges if b in ("MACRO VETO", "BINARY RISK")]
    if not hard:
        return ""
    return f'<span class="excl-tag" title="{" · ".join(hard)}">⚠</span>'


def _fmt_money(v: float) -> str:
    a = abs(v)
    if a < 1e-9:
        return "$0"
    sign = "+" if v > 0 else "-"
    if a >= 1e6:
        return f"{sign}${a/1e6:.2f}M"
    if a >= 1e3:
        return f"{sign}${a/1e3:.0f}K"
    return f"{sign}${a:.0f}"


def _inst_flow_window_text(card) -> str:
    n = getattr(card, "inst_flow_prints", 0) or 0
    span = getattr(card, "inst_flow_span_hours", None)
    if n <= 0 and span is None:
        return ""
    parts = []
    if n > 0:
        parts.append(f"{n}p")
    if span is not None:
        if span < 1:
            parts.append(f"{max(1, int(round(span * 60)))}m")
        else:
            parts.append(f"{span:.1f}h")
    return " · ".join(parts)


def _gex_num(s):
    if not s:
        return None
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


_GEX_LEVELS = [
    # (attr, letter, cls, short, full) — label order; ties keep the earlier one.
    ("gex_put_wall", "P", "put", "put wall", "Put wall — downside support, largest put OI"),
    ("gex_gamma_magnet", "M", "magnet", "magnet", "Magnet / pin — gamma pin level"),
    ("current_price", "S", "spot", "spot", "Spot — current price"),
    ("gex_gamma_flip", "F", "flip", "flip 0\u03b3", "Flip 0\u03b3 — positive/negative gamma boundary"),
    ("gex_call_wall", "C", "call", "call wall", "Call wall — upside resistance, largest call OI"),
]


def _gex_nudge(positions: list, gap: float = 16.0) -> list:
    """Label positions anchored to ticks, then spread so neighbours keep ``gap``."""
    n = len(positions)
    if n == 0:
        return []
    idx = sorted(range(n), key=lambda i: positions[i])
    out = list(positions)
    # forward pass: enforce the minimum gap left→right
    for k in range(1, n):
        i, j = idx[k - 1], idx[k]
        if out[j] - out[i] < gap:
            out[j] = out[i] + gap
    # backward pass: pull the run back inside the track (never stack at the edge)
    if out[idx[-1]] > 100.0:
        out[idx[-1]] = 100.0
        for k in range(n - 2, -1, -1):
            i, j = idx[k], idx[k + 1]
            if out[j] - out[i] < gap:
                out[i] = out[j] - gap
    return [max(0.0, min(100.0, p)) for p in out]


def _gex_profile_cell(card) -> str:
    """Horizontal GEX track: raw ticks on the true price scale, plus labelled
    level keys (P/M/S/F/C) anchored to their ticks and nudged apart."""
    levels = []
    for attr, letter, cls, short, full in _GEX_LEVELS:
        val = _gex_num(getattr(card, attr, ""))
        if val is None:
            continue
        levels.append({"letter": letter, "cls": cls, "short": short,
                       "full": full, "value": val})
    if not levels:
        return '<span style="color:#484f58">N/A</span>'

    xs = [l["value"] for l in levels]
    lo, hi = min(xs), max(xs)
    span = (hi - lo) or 1
    for l in levels:
        l["pos"] = (l["value"] - lo) / span * 100.0
    nudged = _gex_nudge([l["pos"] for l in levels])
    for l, p in zip(levels, nudged):
        l["lpos"] = p

    # Ticks stay on the true min→max scale (mockup marker order).
    mark_order = {"put": 0, "magnet": 1, "flip": 2, "spot": 3, "call": 4}
    markers = "".join(
        f'<b class="{l["cls"]}" style="left:{l["pos"]:.1f}%"></b>'
        for l in sorted(levels, key=lambda x: mark_order.get(x["cls"], 9))
    )
    labels = []
    for l in sorted(levels, key=lambda x: x["lpos"]):
        lp = l["lpos"]
        tf = "translateX(0)" if lp < 10 else ("translateX(-100%)" if lp > 90 else "translateX(-50%)")
        title = f'{l["full"]} ${l["value"]:.2f}'
        labels.append(
            f'<span class="gex-lbl {l["cls"]}" style="left:{lp:.1f}%;transform:{tf}" title="{title}">'
            f'<b class="k">{l["letter"]}</b><span class="strike">{l["value"]:.2f}</span>'
            f'<span class="m">{l["short"]}</span></span>'
        )

    spot = next((l["value"] for l in levels if l["cls"] == "spot"), None)
    flip = next((l["value"] for l in levels if l["cls"] == "flip"), None)
    vs = (spot - flip) / flip if (spot is not None and flip) else None
    if vs is None:
        cap, tone = "flip N/A", "muted"
    else:
        tone = "bull" if vs >= 0 else "bear"
        cap = f"{'+' if vs >= 0 else ''}{vs * 100:.1f}% vs flip"
    tip = (f"Call wall {card.gex_call_wall or '—'} · Put wall {card.gex_put_wall or '—'} · "
           f"Flip {card.gex_gamma_flip or '—'} · Magnet {card.gex_gamma_magnet or '—'}")
    return (f'<div class="gex-track-wrap" title="{esc(tip)}">'
            f'<div class="gex-cap {tone}">{cap}</div>'
            f'<div class="gex-track">{markers}</div>'
            f'<div class="gex-labels">{"".join(labels)}</div>'
            f'</div>')


EXCLUDED_LIST_MAX = 50


def _methodology_tables(filter_stats: dict = None) -> str:
    """Generate explanatory tables at the end of the HTML showing methodology."""
    h = '<details class="section methodology" style="margin-top:40px"><summary class="section-title" style="color:#d2991d">Methodology</summary>'

    # Table 1: Filter Gates
    h += '<h3 style="color:#58a6ff;font-size:13px;margin:16px 0 8px">Filter Gates — Why These Thresholds?</h3>'
    h += '<table style="font-size:11px"><tr><th>Parameter</th><th>Threshold</th><th>What It Filters</th><th>Why</th></tr>'
    rows = [
        ("OptVol", f"≥ {MIN_OPTION_VOLUME:,} contracts", "Dead options", "Ensures at least minimal option activity — removes stocks nobody is trading options on. Earnings plays need option liquidity."),
        ("OI (Open Interest)", "≥ 200", "No options chain", "Even a thin chain has 200+ OI. Without it, there is no options market to trade the earnings reaction."),
        ("$Vol (call + put premium)", "≥ $50K", "Penny options / dead tickers", "The day's call plus put premium as the UW screener served it; a name without it fails this gate."),
        ("Exp. Move Min", "≥ 1.5%", "Dull / no-reaction stocks", "If the market expects less than 1.5% move, there is no edge to capture. The stock is too stable."),
        ("Exp. Move Max", "≤ 25%", "Binary / biotech events", "Above 25% expected move, directional signals break down. These are lottery-ticket events, not systematic trades."),
        ("Watchlist ★", "Always pass", "Your selected tickers", "Tick from watchlist_tickers.json always get scored regardless of filters."),
    ]
    for param, thresh, what, why in rows:
        h += f'<tr><td style="color:#c9d1d9;font-weight:600">{param}</td><td>{thresh}</td><td style="color:#8b949e">{what}</td><td style="color:#8b949e;font-size:10px">{why}</td></tr>'
    h += '</table>'

    # Table 2: NRC Components
    h += '<h3 style="color:#58a6ff;font-size:13px;margin:20px 0 8px">NRC Scoring — What Each Component Measures</h3>'
    h += '<table style="font-size:11px"><tr><th>Component</th><th>Weight</th><th>Measures</th><th>Data Source</th></tr>'
    nrc_rows = [
        ("R0 · Composite lean", "25%", "Mapped from v1 direction score (not raw revenue growth)", "50 + 0.5 × direction"),
        ("R0 · Growth Acceleration", "20%", "AGH: revenue growth accelerating YoY", "ticker_scores; skipped if unknown"),
        ("R0 · Accounting quality", "15%", "Signed Sloan (R7) mapped 0–100", "R7; skipped if unavailable"),
        ("R0 · Event Beat Rate", "10%", "Keyword beat/miss from thesis (heuristic)", "card.key_thesis"),
        ("R0 · Rotation Bonus", "±", "Sector/theme quadrant adjustment", "LEADING +10, IMPROVING +5, LAGGING −10"),
    ]
    for comp, wt, measures, src in nrc_rows:
        h += f'<tr><td style="color:#c9d1d9;font-weight:600">{comp}</td><td>{wt}</td><td style="color:#8b949e">{measures}</td><td style="color:#8b949e;font-size:10px">{src}</td></tr>'
    h += '</table>'
    h += '<p style="color:#8b949e;font-size:10px;margin-top:4px">Live slots only — revision/corroboration dropped. Missing accel/R7 skip their weight (no 50-fill). Quality gate: if R7-mapped quality &lt; 12 → capped at 49. Tiers: A ≥70, B 55–69, C 40–54, Ignore &lt;40.</p>'

    # Table 2.5: Active Rules (scoring rules)
    h += '<h3 style="color:#58a6ff;font-size:13px;margin:20px 0 8px">Active Scoring Rules — Composite Inputs</h3>'
    h += '<table style="font-size:11px"><tr><th>Rule</th><th>Role</th><th>What It Scores</th><th>Data Source</th></tr>'
    rule_rows = [
        ("R1 · Drivers", "reserved 0%", "Demand drivers before the print — not yet wired", "Future: street revenue revisions / alt-data"),
        ("R2 · Guidance", "direction 20%", "Recency-weighted EPS surprise + median; beat > +1%", "/stock/{t}/earnings (EPS vs street)"),
        ("R3 · Financials", "direction 20%", "GAAP operating-margin vote + opex/franchise tag", "/stock/{t}/financials (IS+BS)"),
        ("R4 · Volatility", "vehicle 0%", "Expected-move box, volume-book GEX regime, last-4 straddle", "UW options + /spot-exposures/strike"),
        ("R5 · Positioning", "tag 0% (no vote)", "Lean / Thin / package-risk / squeeze read", "/stock/{t}/flow-recent + /short_screener"),
        ("R6 · Macro", "veto 0%", "Badge on extreme 10Y rate beta; NO_TRADE only w/ FOMC/CPI", "FRED DGS10 or UW 10Y daily"),
        ("R7 · Accounting", "direction 15%", "Signed Sloan accruals, last filed quarter", "cash-flows + balance-sheets (NI cross-check)"),
        ("R8 · Sentiment", "momentum 10%", "Retail call share > 65% → +20; no call premium → unavailable", "/stock/{t}/flow-recent (size < 10)"),
    ]
    for rule, wt, what, src in rule_rows:
        h += f'<tr><td style="color:#c9d1d9;font-weight:600">{rule}</td><td>{wt}</td><td style="color:#8b949e">{what}</td><td style="color:#8b949e;font-size:10px">{src}</td></tr>'
    h += '</table>'
    h += '<p style="color:#8b949e;font-size:10px;margin-top:4px">Direction comes from R2/R3/R7 and the R8 retail-call momentum; R5 is a tag (never a vote), R4/R6 vehicle/veto. Unavailable rules drop out of the direction score (no 0 fill), but they still count against Coverage %.</p>'

    h += '<h3 style="color:#58a6ff;font-size:13px;margin:20px 0 8px">What Is Coverage %?</h3>'
    h += '<div style="font-size:11px;color:#8b949e;line-height:1.6">'
    h += (f'<b>Coverage = 100 × (Σ weight × rule coverage) / (Σ all direction weights '
          f'({_DIRECTION_WEIGHT_SUM:.2f})) × (1 − disagreement)</b><br>')
    h += ('The sum on top runs over the rules that returned data; the denominator is always all four direction rules '
          '(R2, R3, R7, R8), whether or not they returned data. Example: with R8 missing and R2, R3, R7 fully '
          'covered, Coverage = 0.55 / 0.65 = 84.6%, not 100%.<br><br>')
    h += 'This is <b>not a hit rate</b>. An 80 does not mean “right 80% of the time.” It means most of the direction weight returned data and R3 and R7 did not disagree on sign.<br>'
    h += '· <b>Disagreement penalty:</b> −20% only when R3 (GAAP quality) and R7 (cash vs NI) land on opposite signs.<br>'
    h += '· <span style="color:#3fb950">≥50%</span> — enough rules fired to act on coverage grounds.<br>'
    h += '· <span style="color:#d2991d">25–49%</span> — thin coverage or mixed signs → WATCH_LOW_COVERAGE.<br>'
    h += '</div>'

    # Table 3: Rotation Clusters
    h += '<h3 style="color:#58a6ff;font-size:13px;margin:20px 0 8px">Rotation — Cluster to ETF Mapping</h3>'
    h += '<table style="font-size:11px"><tr><th>Cluster</th><th>Sectors</th><th>Tracked ETFs</th></tr>'
    clusters = [
        ("C1 — SaaS", "Software, Cloud, Internet Services", "IGV, WCLD, SKYY"),
        ("C2 — Hardware", "Semiconductors, Electronics, Aerospace", "SMH, SOXX, XLK"),
        ("C3 — Fintech", "Banks, Lending, Financial Services", "XLF, KRE, XHB"),
        ("C4 — Crypto", "Crypto Mining, Blockchain", "IWM, ARKW, XLK"),
        ("C5 — Consumer", "Retail, Travel, Entertainment", "XLY, JETS, XRT"),
        ("C6 — Energy", "Oil, Gas, Utilities, Mining", "XLE, XOP, XLU"),
        ("C7 — Healthcare", "Pharma, Biotech, Medical Devices, Hospitals", "IHI, IBB, XBI"),
    ]
    for cl, sec, etfs in clusters:
        h += f'<tr><td style="color:#c9d1d9;font-weight:600">{cl}</td><td style="color:#8b949e">{sec}</td><td style="color:#58a6ff">{etfs}</td></tr>'
    h += '</table>'
    h += '<p style="color:#8b949e;font-size:10px;margin-top:4px">RS computed weekly vs SPY. Ratio = 0.20×1W + 0.35×1M + 0.45×3M. Momentum = 0.55×1W + 0.30×1M − 0.15×3M. Z-score normalized across all ' + str(len(ROTATION_ETFS)) + ' ETFs.</p>'

    h += '</details>'
    return h


def _kit_rows(cards: list) -> tuple[list[dict], str | None]:
    names = {}
    for c in cards:
        t = str(getattr(c, "ticker", "") or "")
        if t and t not in names:
            names[t] = (getattr(c, "name", None) or "").strip() or t
    try:
        fields = universe.filter_fields(list(names))
    except Exception:  # noqa: BLE001 - a failed read must not fail the render or the Fetch
        return ([{"ticker": t, "name": n} for t, n in names.items()],
                "The filter fields (index, market cap, sector, theme) could not be read, "
                "so the filters have nothing to match on. The board itself is unaffected.")
    rows = []
    for t, n in names.items():
        src = fields.get(t) or fields.get(t.upper())
        if src:
            row = dict(src)
            row["ticker"] = t
            if not row.get("name"):
                row["name"] = n
        else:
            row = {"ticker": t, "name": n}
        rows.append(row)
    return rows, None


def _kit_themes() -> dict:
    try:
        return universe.theme_catalog()
    except Exception:  # noqa: BLE001 - an empty catalog just leaves the Theme facet empty
        return {}


def export_html(cards: list, calendar_events: list[dict] = None, nrc_results: dict = None, filename: str = None, filter_stats: dict = None,
                provider_health: dict | None = None, notice: str | None = None, run_at: float | None = None) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / (filename or f"earnings/revenue_chaser_{ts}.html")

    wl = rawstore.load_watchlist()
    cal_lookup = {e["ticker"]: e for e in (calendar_events or [])}
    col_widths = _compute_col_widths(cards, cal_lookup)

    sections = {k: [] for k in TIER_DESC}
    card_map = {c.ticker: c for c in cards}
    included_tickers = {c.ticker for c in cards if c.filter_status != "excluded"}
    excluded_cards = [c for c in cards if c.filter_status == "excluded"]
    if nrc_results:
        for t, info in nrc_results.items():
            if t in included_tickers:
                tier = info.get("tier", "Ignore")
                sections[tier].append((t, info, card_map.get(t)))
    else:
        sections["Ignore"] = [(c.ticker, {}, c) for c in cards if c.filter_status != "excluded"]

    # Render helper: one table section for a list of items
    def _render_section(items, section_title, section_cls):
        nonlocal body
        if not items: return
        group_tone = {"identity": "th-g1", "decision": "th-g6", "earnings": "th-g2",
                      "market": "th-g4", "gex": "th-g4", "hunter": "th-g5"}
        group_by_id = {c[0]: c[2] for c in _COLUMNS}
        sort_key = {"datetime": "date", "tradeBy": "tradeby", "cov": "conf",
                    "expMove": "em", "edge": "edge", "surprises": "m1", "gex": "gex",
                    "mcap": "mcap", "stkVol": "stkvol", "optVol": "optvol", "oi": "oi",
                    "score": "agh", "acceleration": "accel", "owners": "own"}

        body += (f'<div class="section"><div class="section-title {section_cls}">'
                 f'{section_title} <span class="n" data-total="{len(items)}" '
                 f'style="font-size:14px;color:#8b949e">({len(items)})</span></div>')
        body += '<table class="data-table">' + _colgroup(col_widths) + '<thead>'
        # Group header — colspans recomputed client-side per active view.
        body += '<tr>'
        first = True
        for g in _GROUP_ORDER:
            n = sum(1 for cid in _COL_IDS if group_by_id[cid] == g)
            if not n:
                continue
            radius = ""
            if first:
                radius = ' style="border-radius:6px 0 0 0"'
                first = False
            body += f'<th class="group {group_tone[g]}" data-group="{g}" colspan="{n}"{radius}>{_GROUP_LABELS[g]}</th>'
        body += '</tr><tr>'
        for cid, label, grp, align, _w, cls, _mode, _cp, _m in _COLUMNS:
            # X-4 (#372): the Owners values come from AG's store, not this board's 13F period.
            title = (' title="13F buy vs sell flow as stored by the Asymmetric Growth board, '
                     'not this board\'s 13F period"') if cid == "owners" else ""
            a = ' class="r"' if align == "right" else (' class="c"' if align == "center" else "")
            sk = sort_key.get(cid)
            if sk:
                body += f'<th class="{cls}" data-col="{cid}" data-group="{grp}"{a}{title} data-sort-key="{sk}" onclick="sortColumn(\'{sk}\',this)">{label}</th>'
            else:
                body += f'<th class="{cls}" data-col="{cid}" data-group="{grp}"{a}{title}>{label}</th>'
        body += '</tr></thead><tbody>'

        for t, info, card in items:
            if not card: continue
            cal = cal_lookup.get(t, {})
            sect = esc((card.sector or "—")[:20])
            name = card.name if card.name and card.name != t else "—"
            if len(name) > 18:
                name = name[:17] + "\u2026"
            name = esc(name)
            th = esc(t)
            dir_val = "bull" if card.direction_score >= 12 else ("bear" if card.direction_score <= -12 else "neutral")
            bias_l = "BULL" if card.direction_score >= 12 else ("BEAR" if card.direction_score <= -12 else "NEUTRAL")
            em_pct = card.expected_move_pct or 0
            em_str = f"{em_pct:.1f}%" if em_pct else "—"

            sort_vals = {
                "conf": card.confidence_score, "em": em_pct,
                "edge": card.edge_ratio,
                "m1": _to_sort_float(card.earn_m1_pct),
                "gex": _to_sort_float(card.gex_gamma_flip), "mcap": _to_sort_float(card.market_cap),
                "stkvol": _to_sort_float(card.stock_volume), "optvol": _to_sort_float(card.option_volume),
                "oi": _to_sort_float(card.total_open_interest),
                "agh": card.agh_points if card.agh_points is not None else "",
                "accel": 1 if card.agh_accel is True else (0 if card.agh_accel is False else ""),
                "own": card.agh_owners or "",
                "date": card.earnings_date.isoformat() if card.earnings_date else "",
                "tradeby": _trade_by_iso(card) or "",
            }
            sort_attrs = "".join(f' data-sort-{k}="{esc(v)}"' for k, v in sort_vals.items() if v not in (None, ""))
            row_cls = "watchlist-row" if t in wl else ""
            accel_val = "1" if card.agh_accel is True else ("0" if card.agh_accel is False else "")
            body += (f'<tr class="{row_cls}" data-ticker="{th}" data-name="{name}" '
                     f'data-am-pm="{esc(card.report_time_label or "Other")}" data-wl="{"1" if t in wl else "0"}" '
                     f'data-dir="{dir_val}" data-rot="{esc(card.rotation_quadrant or "")}" '
                     f'data-liq="{"1" if card.liquidity_label in ("Excellent", "Tradeable") else "0"}" '
                     f'data-edge="{"1" if card.edge_ratio > 1 else "0"}" data-attn="{card.attention_score}" '
                     f'data-score="{card.agh_points if card.agh_points is not None else ""}" '
                     f'data-accel="{accel_val}" data-bias="{esc(card.bias_label or "")}"{sort_attrs}>')
            excl_tag = f'<span class="excl-tag" title="Filtered out: {esc(card.filter_reasons)}">⛔</span>' if card.filter_reasons else ""
            ed_str = card.earnings_date.strftime("%m/%d") if card.earnings_date else "—"
            time_lbl = esc(card.report_time_label or "—")
            bias_display = card.bias_label or bias_l
            action_display = card.final_action or "—"
            act_tone = ("bull" if ("BULL" in action_display) else
                        ("bear" if ("BEAR" in action_display) else
                         ("neutral" if "WATCH" in action_display else "muted")))
            cells = {
                "datetime": f'<span style="font-size:10px;color:#a5d6ff">{ed_str} {time_lbl}</span>',
                "tradeBy": _trade_by_cell(card),
                "ticker": f'<span class="ticker">{th}</span>{_hard_block_icon(card)}{excl_tag}',
                "name": name,
                "sector": sect,
                "rotation": _quadrant_badge(card.rotation_quadrant),
                "cov": _conf_cell(card.confidence_score),
                "bias": _bias_cell(bias_display, card.direction_score),
                "action": (f'<span class="badge tone-{act_tone}" style="font-size:9px" title="{_action_tip(card)}">'
                           f'{esc(action_display)}</span>'
                           f'<span class="tw-pct" style="color:#6e7681">{_action_basis(card)}</span>'),
                "expMove": em_str,
                "edge": _edge_cell(card),
                "surprises": _er_spark_cell(card),
                "sinceEr": _render_since_er_chart(card.since_er_chart),
                "price": _price_cell(card.current_price),
                "gex": _gex_profile_cell(card),
                "liquidity": _liquidity_cell(card),
                "mcap": esc(card.market_cap or "—"),
                "stkVol": esc(card.stock_volume or "—"),
                "optVol": esc(card.option_volume or "—"),
                "oi": esc(card.total_open_interest or "—"),
                "score": _agh_points_cell(card.agh_points),
                "acceleration": _agh_accel_cell(card.agh_accel),
                "owners": _agh_owners_cell(card.agh_owners, card.agh_dilution),
            }
            for cid in _COL_IDS:
                body += (f'<td class="{_COL_CLASS[cid]}" data-col="{cid}" '
                         f'data-group="{group_by_id[cid]}">{cells[cid]}</td>')
            body += '</tr>'

        body += "</tbody></table></div>"

    body = ""
    for tier_key in ["Tier A — Revenue Catalyst", "Tier B — Watchlist", "Tier C — Emerging", "Ignore"]:
        items = sections[tier_key]
        if not items: continue
        cls = "st-A" if "A" in tier_key else ("st-B" if "B" in tier_key else ("st-C" if "C" in tier_key else "st-I"))

        # Split into AM and PM
        am_items = [(t, i, c) for t, i, c in items if c and c.report_time_label == "AM"]
        pm_items = [(t, i, c) for t, i, c in items if c and c.report_time_label == "PM"]
        other_items = [(t, i, c) for t, i, c in items if c and c.report_time_label not in ("AM", "PM")]

        # AM section (yellow)
        if am_items:
            _render_section(am_items, f"🟡 {tier_key} — AM", cls + " am-section")
        # PM section (orange)
        if pm_items:
            _render_section(pm_items, f"🟠 {tier_key} — PM", cls + " pm-section")
        # Unknown timing
        if other_items:
            _render_section(other_items, f"⬜ {tier_key} — Other", cls)

    # Excluded by filters — same full columns, placed at the end
    if excluded_cards:
        excluded_items = [(c.ticker, (nrc_results or {}).get(c.ticker, {}), c) for c in excluded_cards]
        _render_section(excluded_items, "⛔ Excluded by Filters", "st-I excluded-section")

    # Same >= 12 line the Bias badge uses (classify_bias), so header counts match the badges (DF11).
    biases = [classify_bias(c.direction_score) for c in cards]
    bulls = biases.count("BULLISH_LEAN")
    bears = biases.count("BEARISH_LEAN")
    neut = biases.count("NEUTRAL")

    # Filter summary — explain gates
    filter_html = ""
    if filter_stats and (filter_stats.get("total") or 0) > 0:   # nothing entered the gates: no line (DF03)
        total = filter_stats.get("total", 0)
        passed = filter_stats.get("passed", 0)
        bypassed = filter_stats.get("bypassed", 0)
        filtered = filter_stats.get("filtered", 0)
        reasons = filter_stats.get("by_reason", {})
        scored_count = passed + bypassed
        filter_html += '<div class="formula" style="line-height:1.7">'
        filter_html += f'<b>Filter Gates:</b> {total} tickers → <span style="color:#3fb950">{scored_count} scored</span>'
        if bypassed:
            filter_html += f' (<span style="color:#d2991d">{bypassed} bypassed:</span> Watchlist always pass)'
        if filtered:
            filter_html += f' · <span style="color:#8b949e">{filtered} filtered</span> by thresholds: '
            filter_html += " · ".join(f'<span style="color:#8b949e">{k}:{v}</span>' for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
        excluded = filter_stats.get("excluded") or []
        if excluded:
            # Excluded names are not scored (#198): list who was left out and why.
            shown = ", ".join(f'{esc(e.get("ticker"))} ({esc(", ".join(e.get("reasons") or []))})'
                              for e in excluded[:EXCLUDED_LIST_MAX])
            more = f" · +{len(excluded) - EXCLUDED_LIST_MAX} more" if len(excluded) > EXCLUDED_LIST_MAX else ""
            filter_html += f'<br><span style="color:#8b949e"><b>Not scored:</b> {shown}{more}</span>'
        filter_html += '<br>'
        filter_html += f'<span style="color:#484f58">Gates: OptVol≥{MIN_OPTION_VOLUME:,} contracts · OI≥{MIN_TOTAL_OI:,} · $Vol≥${MIN_DOLLAR_OPTION_VOLUME // 1000}K (call + put premium) · Exp.Move 1.5%-25% · Max 500 scored/day</span>'
        filter_html += '</div>'

    detail_data = {}
    for c in cards:
        if c.detail_json:
            try:
                detail_data[c.ticker] = json.loads(c.detail_json)
            except (json.JSONDecodeError, TypeError):
                detail_data[c.ticker] = []
    detail_script = f'<script>const DETAIL_DATA = {script_json(detail_data)};</script>'
    colwidths_script = f'<script>const COL_WIDTHS = {script_json(col_widths)};</script>'

    gen_date = date.today()
    decision_data = {}
    for c in cards:
        rules = []
        for key, rd in (c.rule_audit or {}).items():
            if rd.get("available"):
                # R5 is a tag, not a vote — never show it as a signed score.
                show_score = None if key == "rule5_positioning" else rd.get("score")
                rules.append({
                    "r": _RULE_SHORT.get(key, rd.get("label", key)),
                    "role": _RULE_ROLE.get(key, ""),
                    "score": show_score,
                    "coverage": rd.get("coverage"),
                    "reasons": (rd.get("reasons") or [])[:2],
                })
        decision_data[c.ticker] = {
            "action": c.final_action or "",
            "trade_read": c.trade_read or "",
            "trade_read_reason": c.trade_read_reason or "",
            "bias": c.bias_label or "",
            "direction": c.direction_score,
            "vol_edge": c.volatility_edge_score,
            "confidence": c.confidence_score,
            "rules": rules,
            "structure": c.gex_structure or "",
            "structure_tip": _gamma_regime_tip(c) if c.gex_structure else "",
            "inst_flow_label": c.inst_flow_label or "",
            "flow_source": c.flow_source or "",
            "inst_flow_net": c.inst_flow_net or 0,
            "inst_flow_tone": c.inst_flow_tone or "",
            "inst_flow_prints": getattr(c, "inst_flow_prints", 0) or 0,
            "inst_flow_span": getattr(c, "inst_flow_span_hours", None),
            "leaps": c.leaps_label or "",
            "leaps_as_of": c.leaps_as_of or "",
            "leaps_age_days": _leaps_age_days(c.leaps_as_of, gen_date),
            "past_1d": c.past_1d or "",
            "past_1w": c.past_1w or "",
            "past_2w": c.past_2w or "",
            "return_4w": c.return_4w or "",
            "last_er_label": c.last_er_label or "",
        }
    decision_script = f'<script>const DECISION_DATA = {script_json(decision_data)};</script>'

    wl_count = sum(1 for c in cards if c.ticker in wl)
    # ER-9 (#372): the stamp is the run's time in New York, not date.today() at render time.
    stamp_ts = datetime.now(timezone.utc).timestamp() if run_at is None else run_at
    last_sync = datetime.fromtimestamp(stamp_ts, tz=timezone.utc).astimezone(NY).strftime("%A, %B %d, %Y %H:%M ET")

    view_btns = "".join(
        f'<button data-view="{v}" class="{"on" if v == "scan" else ""}" onclick="setView(\'{v}\')">{_VIEW_LABELS[v]}</button>'
        for v in _VIEWS
    )
    chips_html = (
        '<div class="chips" id="chips">'
        '<button class="chip" data-f="score6" onclick="toggleFilter(this)">Score \u2265 6</button>'
        '<button class="chip" data-f="accel" onclick="toggleFilter(this)">Accelerating</button>'
        '<button class="chip" data-f="bull" onclick="toggleFilter(this)">Bullish lean</button>'
        '<button class="chip" data-f="liq" onclick="toggleFilter(this)">Liquid</button>'
        '<button class="chip" data-f="wl" onclick="toggleFilter(this)">Watchlist</button>'
        '</div>'
    )
    views_js = ('<script>const VIEWS = ' + json.dumps(_VIEWS) +
                ';const STICKY_PRICE_VIEWS = ' + json.dumps(_STICKY_PRICE_VIEWS) + ';</script>')
    colpick_html = (
        '<details class="colpick" id="colpick"><summary>Columns</summary><div class="colpick-body">'
        + "".join(
            f'<label><input type="checkbox" data-col="{cid}" checked onchange="onColToggle()"> {label}</label>'
            for cid, label, *_ in _COLUMNS
        )
        + '</div></details>'
    )

    kit_rows, kit_notice = _kit_rows(cards)
    kit_meta = filters_meta("earnings", facets=["index", "mc", "gics", "theme"], themes=_kit_themes())
    kit_js = (f"<script>var DATA = {script_json(kit_rows)};\nvar META = {script_json(kit_meta)};\n"
              f"{FILTERS_JS}\ndocument.addEventListener('DOMContentLoaded', initKit);</script>")
    help_block = help_html(
        "One row per company reporting earnings in the chosen dates, with a trade card: direction "
        "(bullish, bearish or neutral), the <b>NRC</b> score and its tier, sector rotation and the "
        "options-implied move. The <b>Scoring formulas</b> box below spells out the scores. "
        "<b>Fetch</b> reads the earnings calendar for the dates you pick from <b>Unusual Whales</b> and "
        "builds the cards. <b>Refresh</b> shows the last Fetch's board as it was written; this board "
        "is not rebuilt from the store, so new numbers need a new Fetch.")
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>RevenueChaser — Earnings Radar</title>{CSS}{FILTERS_CSS}</head><body>
<div class="app" id="app">
  <div class="main">
    <div class="header">
      <div><h1>REVENUECHASER — Earnings Radar</h1>
        <div class="sub">{len(cards)} events · {bulls} bullish · {bears} bearish · {neut} neutral · {wl_count} watchlist</div></div>
      <div class="sync">Last sync: {last_sync}</div>
    </div>
    {provider_health_html(provider_health)}
    {notice_html(notice)}
    {notice_html(kit_notice)}
    {help_block}
    {recorded_sessions_html("module_earnings_radar", read_note=_HISTORY_NOTE)}
    <div class="toolbar">
      <div class="seg" id="views">{view_btns}</div>
      <div class="seg" id="density"><button data-d="compact" class="on" onclick="setDensity('compact')">Compact</button><button data-d="comfort" onclick="setDensity('comfort')">Comfort</button></div>
      <input class="tb-search" id="q" placeholder="Search ticker / name" />
      {chips_html}
      {colpick_html}
      <div class="spacer"></div>
      <div class="tb-meta" id="clock">as of --:--</div>
      <div class="tb-kbd">j/k · Enter · Esc · 1-4</div>
    </div>
    {filters_bar_html("rc-filters")}
    <div class="kit-count" id="kit-count" aria-live="polite"></div>
    <div class="board" id="board">
      {filter_html}
      <details class="section methodology" style="margin-top:4px"><summary class="section-title" style="color:#d2991d;cursor:pointer">Scoring formulas (Rotation + NRC)</summary>
      <div class="formula" style="line-height:1.7"><b>Rotation Mapping:</b> Weekly candles vs SPY · C1 SaaS→IGV/WCLD/SKYY · C2 Hardware→SMH/SOXX/XLK · C3 Fintech→XLF/KRE/XHB · C4 Crypto→IWM/ARKW/XLK · C5 Consumer→XLY/JETS/XRT · C6 Energy→XLE/XOP/XLU · C7 Healthcare→IHI/IBB/XBI</div>
      <div class="formula" style="line-height:1.7"><b>NRC:</b> live slots only (growth · accel · R7 quality · event), weights renormalized. No 50-fills.<br>
      Quality gate: capped at 49 if R7-mapped quality &lt;12<br>
      <span>Tier A</span> (≥70) · <span>Tier B</span> (55–69) · <span>Tier C</span> (40–54) · <span>Ignore</span> (&lt;40)</div>
      </details>
      {body}
      {_methodology_tables(filter_stats)}
    </div>
  </div>
  <aside class="drawer" id="drawer"><div class="drawer-in" id="drawerIn"></div></aside>
</div>
{detail_script}
{decision_script}
{colwidths_script}
{views_js}
{watchlist_html("earnings_radar")}
{DETAIL_JS}
{kit_js}</body></html>"""

    path.parent.mkdir(parents=True, exist_ok=True)
    write_artifact(path, html)
    return path


def open_in_browser(path: Path):
    import webbrowser
    webbrowser.open(f"file:///{path.as_posix()}")
