"""Asymmetric Growth Hunter raporu — koyu terminal teması, kart grid, Rules paneli, filtreler."""
from __future__ import annotations

from datetime import datetime

from RevenueChaser.core import funnel as funnel_mod
from RevenueChaser.core import ticker_store
from RevenueChaser.core import universe as _universe
from RevenueChaser.core.gics_map import sector_path
from RevenueChaser.core.html_safe import esc, script_json
from RevenueChaser.core.reporting_common import (
    FILTERS_CSS, FILTERS_JS, PAYLOAD_JS, filters_bar_html, filters_meta,
    help_html, notice_html, provider_health_html, universe_provenance_html, watchlist_html,
)
from RevenueChaser.module_asymmetric_growth import engine
from RevenueChaser.module_asymmetric_growth import history as ag_history


def _card_rows(cards: list[dict]) -> list[dict]:
    """The cards as stored (``ticker_store.card_view``) plus the universal-filter fields (#383 C1c).

    A Fetch and a Refresh show the same card (K4, #383 R1); ``gics`` and ``theme_ids`` are added here, from a
    store read, so a Refresh stays network-free. The stored card is never mutated (``card_view`` copies it).
    """
    views = [ticker_store.card_view(c) for c in cards]
    fields = _universe.filter_fields([v.get("ticker") for v in views])
    for v in views:
        f = fields.get(v.get("ticker"))
        if f:
            v["gics"] = f["gics"]
            v["theme_ids"] = f["theme_ids"]
        else:
            v["gics"] = sector_path(v.get("sector") or "Unclassified", v.get("industry") or "")
            v["theme_ids"] = []
    return views


def board_payload(result: dict) -> dict:
    """The board's cards outside its HTML (S7, K10 #383 C6a, card F): ``{asof, cards}``.

    The runner stores it next to the page (``reporting_common.write_board``, gzip) and the page loads it from
    ``GET /view/module_asymmetric_growth/payload.json``; the HTML carries the chrome and the scripts only. An empty
    scan has an empty ``cards`` list, so the page shows its no-data message rather than a failed load.
    """
    return {"asof": result.get("asof") or "", "cards": _card_rows(result.get("cards") or [])}


def _theme_catalog() -> dict:
    """The pinned theme run's catalog for the Theme facet (store read; empty on a fresh store)."""
    try:
        return _universe.theme_catalog()
    except Exception:  # noqa: BLE001 - a missing or unreadable catalog never fails a render
        return {}


def _fmt_date(asof: str) -> str:
    try:
        dt = datetime.fromisoformat(asof.replace("Z", "+00:00"))
        return dt.strftime("%A, %B %d, %Y")
    except (ValueError, TypeError):
        return asof


_NO_SCORES_MESSAGE = ("No companies could be scored from the stored data. "
                      "Press Fetch to load the per-ticker inputs.")


def _universe_tile_and_funnel(result: dict, summary: dict) -> tuple[str, str]:
    """The header's first tile and the funnel panel (#150 / #232).

    With a valid funnel the tile is what was SCORED, of what was screened; the panel lists every reason a name
    has no card. A result without one (an older cached payload) keeps the universe-size tile.
    """
    universe = result.get("universe_total", 0) or summary.get("scanned", 0)
    old = (f'<div class="stat"><div class="v">{esc(universe)}</div><div class="l">Scanned Universe</div>'
           f'<div class="d">all market caps</div></div>')
    steps = funnel_mod.from_dict(result.get("funnel_steps"))
    if funnel_mod.validate(steps):
        return old, ""
    start = steps[0]["count"]
    scored = steps[-1]["count"]
    excluded = sum(st["count"] for st in steps if st["key"] == "excluded_types")
    tile = (f'<div class="stat"><div class="v">{esc(scored)}</div><div class="l">Scored</div>'
            f'<div class="d">of {esc(start - excluded)} screened &middot; {esc(start)} in universe</div></div>')
    return tile, funnel_mod.funnel_panel_html(steps)


def _history_panel(sessions: list[dict], error: str | None = None) -> str:
    """The recorded-sessions panel (#380, slice A2): what the result history holds for this board, newest first."""
    head = ('<details class="rules" id="history-panel"><summary>Recorded sessions (result history)</summary>'
            '<div class="rules-body">')
    if error:
        return head + f'<p>History could not be read: {esc(error)}</p></div></details>'
    if not sessions:
        return head + ('<p>No session recorded yet: the first complete Fetch after a session close records one.</p>'
                       '</div></details>')
    rows = []
    for s in sessions:
        avg = "" if s["avg_points"] is None else f'{s["avg_points"]:.1f}'
        rows.append(f'<tr><td>{esc(s["session_date"])}</td><td>{esc(s["runs"])}</td><td>{esc(s["names"])}</td>'
                    f'<td>{esc(avg)}</td><td>{esc(", ".join(s["top"] or []))}</td></tr>')
    body = "".join(rows)
    return (head + '<p>One snapshot per session: the first complete Fetch after the close (later Fetches of the '
            'session are counted, their rows are not kept). Read when this page was rendered, so a Fetch shows its '
            'own session from the next Refresh.</p>'
            f'<table><tr><th>Session</th><th>Fetches</th><th>Names</th><th>Avg pts</th>'
            f'<th>Top {ag_history.TOP_N} by points</th></tr>{body}</table></div></details>')


def _history_html() -> str:
    """Read the history for the panel; a failed read is shown on the board, never a failed render."""
    try:
        return _history_panel(ag_history.recorded_sessions())
    except Exception as exc:  # noqa: BLE001 - the board renders without its history panel's rows
        first = str(exc).splitlines()[0] if str(exc) else ""
        return _history_panel([], f"{type(exc).__name__}: {first}")


def render_asymmetric_growth_hunter_html(result: dict) -> str:
    summary = result.get("summary", {})
    asof = result.get("asof", "")
    sync_date = _fmt_date(asof)
    thresholds = script_json(result.get("thresholds", {}))
    # Why the board is empty (#145): the scan's own message, never "Click Refresh to retry" (Refresh is store-only).
    empty_msg = script_json(result.get("no_data_message") or _NO_SCORES_MESSAGE)
    # The filter kit's meta (K1): the pinned theme catalog and the fixed facet order. No floor_mc: the board covers
    # every cap, so Micro stays a counted bucket (R2B-1, avo "Fix ile al").
    meta = script_json(filters_meta("asymmetric_growth", earnings_mode="filter",
                                    themes=_theme_catalog()))

    thr = result.get("thresholds", {})

    def _def(key: str) -> str:
        v = thr.get(key)
        if key in ("accelerating", "fcf", "above_sma50"):
            return "default: on" if v else "default: off"
        if key == "runway":
            return f"default {v:g}y"
        if key == "vol_ratio":
            return f"default {v:g}×"
        return f"default {int(float(v) * 100)}%"

    universe_prov = universe_provenance_html(
        result.get("universe_generated_at"), result.get("universe_refreshed_by"),
        partial=bool(result.get("universe_partial")))

    score_errors = int(summary.get("score_errors") or 0)
    score_qa = ""
    if score_errors:
        kinds = ", ".join(f"{esc(k)} x{esc(v)}" for k, v in
                          sorted((summary.get("score_error_types") or {}).items()))
        score_qa = (f'<div class="sub" id="score-qa" style="color:#e67e22">'
                    f'QA: {score_errors} ticker(s) could not be scored ({kinds}); see the log.</div>')

    universe_tile, funnel_html = _universe_tile_and_funnel(result, summary)
    history_html = _history_html()
    # The prices-store session the momentum came from (#380 S4, PR 11): the Fetch dates it, a Refresh restores it from the header.
    prices_date = result.get("data_as_of")
    prices_chip = (f'<span id="prices-date">Prices: session {esc(prices_date)}</span>'
                   if prices_date else "")
    # Rows a Fetch stored before this version (#380 S2): no tier yet, so the board opens on every card, said here.
    old_rows = engine.scored_before(result.get("cards") or [])
    old_notice = notice_html(
        f"These cards were scored before v{engine.SELECTION_VERSION} (tiers, the C18 data checks and momentum "
        "from the stored daily bars of the scored session); the board shows every card. The next Fetch applies the new "
        "rules.") if old_rows else ""
    tier_default = "all" if old_rows else "top"

    help_block = help_html(
        "Each card is a US-listed company scored on three signals: <b>accelerated growth</b> "
        "(revenue growth of 20%+ that is speeding up, gross margin 40%+), <b>survival</b> (positive free "
        "cash flow, low net debt, two years of runway) and <b>momentum</b> (near the 52-week high, rising "
        "volume, above the 50-day average). Up to 9 points; the tiles above count the cards per band. "
        "A signal is <b>strong</b> with 2 of its 3 checks passed: three strong signals make the <b>Balanced</b> "
        "tier, two the <b>Strong</b> tier, and the board opens on those two (ALL shows every card). The tier "
        "uses the default thresholds; the sliders move points only. "
        "Figures come from <b>SEC EDGAR</b> filings; prices, volume and the 52-week high from the <b>stored daily "
        "bars</b> (the prices store, the session named above). <b>Fetch</b> runs the scan: it reads filings and prices "
        "for the universe and stores the scored cards. <b>Refresh</b> shows the last Fetch's stored cards with no "
        "network call and no re-scoring.")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Asymmetric Growth Hunter</title>
<style>
  :root {{
    --bg:#0a0d12; --panel:#12161e; --panel2:#1a1f2b; --border:#232a38;
    --text:#d7dce5; --muted:#8a93a6; --accent:#4f9dff; --green:#2ecc71;
    --red:#e74c3c; --amber:#f1c40f; --neon:#00e6a8;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; font-size:14px; line-height:1.5; }}
  .wrap {{ max-width:100%; margin:0 auto; padding:20px 24px 60px; }}
  .topbar {{ display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;
    border-bottom:1px solid var(--border); padding-bottom:12px; margin-bottom:14px; }}
  .topbar h1 {{ margin:0; font-size:22px; letter-spacing:-.4px; }}
  .topbar .sub {{ color:var(--muted); margin-top:2px; font-size:13px; }}
  .status {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; font-size:12px; color:var(--muted); }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:20px; font-size:11px; font-weight:700; }}
  .b-sector {{ background:var(--panel2); color:var(--muted); }}
  .b-index {{ background:rgba(79,157,255,.14); color:var(--accent); }}
  .b-tier3 {{ background:rgba(0,230,168,.16); color:var(--neon); }}
  .b-tier2 {{ background:rgba(79,157,255,.18); color:var(--accent); }}
  .b-flag {{ background:rgba(241,196,15,.14); color:var(--amber); }}
  .checks {{ font-size:11px; color:var(--amber); }}
  .prov {{ font-size:10.5px; color:var(--muted); }}
  .banner {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:18px; }}
  .stat {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:12px 14px; }}
  .stat .v {{ font-size:22px; font-weight:800; }}
  .stat .l {{ color:var(--text); font-size:12px; font-weight:600; margin-top:2px; }}
  .stat .d {{ color:var(--muted); font-size:11px; }}
  details.rules {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; margin-bottom:16px; }}
  details.rules summary {{ cursor:pointer; padding:12px 16px; font-weight:700; color:var(--accent); }}
  .rules-body {{ padding:0 16px 16px; color:var(--muted); }}
  .rules-body table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
  .rules-body th,.rules-body td {{ text-align:left; padding:5px 8px; border-bottom:1px solid var(--panel2); }}
  .rules-body th {{ color:var(--text); text-transform:uppercase; font-size:10.5px; letter-spacing:.4px; }}
  .rules-body .note {{ font-size:11px; margin-top:8px; }}
  .controls {{ display:flex; flex-direction:column; gap:8px; margin-bottom:16px; }}
  .frow {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; }}
  .flbl {{ color:var(--muted); font-size:12px; font-weight:600; }}
  .tabs {{ display:inline-flex; background:var(--panel); border:1px solid var(--border); border-radius:8px; overflow:hidden; flex-wrap:wrap; }}
  .tab {{ padding:0 14px; height:34px; line-height:34px; cursor:pointer; font-weight:600; font-size:13px;
    color:var(--muted); border-right:1px solid var(--border); user-select:none; }}
  .tab:last-child {{ border-right:none; }}
  .tab.on {{ background:var(--panel2); color:var(--text); }}
  .tab .cnt {{ color:var(--muted); font-size:11px; font-weight:400; }}
  .tab.on .cnt {{ color:var(--neon); }}
  select, input[type=text], input[type=number] {{ background:#10151f; border:1px solid #2a3345; color:var(--text);
    border-radius:6px; height:34px; padding:0 10px; font:500 13px/1 inherit; }}
  input[type=text] {{ flex:1; min-width:200px; }}
  input[type=number] {{ width:96px; }}
  .count {{ color:var(--muted); font-size:12px; margin-left:auto; white-space:nowrap; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:14px; }}
  .card {{ background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:14px; display:flex; flex-direction:column; gap:9px; }}
  .head {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center; }}
  .mult {{ font-weight:800; font-size:15px; color:#0a0d12; background:var(--neon); border-radius:6px; padding:1px 8px; }}
  .tick {{ font-size:14px; line-height:1.35; }}
  .tick b {{ font-size:15px; letter-spacing:-.2px; }}
  .tick .sep {{ color:var(--muted); }}
  .tick .focus {{ color:var(--accent); }}
  .pv {{ font-size:12px; color:var(--muted); }}
  .pv b {{ color:var(--text); }}
  .pv .earn b {{ color:var(--amber); }}
  .metrics {{ display:grid; grid-template-columns:repeat(6,1fr); gap:5px; }}
  .m {{ background:var(--panel2); border-radius:8px; padding:6px 4px; text-align:center; min-width:0; }}
  .m .k {{ font-size:9px; color:var(--muted); letter-spacing:.3px; text-transform:uppercase; }}
  .m .val {{ font-weight:700; font-size:11px; overflow-wrap:break-word; line-height:1.15; }}
  .signals {{ display:flex; flex-direction:column; gap:5px; }}
  .sig {{ display:flex; align-items:center; gap:8px; font-size:12.5px; }}
  .sig .ck {{ width:16px; font-weight:700; }}
  .ck.on {{ color:var(--neon); }}
  .ck.off {{ color:var(--muted); }}
  .sig .nm {{ flex:1; color:var(--muted); }}
  .sig .score {{ font-weight:700; }}
  .warn {{ font-size:11px; color:var(--red); }}
  .tags {{ display:flex; flex-wrap:wrap; gap:6px; }}
  .tag {{ font-size:10.5px; padding:2px 8px; border-radius:20px; background:var(--panel2); color:var(--muted); }}
  .tag.tech {{ background:rgba(0,230,168,.12); color:var(--neon); }}
  .foot {{ font-size:10.5px; color:var(--muted); border-top:1px solid var(--panel2); padding-top:6px; }}
  .foot .acc {{ color:var(--green); font-weight:700; }}
  .foot .dis {{ color:var(--red); font-weight:700; }}
  .foot .mix {{ color:var(--amber); font-weight:700; }}
  .inst {{ border-top:1px solid var(--panel2); padding-top:6px; }}
  .inst .ih {{ display:flex; align-items:center; gap:8px; font-size:11px; color:var(--muted); font-weight:700; margin-bottom:4px; }}
  .inst .ih .acc {{ color:var(--green); }}
  .inst .ih .dis {{ color:var(--red); }}
  .inst .ih .mix {{ color:var(--amber); }}
  .inst table {{ width:100%; border-collapse:collapse; font-size:11px; }}
  .inst td {{ padding:2px 0; }}
  .inst td.k {{ color:var(--muted); }}
  .inst td.v {{ text-align:right; color:var(--text); font-variant-numeric:tabular-nums; }}
  .empty {{ color:var(--muted); padding:40px; text-align:center; }}
  .thr {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:10px; }}
  .thr .t {{ background:var(--panel2); border-radius:8px; padding:8px 10px; }}
  .thr .tl {{ display:flex; justify-content:space-between; font-size:11.5px; color:var(--muted); }}
  .thr .tl b {{ color:var(--text); }}
  .thr .row {{ display:flex; align-items:center; gap:8px; }}
  .thr input[type=range] {{ flex:1; accent-color:var(--neon); }}
  .thr input[type=number] {{ width:56px; height:26px; padding:0 6px; font:500 12px/1 inherit; }}
  .thr .def {{ font-size:10.5px; color:var(--muted); margin-top:3px; }}
  .thr .def-inline {{ color:var(--muted); font-size:11px; }}
  .thr .tog {{ display:flex; align-items:center; gap:8px; font-size:12.5px; color:var(--muted); padding:6px 0; }}
  .thr .tog input {{ accent-color:var(--neon); width:16px; height:16px; }}
  button.reset {{ background:#10151f; color:var(--text); border:1px solid #2a3345; border-radius:6px;
    height:28px; padding:0 12px; cursor:pointer; font-weight:600; font-size:12px; margin-bottom:10px; }}
  button.reset:hover {{ background:#18293a; }}
</style>
{FILTERS_CSS}
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>Asymmetric Growth Hunter</h1>
      <div class="sub">Asymmetric growth hunt: 3 signals (accelerated growth, survival, momentum)</div>
      {universe_prov}
      {score_qa}
    </div>
    <div class="status">
      <span>Last sync: {sync_date} EOD</span>
      {prices_chip}
    </div>
  </div>

  {provider_health_html(result.get("provider_health"))}
  {old_notice}
  {help_block}

  <div class="banner">
    {universe_tile}
    <div class="stat"><div class="v" id="b-tier3">{summary.get("tier_3", 0)}</div><div class="l">Balanced</div><div class="d">3 strong signals</div></div>
    <div class="stat"><div class="v" id="b-tier2">{summary.get("tier_2", 0)}</div><div class="l">Strong</div><div class="d">2 strong signals</div></div>
    <div class="stat"><div class="v" id="b-9">{summary.get("pts_9", 0)}</div><div class="l">9 pts</div><div class="d">perfect score</div></div>
    <div class="stat"><div class="v" id="b-87">{summary.get("pts_8_7", 0)}</div><div class="l">8-7 pts</div><div class="d">7-8 points</div></div>
    <div class="stat"><div class="v" id="b-65">{summary.get("pts_6_5", 0)}</div><div class="l">6-5 pts</div><div class="d">5-6 points</div></div>
    <div class="stat"><div class="v" id="b-lt5">{summary.get("pts_lt5", 0)}</div><div class="l">&lt;5 pts</div><div class="d">0-4 points</div></div>
    <div class="stat"><div class="v" id="b-disruptive">{summary.get("disruptive_themed", 0)}</div><div class="l">Themed</div><div class="d">any pinned theme of the rotation map</div></div>
    <div class="stat"><div class="v" id="b-tech">{summary.get("tech_breakthrough", 0)}</div><div class="l">Technological Breakthrough</div><div class="d">High R&D intensity</div></div>
  </div>

  {funnel_html}
  {history_html}

  <details class="rules" id="thr-panel">
    <summary>Thresholds (adjustable on the go)</summary>
    <div class="rules-body">
      <button class="reset" onclick="resetThr()">Reset defaults</button>
      <div class="thr">
        <div class="t">
          <div class="tl"><span>Revenue growth ≥</span><b id="th-rev_growth">20%</b></div>
          <div class="row"><input type="range" id="s-rev_growth" min="0" max="100" step="5" value="20" oninput="setThr('rev_growth', this.value/100)"><input type="number" id="n-rev_growth" min="0" max="100" step="5" value="20" onchange="setThrNum('rev_growth', this.value, 100)"></div>
          <div class="def">{_def("rev_growth")}</div>
        </div>
        <div class="t">
          <div class="tl"><span>Gross margin ≥</span><b id="th-gross_margin">40%</b></div>
          <div class="row"><input type="range" id="s-gross_margin" min="0" max="100" step="5" value="40" oninput="setThr('gross_margin', this.value/100)"><input type="number" id="n-gross_margin" min="0" max="100" step="5" value="40" onchange="setThrNum('gross_margin', this.value, 100)"></div>
          <div class="def">{_def("gross_margin")}</div>
        </div>
        <div class="t">
          <div class="tl"><span>Net debt / assets ≤</span><b id="th-net_debt_assets">10%</b></div>
          <div class="row"><input type="range" id="s-net_debt_assets" min="0" max="100" step="5" value="10" oninput="setThr('net_debt_assets', this.value/100)"><input type="number" id="n-net_debt_assets" min="0" max="100" step="5" value="10" onchange="setThrNum('net_debt_assets', this.value, 100)"></div>
          <div class="def">{_def("net_debt_assets")}</div>
        </div>
        <div class="t">
          <div class="tl"><span>Cash runway ≥</span><b id="th-runway">2y</b></div>
          <div class="row"><input type="range" id="s-runway" min="0" max="10" step="0.5" value="2" oninput="setThr('runway', this.value)"><input type="number" id="n-runway" min="0" max="10" step="0.5" value="2" onchange="setThrNum('runway', this.value, 1)"></div>
          <div class="def">{_def("runway")}</div>
        </div>
        <div class="t">
          <div class="tl"><span>52w proximity ≤</span><b id="th-dist_52w">10%</b></div>
          <div class="row"><input type="range" id="s-dist_52w" min="0" max="100" step="5" value="10" oninput="setThr('dist_52w', this.value/100)"><input type="number" id="n-dist_52w" min="0" max="100" step="5" value="10" onchange="setThrNum('dist_52w', this.value, 100)"></div>
          <div class="def">{_def("dist_52w")}</div>
        </div>
        <div class="t">
          <div class="tl"><span>Volume ratio ≥</span><b id="th-vol_ratio">1.4×</b></div>
          <div class="row"><input type="range" id="s-vol_ratio" min="0" max="5" step="0.1" value="1.4" oninput="setThr('vol_ratio', this.value)"><input type="number" id="n-vol_ratio" min="0" max="5" step="0.1" value="1.4" onchange="setThrNum('vol_ratio', this.value, 1)"></div>
          <div class="def">{_def("vol_ratio")}</div>
        </div>
        <div class="t">
          <div class="tl"><span>R&amp;D / revenue ≥</span><b id="th-rd_intensity">15%</b></div>
          <div class="row"><input type="range" id="s-rd_intensity" min="0" max="50" step="5" value="15" oninput="setThr('rd_intensity', this.value/100)"><input type="number" id="n-rd_intensity" min="0" max="50" step="5" value="15" onchange="setThrNum('rd_intensity', this.value, 100)"></div>
          <div class="def">{_def("rd_intensity")}</div>
        </div>
        <div class="t">
          <div class="tl"><span>Dilution warn &gt;</span><b id="th-dilution">20%</b></div>
          <div class="row"><input type="range" id="s-dilution" min="0" max="200" step="5" value="20" oninput="setThr('dilution', this.value/100)"><input type="number" id="n-dilution" min="0" max="200" step="5" value="20" onchange="setThrNum('dilution', this.value, 100)"></div>
          <div class="def">{_def("dilution")}</div>
        </div>
        <div class="t">
          <label class="tog"><input type="checkbox" id="s-accelerating" checked onchange="setThr('accelerating', this.checked)"> Accelerating growth <span class="def-inline">({_def("accelerating")})</span></label>
        </div>
        <div class="t">
          <label class="tog"><input type="checkbox" id="s-fcf" checked onchange="setThr('fcf', this.checked)"> FCF &gt; 0 required <span class="def-inline">({_def("fcf")})</span></label>
        </div>
        <div class="t">
          <label class="tog"><input type="checkbox" id="s-above_sma50" checked onchange="setThr('above_sma50', this.checked)"> Price &gt; 50d MA required <span class="def-inline">({_def("above_sma50")})</span></label>
        </div>
      </div>
    </div>
  </details>

  <details class="rules">
    <summary>Methodology / Rules (all signals)</summary>
    <div class="rules-body">
      <table>
        <tr><th>Section</th><th>Signal</th><th>Criteria (each sub-criterion counts toward the signal's X/3)</th></tr>
        <tr><td>Universe</td><td>—</td><td><b>United States (US)-listed common stocks only</b> (American Depositary Receipts (ADRs) and foreign companies excluded) · all market capitalizations · all sectors</td></tr>
        <tr><td rowspan="3">Signal 1</td><td>Accelerated Growth</td><td>Revenue growth of at least 20% · growth accelerating year-over-year (YoY) · gross profit margin of at least 40%</td></tr>
        <tr><td>Survival / Runway</td><td>Positive free cash flow (FCF) · net debt divided by total assets of at most 10% · cash runway of at least 2 years</td></tr>
        <tr><td>Momentum / Volume</td><td>Price within 10% of the 52-week high · 10-day average trading volume at least 1.4× the 3-month average volume · price above the 50-day moving average (MA), read from the stored daily bars (the prices store) of the scored session</td></tr>
        <tr><td>Scoring</td><td>—</td><td>Total points = the number of passed sub-criteria (0 to 9). Point buckets: 9 · 8-7 · 6-5 · below 5.</td></tr>
        <tr><td>Tier</td><td>—</td><td>A signal is strong with at least {engine.TIER_MIN_PASSES} of its 3 sub-criteria passed. Balanced = 3 strong signals, Strong = 2. Computed with the default thresholds (the sliders move points only). Cards sort by tier, then points, then revenue growth capped at +100%.</td></tr>
        <tr><td>Sector</td><td>—</td><td>Financial Services and Real Estate (the universe sector) are not comparable on revenue, margin and free cash flow: their cards keep their points, carry a "Not comparable" badge and get no tier.</td></tr>
        <tr><td>Data checks</td><td>—</td><td>A prior year with revenue of zero or less, gross profit above revenue, or share counts on different bases (diluted against basic) make that figure unknown, not a fail. Growth above +{int(engine.RULES["suspicious_growth"] * 100)}% is flagged and sorts with the unknowns. A name whose latest annual report ended more than {engine.RULES["stale_days"]} days ago is excluded and listed in the funnel. When only its revenue figure is that old (the newer reports carry none), the card reads the latest report with revenue unknown, is flagged and gets no tier.</td></tr>
        <tr><td>Overlays</td><td>—</td><td>Themed (any pinned theme of the rotation map; no theme is graded as disruptive) · Technological Breakthrough (research and development (R&amp;D) spending divided by revenue of at least 15%)</td></tr>
        <tr><td>Warnings</td><td>—</td><td>High dilution (year-over-year (YoY) diluted share count growth above +20%)</td></tr>
      </table>
      <div class="note">
        Data: fundamentals from the Securities and Exchange Commission (SEC) EDGAR filings (annual 10-K) ·
        price, volume, 52-week high and 50-day mean from the stored daily price bars (the prices store, the session in the header) ·
        institutional ownership from SEC Form 13F (reported quarterly, ~45-day filing lag) where available,
        otherwise the institutional ownership percentage from Yahoo ·
        Unusual Whales (UW) options flow is manual-only (rule K3) ·
        no Seeking Alpha confirmation (rule 3).
        The scanner produces candidate lists, not directional signals.
      </div>
    </div>
  </details>

  <div class="controls">
    <div class="frow">
      <span class="flbl">Tier</span>
      <div class="tabs">
        <div class="tab" data-tier="top" onclick="setTier('top')">Balanced + Strong</div>
        <div class="tab" data-tier="3" onclick="setTier('3')">Balanced</div>
        <div class="tab" data-tier="2" onclick="setTier('2')">Strong</div>
        <div class="tab" data-tier="none" onclick="setTier('none')">No tier</div>
        <div class="tab" data-tier="all" onclick="setTier('all')">ALL</div>
      </div>
    </div>
    <div class="frow">
      <span class="flbl">Points</span>
      <div class="tabs">
        <div class="tab on" data-pt="all" onclick="setPt('all')">ALL</div>
        <div class="tab" data-pt="9" onclick="setPt('9')">9</div>
        <div class="tab" data-pt="8-7" onclick="setPt('8-7')">8-7</div>
        <div class="tab" data-pt="6-5" onclick="setPt('6-5')">6-5</div>
        <div class="tab" data-pt="<5" onclick="setPt('<5')">&lt;5</div>
      </div>
    </div>
    <div class="frow">
      <input type="text" id="q" placeholder="Search ticker / company" oninput="render()" />
      <span class="count" id="count"></span>
    </div>
  </div>

  {filters_bar_html("rc-filters")}

  <div class="grid" id="grid"></div>
</div>

<script>{FILTERS_JS}</script>
{watchlist_html("asymmetric_growth")}
<script>{PAYLOAD_JS}</script>
<script>
var DATA = [];   // the cards: loaded from the payload route (S7, K10), never inlined
var META = {meta};
var PT = "all";
var TIER = {script_json(tier_default)};   // "top" = Balanced + Strong; "all" while the rows predate the tiers
var RCF = null;
var THRESHOLDS = {thresholds};
var DEFAULTS = {thresholds};
var EMPTY_MSG = {empty_msg};

var SLIDERS = [
  {{param:"rev_growth", s:"s-rev_growth", n:"n-rev_growth", scale:100}},
  {{param:"gross_margin", s:"s-gross_margin", n:"n-gross_margin", scale:100}},
  {{param:"net_debt_assets", s:"s-net_debt_assets", n:"n-net_debt_assets", scale:100}},
  {{param:"runway", s:"s-runway", n:"n-runway", scale:1}},
  {{param:"dist_52w", s:"s-dist_52w", n:"n-dist_52w", scale:100}},
  {{param:"vol_ratio", s:"s-vol_ratio", n:"n-vol_ratio", scale:1}},
  {{param:"rd_intensity", s:"s-rd_intensity", n:"n-rd_intensity", scale:100}},
  {{param:"dilution", s:"s-dilution", n:"n-dilution", scale:100}}
];
var TOGGLES = [
  {{param:"accelerating", id:"s-accelerating"}},
  {{param:"fcf", id:"s-fcf"}},
  {{param:"above_sma50", id:"s-above_sma50"}}
];

function pass(c) {{
  return RCF ? RCF.pass(c) : true;
}}

function fmtDate(sec) {{
  if (sec == null) return null;
  var d = new Date(sec * 1000);
  var mo = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][d.getMonth()];
  return mo + " " + d.getDate();
}}

function pointBucket(p) {{
  if (p >= 9) return "9";
  if (p >= 7) return "8-7";
  if (p >= 5) return "6-5";
  return "<5";
}}

function tierPass(c) {{
  // The tier is the server's (default thresholds); the sliders never move it.
  if (TIER === "all") return true;
  if (TIER === "top") return c.tier === 3 || c.tier === 2;
  if (TIER === "none") return c.tier !== 3 && c.tier !== 2;
  return String(c.tier) === TIER;
}}

function markTier(t) {{
  TIER = t;
  document.querySelectorAll('.tab[data-tier]').forEach(function(el){{
    el.classList.toggle("on", el.getAttribute("data-tier")===t);
  }});
}}

function setTier(t) {{
  markTier(t);
  render();
}}

function setPt(pt) {{
  PT = pt;
  document.querySelectorAll('.tab[data-pt]').forEach(function(el){{
    el.classList.toggle("on", el.getAttribute("data-pt")===pt);
  }});
  render();
}}

function usd(v) {{
  if(v===null||v===undefined) return "N/A";
  var a=Math.abs(v), s=v<0?"-":"";
  if(a>=1e9) return s+"$"+(a/1e9).toFixed(2)+"B";
  if(a>=1e6) return s+"$"+(a/1e6).toFixed(1)+"M";
  if(a>=1e3) return s+"$"+(a/1e3).toFixed(1)+"K";
  return s+"$"+a.toFixed(0);
}}
function shares(v) {{
  // 13F buy / sell are share counts (summed per-filer share deltas), never dollars (#380 S3, AG-7).
  if(v===null||v===undefined) return "N/A";
  var a=Math.abs(v), s=v<0?"-":"";
  if(a>=1e9) return s+(a/1e9).toFixed(2)+"B";
  if(a>=1e6) return s+(a/1e6).toFixed(1)+"M";
  if(a>=1e3) return s+(a/1e3).toFixed(1)+"K";
  return s+a.toFixed(0);
}}
function pct(v) {{ return (v===null||v===undefined)?"N/A":(v*100).toFixed(1)+"%"; }}
function num(v,d) {{ return (v===null||v===undefined)?"N/A":v.toFixed(d==null?2:d); }}
function runway(v) {{ if(v===null||v===undefined) return "N/A"; if(v>=999) return "self-funded"; return v.toFixed(1)+"y"; }}

function focusLabel(c) {{
  var base = c.industry || c.sector || "";
  var th = (c.themes && c.themes.length) ? c.themes[0] : "";
  return th ? (base ? base + "/" + th : th) : base;
}}

function sigHtml(s) {{
  var full = s.passed >= 2;
  var mark = full ? '<span class="ck on">&#10003;</span>' : '<span class="ck off">&#10007;</span>';
  var known = (s.known == null || s.known === s.total) ? '' : ' of '+rcEsc(s.known)+' known';
  return '<div class="sig">'+mark+'<span class="nm">'+rcEsc(s.name)+'</span>'
    + '<span class="score">'+rcEsc(s.passed)+'/'+rcEsc(s.total)+known+'</span></div>';
}}

function instTable(c) {{
  if (c.inst_holders == null) {{
    if (c.inst_ownership_pct != null) {{
      return '<div class="foot">Institutional: ' + (c.inst_ownership_pct * 100).toFixed(1) + '% held</div>';
    }}
    return '<div class="foot">Institutional: N/A</div>';
  }}
  // The 13F quarter read and the one QoQ compares to (AG review SF-3); none compared when it was not stored.
  var q13 = (c.period_ends || {{}}).inst_13f || [];
  var netH = (c.inst_new||0) - (c.inst_exited||0);
  var netF = (c.inst_buy_flow||0) - (c.inst_sell_flow||0);
  var dir, cls;
  if (netH > 0 && netF > 0) {{ dir = 'Accumulating'; cls = 'acc'; }}
  else if (netH < 0 && netF < 0) {{ dir = 'Distributing'; cls = 'dis'; }}
  else {{ dir = 'Mixed'; cls = 'mix'; }}
  var qoq = (c.inst_shares_delta_pct != null)
    ? ((c.inst_shares_delta_pct >= 0 ? '+' : '') + (c.inst_shares_delta_pct * 100).toFixed(1) + '%')
    : 'N/A';
  return '<div class="inst">'
    + '<div class="ih">Institutional' + (q13[0] ? ' (13F ' + rcEsc(q13[0]) + ')' : '')
    + ' <span class="' + cls + '">' + dir + '</span></div>'
    + '<table>'
    + '<tr><td class="k">Holders</td><td class="v">' + c.inst_holders + '</td></tr>'
    + '<tr><td class="k">New / Exited</td><td class="v">+' + (c.inst_new||0) + ' / -' + (c.inst_exited||0) + '</td></tr>'
    + '<tr><td class="k">Buy / Sell (shares)</td><td class="v">' + shares(c.inst_buy_flow) + ' / ' + shares(c.inst_sell_flow) + '</td></tr>'
    + '<tr><td class="k">Shares QoQ' + (q13[1] ? ' (vs ' + rcEsc(q13[1]) + ')' : '')
    + '</td><td class="v">' + qoq + '</td></tr>'
    + '</table></div>';
}}

// The data checks a card carries (#380 S1 C18 and S6 C17): what made a figure unknown, or what to read with care.
var FLAG_TEXT = {{
  prior_nonpositive: "Prior-year revenue of zero or less: growth unknown",
  suspicious_growth: "Growth above +500%: check the filing (sorts with the unknowns)",
  revenue_nonpositive: "Revenue of zero or less: margin unknown",
  gross_profit_exceeds_revenue: "Gross profit above revenue: margin unknown",
  mixed_share_basis: "Share counts on different bases (diluted against basic): dilution unknown",
  price_bar_older: "Price and momentum from an older session than the board's (a halt or a gap in the stored bars)",
  no_recent_revenue: "No revenue figure in the last {engine.RULES["stale_days"]} days (the newer reports carry none): revenue unknown, no tier"
}};
function flagsHtml(c) {{
  var lines = (c.flags || []).filter(function(f){{ return f !== "sector_not_comparable"; }})
    .map(function(f){{ return rcEsc(FLAG_TEXT[f] || f); }});
  return lines.length ? '<div class="checks">Data checks: ' + lines.join(" · ") + '</div>' : "";
}}

function provHtml(c) {{
  // Which filings the card read (#380 S3, #66): anchor and prior fiscal year, the three revenue period ends, and the
  // anchor's balance-sheet and cash-flow period ends; "n/a" where a record has none (stored before #239 or S3).
  var pe = c.period_ends || {{}}, rev = pe.revenue || [];
  function d(v) {{ return v ? rcEsc(v) : "n/a"; }}
  function fy(v) {{ return v != null ? "FY" + rcEsc(v) : "FY n/a"; }}
  // Review SF-4 (G6): on the trailing years the card names their ends, not the fiscal years.
  var years = pe.basis === "TTM" ? "TTM to " + d(rev[0]) + " vs TTM to " + d(rev[1]) : fy(c.anchor_fy) + ' vs ' + fy(c.prior_fy);
  return '<div class="prov">' + years
    + ' · revenue ' + d(rev[0]) + ' · ' + d(rev[1]) + ' · ' + d(rev[2])
    + ' · balance sheet ' + d(pe.balance_sheet) + ' · cash flow ' + d(pe.cash_flow)
    + ' · price ' + d(pe.price_bar) + '</div>';
}}

function cardHtml(c) {{
  var focus = focusLabel(c);
  var warn = c.dilution_warning ? '<div class="warn">WARNING: High dilution ('+pct(c.dilution_yoy)+' shares)</div>' : "";
  var ear = c.earnings_date != null ? '<span class="earn">Earnings <b>'+fmtDate(c.earnings_date)+'</b></span>' : "";
  var indexBadges = (c.index || []).map(function(i){{ return '<span class="badge b-index">'+rcEsc(i)+'</span>'; }}).join("");
  var tierBadge = c.tier === 3 ? '<span class="badge b-tier3">Balanced</span>'
    : (c.tier === 2 ? '<span class="badge b-tier2">Strong</span>' : "");
  if ((c.flags || []).indexOf("sector_not_comparable") >= 0) {{
    tierBadge += '<span class="badge b-flag" title="Financial Services and Real Estate are not compared on revenue, '
      + 'margin and free cash flow: no tier">Not comparable</span>';
  }}
  return '<div class="card">'
    + '<div class="head"><span class="mult">'+rcEsc(c.total_points)+'/9</span>' + tierBadge
    + (c.sector?'<span class="badge b-sector">'+rcEsc(c.sector)+'</span>':"")
    + indexBadges + '</div>'
    + '<div class="tick"><b>'+rcEsc(c.ticker)+'</b><span class="sep"> • </span>'+rcEsc(c.name)
    + (focus?'<span class="sep"> • </span><span class="focus">'+rcEsc(focus)+'</span>':"")+'</div>'
    + rcWatchRow(c.ticker)
    + '<div class="pv"><span>Price <b>'+(c.price!=null?"$"+c.price.toFixed(2):"N/A")+'</b></span>'
    + '<span> · Mkt Cap <b>'+usd(c.market_cap)+'</b></span>'
    + (ear?'<span> · '+ear+'</span>':"")+'</div>'
    + '<div class="metrics">'
    + '<div class="m"><div class="k">Rev. Growth</div><div class="val">'+pct(c.rev_growth)+'</div></div>'
    + '<div class="m"><div class="k">Gross Margin</div><div class="val">'+pct(c.gross_margin)+'</div></div>'
    + '<div class="m"><div class="k">P/S</div><div class="val">'+num(c.ps_ratio)+'</div></div>'
    + '<div class="m"><div class="k">FCF</div><div class="val">'+usd(c.fcf)+'</div></div>'
    + '<div class="m"><div class="k">Runway</div><div class="val">'+runway(c.runway_years)+'</div></div>'
    + '<div class="m"><div class="k">R&D / Rev</div><div class="val">'+pct(c.rd_intensity)+'</div></div>'
    + '</div>'
    + provHtml(c)
    + '<div class="signals">'+c.signals.map(sigHtml).join("")+'</div>'
    + (c.tech_breakthrough?'<div class="tags"><span class="tag tech">Technological Breakthrough</span></div>':"")
    + warn
    + flagsHtml(c)
    + instTable(c)
    + '</div>';
}}

function fmtThr(param, val) {{
  if (param==="runway") return val + "y";
  if (param==="vol_ratio") return val.toFixed(1) + "×";
  if (["rev_growth","gross_margin","net_debt_assets","dist_52w","rd_intensity","dilution"].indexOf(param) >= 0) return (val*100).toFixed(0) + "%";
  return String(val);
}}

function setThr(param, val) {{
  THRESHOLDS[param] = val;
  var label = document.getElementById("th-" + param);
  if (label) label.textContent = fmtThr(param, val);
  var cfg = SLIDERS.filter(function(s){{ return s.param===param; }})[0];
  if (cfg) {{
    var disp = val * cfg.scale;
    var sEl = document.getElementById(cfg.s), nEl = document.getElementById(cfg.n);
    if (sEl) sEl.value = disp;
    if (nEl) nEl.value = disp;
  }}
  render();
}}

function setThrNum(param, disp, scale) {{
  var raw = parseFloat(disp);
  if (isNaN(raw)) return;
  setThr(param, raw / scale);
}}

function resetThr() {{
  THRESHOLDS = Object.assign({{}}, DEFAULTS);
  SLIDERS.forEach(function(cfg){{
    var disp = DEFAULTS[cfg.param] * cfg.scale;
    var sEl = document.getElementById(cfg.s), nEl = document.getElementById(cfg.n);
    if (sEl) sEl.value = disp;
    if (nEl) nEl.value = disp;
    var label = document.getElementById("th-" + cfg.param);
    if (label) label.textContent = fmtThr(cfg.param, DEFAULTS[cfg.param]);
  }});
  TOGGLES.forEach(function(t){{
    document.getElementById(t.id).checked = DEFAULTS[t.param];
  }});
  render();
}}

function evalSub(sub) {{
  // true / false / null (unknown: an input is missing). A switched-off rule counts as passed, as before.
  var v = sub.value, op = sub.op;
  if (op === "pair") {{
    if (THRESHOLDS.accelerating === false) return true;
    return (Array.isArray(v) && v[0] != null && v[1] != null) ? v[0] > v[1] : null;
  }}
  if (op === "bool") {{
    if (THRESHOLDS.above_sma50 === false) return true;
    return v == null ? null : v === true;
  }}
  if (op === "gt") {{
    if (THRESHOLDS.fcf === false) return true;
    return v == null ? null : v > 0;
  }}
  if (v == null) return null;
  var t = THRESHOLDS[sub.param];
  if (op === "gte") return v >= t;
  if (op === "lte") return v <= t;
  return sub.passed;
}}

function rescore(c) {{
  var signals = c.signals.map(function(s) {{
    var passed = 0, known = 0;
    var subs = s.subs.map(function(sub) {{
      var p = evalSub(sub);
      if (p === true) passed++;
      if (p !== null) known++;
      return {{ label:sub.label, value:sub.value, unit:sub.unit, threshold:sub.threshold, op:sub.op, param:sub.param, passed:p }};
    }});
    return {{ key:s.key, name:s.name, passed:passed, known:known, total:s.total, subs:subs }};
  }});
  var total_points = 0;
  signals.forEach(function(s){{ total_points += s.passed; }});
  return Object.assign({{}}, c, {{
    signals: signals, total_points: total_points,
    tech_breakthrough: (c.rd_intensity != null && c.rd_intensity >= THRESHOLDS.rd_intensity),
    dilution_warning: (c.dilution_yoy != null && c.dilution_yoy > THRESHOLDS.dilution)
  }});
}}

function render() {{
  var scored = DATA.map(rescore);
  var q = document.getElementById("q").value.trim().toLowerCase();
  var out = scored.filter(function(c){{
    if(!tierPass(c)) return false;
    if(PT!=="all" && pointBucket(c.total_points)!==PT) return false;
    if(!pass(c)) return false;
    if(q && (c.ticker.toLowerCase().indexOf(q)<0 && c.name.toLowerCase().indexOf(q)<0)) return false;
    return true;
  }});
  document.getElementById("b-tier3").textContent = DATA.filter(function(c){{ return c.tier===3; }}).length;
  document.getElementById("b-tier2").textContent = DATA.filter(function(c){{ return c.tier===2; }}).length;
  document.getElementById("b-9").textContent = scored.filter(function(c){{ return c.total_points>=9; }}).length;
  document.getElementById("b-87").textContent = scored.filter(function(c){{ return c.total_points>=7 && c.total_points<=8; }}).length;
  document.getElementById("b-65").textContent = scored.filter(function(c){{ return c.total_points>=5 && c.total_points<=6; }}).length;
  document.getElementById("b-lt5").textContent = scored.filter(function(c){{ return c.total_points<5; }}).length;
  document.getElementById("b-disruptive").textContent = scored.filter(function(c){{ return c.themes && c.themes.length; }}).length;
  document.getElementById("b-tech").textContent = scored.filter(function(c){{ return c.tech_breakthrough; }}).length;
  document.getElementById("count").textContent = out.length + " candidates shown";
  var emptyMsg = DATA.length === 0 ? EMPTY_MSG : "No companies match the current filters.";
  document.getElementById("grid").innerHTML = out.length
    ? out.map(cardHtml).join("")
    : '<div class="empty">'+rcEsc(emptyMsg)+'</div>';
}}
function init() {{
  markTier(TIER);
  document.getElementById("grid").innerHTML = '<div class="empty">Loading cards…</div>';
  rcPayload("module_asymmetric_growth").then(function(p) {{
    DATA = (p && p.cards) || [];
    RCF = RCFilters.mount({{root: "#rc-filters", rows: DATA, meta: META, onChange: function(){{ render(); }}}});
    render();
  }}).catch(function(e) {{
    // Never an empty grid: a payload that does not load (or a render error) is said on the board.
    document.getElementById("count").textContent = "";
    document.getElementById("grid").innerHTML = '<div class="empty">'
      + rcEsc("The cards could not be loaded (" + ((e && e.message) || e) + "). Press Refresh to rebuild the board.")
      + '</div>';
  }});
}}
init();
</script>
</body>
</html>"""
