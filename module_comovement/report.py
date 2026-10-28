"""Co-movement board — self-contained dark-theme HTML renderer.

Layout (plan 2026-09-30_plan_filters_ux.md, pilot of the shared filter bar):

* **Header** — what the board is, when the prices end, and always the universe
  funnel: scanned universe (all caps) → ≥ $300M → operating companies → clustered.
* **About this data** — source, date window, what Horizon does, the full
  funnel, and what a group is (and is not).
* **Co-movement settings** — the board's own controls, each with an ⓘ
  explanation beside it: Remove market (one benchmark; SPY/DIA/QQQ are
  collinear), Horizon (overlapping 1/3/5/10-day log returns), Detail (the cut),
  and a Find type-ahead listing every clustered name.
* **Filters · shared across boards** — ``core/static/rc_filters.js``: Index →
  Market cap → Sector (2 levels: sector, industry) → Theme (10-K) → Earnings. Filters
  select groups and dim non-matching members; a card's numbers always describe
  the whole group and say so (decision Q1). Earnings marks names on this board
  unless the user switches it to filter (Q2).

Cards are ordered by residual **tightness** (``mean_corr`` desc), not size — a
size sort used to put the market-beta blob first. On a residual board a group
that is not tighter than the shuffled-returns null has no shared move to show, so
those groups are merged into one openable card at the end (S8, decision D5); the
raw board keeps its beta warning. A card's colour is the *direction* of the last
n days versus the benchmark, not good/bad.
"""
from __future__ import annotations

import html as _html
import json
import re
from datetime import datetime

from RevenueChaser.core.html_safe import fill, script_json
from RevenueChaser.core.reporting_common import (
    FILTERS_CSS,
    FILTERS_JS,
    JOB_JS,
    combobox_html,
    control_html,
    filters_bar_html,
    filters_meta,
    help_html,
    provider_health_html,
    watchlist_html,
)

from RevenueChaser.core.gics_map import sector_path
from RevenueChaser.core.universe import DEFAULT_MC_MIN

from . import config

# A cluster holding more than this share of the visible names is the market
# moving together (beta), not a coherent group. Used on the raw (Off) board only:
# on a residual board the shuffled-returns null decides, per group (S8/D5).
BETA_SHARE = 0.40

# "Nasdaq" here is QQQ = the Nasdaq-100; the Index filter's "Nasdaq-listed" is
# exchange membership (decision Q5), so the two never share a bare name.
BENCHMARK_LABELS = {"off": "Off", "spy": "S&P 500", "dia": "Dow", "qqq": "Nasdaq-100"}
BENCHMARK_ETFS = {"off": "", "spy": "SPY", "dia": "DIA", "qqq": "QQQ"}

# "What a group is — and is not" (the About panel). The last sentences say what
# ``r`` is compared against, what the persistence badges mean and why a chip is
# dimmed, which the card otherwise leaves to guesswork.
HELP_BODY = (
    "Stocks in the same group <b>moved together in the past</b> (their returns "
    "were similar once the market is removed). That does <b>not</b> mean they do "
    "the same business — rivals and suppliers can land in one group; business "
    "similarity lives on <b>Relatedness</b>. Use <b>Coarse</b> for the big picture "
    "and <b>Fine</b> for tighter, more specific groups. A group is named only when "
    "one industry (else a theme) is over-represented against all grouped names at "
    "that level: at least 4 names and 25% of the group, at least 3× its share at "
    "that level, and significant after correcting for the many groups tested "
    "(BH q < 0.01). The label shows that share and the ×lift; otherwise the group "
    "stays a neutral “Cluster N”. "
    "r is compared to the groups the same method finds in shuffled, pure-noise "
    "returns. “Held out-of-sample” means the matching group, formed on data "
    "ending a quarter ago, still moved together over that quarter, which it "
    "never saw; “new this quarter” means no group a quarter ago shared half its "
    "names. Dashed names joined the group since then."
)

CSS = """
:root{--bg:#05111F;--panel:#12161e;--panel2:#1a1f2b;--border:#232a38;
--text:#d7dce5;--muted:#8a93a6;--accent:#0EA5E9;--green:#2ecc71;--amber:#f1c40f;
--neon:#00e6a8;--down:#f87171;--focus:#4F9DFF;--input:#10151f;--input-b:#2a3345;
--seg1:#0EA5E9;--seg2:#a78bfa;--seg3:#5b6478;--seg4:#3a4150}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5}
body.loading:after{content:"Refreshing…";position:fixed;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(5,17,31,.72);color:var(--text);font-weight:700;letter-spacing:.04em;z-index:99}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.wrap{max-width:100%;margin:0 auto;padding:20px 24px 60px}
.num,.r{font-variant-numeric:tabular-nums}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
button{font:inherit;color:inherit}
.topbar{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:10px;border-bottom:1px solid var(--border);padding-bottom:12px;margin-bottom:12px}
.topbar h1{margin:0;font-size:22px;letter-spacing:-.4px}
.topbar .sub{color:var(--muted);margin-top:2px;font-size:13px}
.topbar .sub b{color:var(--text);font-weight:650}
.topbar .limits{color:var(--muted);margin-top:3px;font-size:12px}
.sync{color:var(--muted);font-size:12.5px;white-space:nowrap}
.sync b{color:var(--text)}
.universe{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:6px;font-size:12.5px;color:var(--muted)}
.universe .step{background:var(--panel);border:1px solid var(--border);border-radius:99px;padding:1px 9px}
.universe .step b{color:var(--text)}
.universe .arrow{opacity:.6}
details.about{background:var(--panel);border:1px solid var(--border);border-radius:10px;margin-bottom:12px;font-size:12.5px;color:var(--muted)}
details.about>summary{cursor:pointer;padding:9px 14px;font-weight:600;color:var(--accent)}
.about-body{padding:0 14px 12px;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px 24px;line-height:1.6}
.about-body h3{margin:0 0 2px;font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--text)}
.about-body code{color:var(--neon)}
.about-body b{color:var(--text)}
.funnel{display:flex;flex-direction:column;gap:3px}
.funnel .row{display:flex;justify-content:space-between;gap:10px;border-bottom:1px dashed var(--border);padding:2px 0}
.funnel .row .num{color:var(--text)}
.funnel .row.minus .num{color:var(--down)}
.funnel .row.total{border-bottom:none;font-weight:700;color:var(--text)}
.funnel .row.note{color:var(--amber)}
.funnel .row.note .num{color:var(--amber)}
.funnel .row.note a{color:var(--accent)}
.alert{border-radius:10px;padding:8px 12px;margin-bottom:10px;font-size:12.5px;background:rgba(241,196,15,.10);border:1px solid rgba(241,196,15,.35);color:var(--amber)}
.alert b{color:var(--text)}
.warn{background:rgba(241,196,15,.10);border:1px solid rgba(241,196,15,.35);color:var(--amber);border-radius:10px;padding:9px 14px;margin-bottom:10px;font-size:12.5px;display:none}
.strip{background:var(--panel);border:1px solid var(--accent);border-radius:8px;padding:7px 12px;margin-bottom:10px;font-size:12.5px;display:none}
.oos{font-size:12.5px;color:var(--muted);margin:0 0 10px;display:flex;gap:6px;align-items:center;flex-wrap:wrap;position:relative}
.oos[hidden]{display:none}
.count{color:var(--muted);font-size:12px;white-space:nowrap;align-self:center}
.onlym{display:inline-flex;gap:6px;align-items:center;color:var(--muted);font-size:12px;align-self:center;cursor:pointer}
.onlym input{accent-color:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:12px}
@media (max-width:520px){.grid{grid-template-columns:1fr}.wrap{padding:16px}}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:9px;transition:box-shadow .2s ease-out}
.card.tight{border-color:rgba(14,165,233,.45)}
.card.quiet{border-style:dashed}
.card.flash{box-shadow:0 0 0 2px var(--accent)}
.chead{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:baseline;justify-content:space-between}
.chead .left{display:flex;gap:6px;align-items:baseline;flex-wrap:wrap}
.clabel{font-weight:750;font-size:15px;letter-spacing:-.2px}
.meta{color:var(--muted);font-size:11.5px}
.match{font-size:11px;color:var(--accent);border:1px solid rgba(14,165,233,.4);border-radius:99px;padding:0 7px}
.badge{background:var(--panel2);border:1px solid var(--border);border-radius:99px;padding:0 7px;font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.badge.ok{color:var(--neon);border-color:rgba(0,230,168,.35)}
.badge.new{color:var(--amber);border-color:rgba(241,196,15,.35)}
.stats{display:grid;grid-template-columns:auto 1fr;gap:6px 10px;align-items:start;font-size:12px;margin:0}
.stats dt{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;line-height:22px}
.stats dd.col{flex-direction:column;align-items:flex-start;gap:2px}
.rscale{color:var(--muted);font-size:10.5px;font-variant-numeric:tabular-nums}
.stats dd{margin:0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-height:22px}
.rbar{position:relative;flex:1;min-width:90px;height:6px;background:#1c2430;border-radius:99px}
.rbar>i{position:absolute;left:0;top:0;bottom:0;background:var(--accent);border-radius:99px}
.rbar>s{position:absolute;top:-3px;width:2px;height:12px;background:var(--amber);border-radius:1px}
.dir.up{color:var(--neon)}
.dir.dn{color:var(--down)}
.comp{display:flex;flex-direction:column;gap:6px}
.compbar{display:flex;height:10px;border-radius:99px;overflow:hidden;gap:2px}
.compbar span{height:100%}
.legend{display:flex;flex-wrap:wrap;gap:4px}
.seg-btn{display:inline-flex;align-items:center;gap:6px;min-height:26px;padding:0 9px;background:var(--panel2);border:1px solid var(--border);border-radius:99px;cursor:pointer;font-size:12px;color:var(--text)}
.seg-btn .sw{width:8px;height:8px;border-radius:2px;flex:none}
.seg-btn .n{font-weight:700}
.seg-btn[aria-expanded=true]{border-color:var(--accent);background:rgba(14,165,233,.1)}
.seg-btn.muted{color:var(--muted)}
.seg-panel{background:var(--input);border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:12px}
.seg-panel[hidden]{display:none}
.seg-panel h5{margin:0 0 6px;font-size:12px;color:var(--text)}
.sub-grp{margin:4px 0 8px}
.sub-grp>div:first-child{color:var(--muted);font-size:11.5px;margin-bottom:3px;display:flex;justify-content:space-between;gap:8px}
.namelist{display:flex;flex-wrap:wrap;gap:4px 12px;color:var(--text)}
.namelist b{margin-right:4px}
.linkbtn{background:none;border:0;color:var(--accent);cursor:pointer;font-size:12px;padding:2px}
.linkbtn:hover{text-decoration:underline}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{position:relative;background:var(--panel2);border:1px solid var(--border);border-left:3px solid var(--seg3);border-radius:6px;padding:0 7px;height:24px;font-size:12px;font-weight:600;white-space:nowrap;cursor:default;color:var(--text)}
.chip.fringe{opacity:.55;border-style:dashed}
.chip.dim{opacity:.28}
.chip.hit{box-shadow:0 0 0 2px var(--accent)}
.chip.earn::after{content:"";position:absolute;top:-3px;right:-3px;width:7px;height:7px;border-radius:50%;background:var(--amber)}
.tip{position:fixed;z-index:60;pointer-events:none;background:#0d1320;border:1px solid var(--border);border-radius:8px;box-shadow:0 10px 24px rgba(0,0,0,.5);padding:7px 10px;font-size:12px;line-height:1.45;max-width:320px;color:var(--muted)}
.tip b{color:var(--text)}
.tip[hidden]{display:none}
details.members summary{cursor:pointer;color:var(--accent);font-size:12px}
.cfoot{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:11.5px;color:var(--muted)}
.empty{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:24px;color:var(--muted);text-align:center;grid-column:1/-1}
.rcf-boardrow .rcf-seg [role=radio] .hint{font-weight:500;font-size:11px;color:var(--muted);margin-left:3px}
.rcf-boardrow .rcf-seg [aria-checked=true] .hint{color:var(--neon)}
.lockmsg{margin-top:4px;font-size:11.5px;color:var(--amber);max-width:260px;line-height:1.35}
.custombox{display:flex;align-items:center;gap:8px;flex-wrap:wrap;align-self:center}
.custombox .badge.custom{color:var(--amber);border-color:rgba(241,196,15,.4)}
.custombox .resetmsg{color:var(--muted);font-size:12px}
"""

_CUT_WORDS = ("Coarse", "Medium", "Fine")

# Window buttons: short label, with the session count in the tooltip.
_WINDOW_LABELS = {21: "1m", 42: "2m", 126: "6m", 189: "9m", 252: "1y", 378: "1.5y"}
_LOCK_REASON = ("Short windows use 1-day returns only "
                "(too few independent observations)")


def _eff(payload: dict) -> dict:
    """The inputs this board was computed with, from the payload (never from
    ``config``): ``payload.settings`` carries the effective values. The config
    constants are read only for a payload cached before #40 (no ``settings``)."""
    s = payload.get("settings") or {}
    window = payload.get("window") or s.get("window") or config.WINDOW
    return {
        "window": int(window),
        "options": [int(w) for w in (s.get("window_options") or config.WINDOW_OPTIONS)],
        "min_obs": int(s.get("min_obs") or (payload.get("funnel") or {}).get("min_obs")
                       or config.MIN_OBS),
        "oos_holdout": int(s.get("oos_holdout") or config.OOS_HOLDOUT),
        "locked": bool(payload.get("horizon_locked", s.get("horizon_locked", False))),
        "custom": bool(s.get("custom")),
    }


def _friendly(as_of: str) -> str:
    try:
        dt = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        return dt.strftime("%A, %B %d, %Y EOD")
    except (ValueError, TypeError):
        return str(as_of or "")


def _resolve(payload: dict) -> tuple[int, str]:
    horizon = payload.get("horizon")
    horizon = int(horizon) if horizon in config.HORIZONS else config.DEFAULT_HORIZON
    benchmark = str(payload.get("benchmark") or config.DEFAULT_BENCHMARK).lower()
    if benchmark not in config.BENCHMARKS:
        benchmark = config.DEFAULT_BENCHMARK
    return horizon, benchmark


def _radios(label_id: str, name: str, options: list[tuple[str, str, str]],
            selected: str, extra: dict | None = None) -> str:
    """An APG radiogroup (roving tabindex; arrows move) — ``RCRadios.wire``.

    ``extra`` maps an option value to ``{"title": str, "disabled": bool}``: a
    tooltip, and ``aria-disabled`` (the shared wiring skips those buttons).
    """
    out = []
    for value, text, hint in options:
        on = value == selected
        ex = (extra or {}).get(value) or {}
        h = f'<span class="hint">{_html.escape(hint)}</span>' if hint else ""
        title = f' title="{_html.escape(ex["title"], quote=True)}"' if ex.get("title") else ""
        dis = ' aria-disabled="true"' if ex.get("disabled") else ""
        out.append(f'<button role="radio" type="button" data-v="{_html.escape(value, quote=True)}" '
                   f'aria-checked="{"true" if on else "false"}" tabindex="{0 if on else -1}"'
                   f'{dis}{title}>'
                   f'{_html.escape(text)}{h}</button>')
    return (f'<div class="rcf-seg" role="radiogroup" aria-labelledby="{label_id}" '
            f'data-group="{name}">{"".join(out)}</div>')


def _no_shared_move(cluster: dict, applied: bool) -> bool:
    """True when a residual group has no shared move to show.

    Only an explicit ``significant is False`` counts as "at chance". A run
    persisted before S8 has no baseline, so its ``significant`` is ``None`` —
    *not judged* — and the card stays: the board must not hide names it cannot
    judge. A one-name group is different: it cannot co-move with anything, so
    it goes with the rest even when the numbers say nothing (S11).
    """
    if not applied:
        return False
    if cluster.get("n") == 1:
        return True
    return cluster.get("significant") is False


def _member_rows(payload: dict, applied: bool) -> list[dict]:
    """One row per (cut, member): the spine contract plus the group's numbers."""
    rows = []
    for cut, clusters in (payload.get("clusters") or {}).items():
        # S12: the catch-all share is of the whole cut, not of the filtered
        # view, so it is fixed here rather than recounted in the browser.
        total = sum(int(cl.get("n") or len(cl.get("members") or [])) for cl in clusters)
        for cl in clusters:
            catchall = bool(cl.get("catchall"))
            pct = (round(100.0 * float(cl.get("n") or 0) / total)
                   if catchall and total else None)
            for m in cl.get("members", []):
                # The filter path: [sector, industry]. The engine's rows carry it already
                # (core.universe.filter_fields); a row without one gets the same path built here.
                path = m.get("gics") or sector_path(m.get("sector"), m.get("industry"))
                rows.append({
                    "cut": str(cut),
                    "cluster_id": f"{cut}:{cl['id']}",
                    "cluster_label": cl.get("label") or f"Cluster {cl['id']}",
                    "cluster_n": cl.get("n"),
                    "mean_corr": cl.get("mean_corr"),
                    "baseline_r": cl.get("baseline_r"),
                    "significant": cl.get("significant"),
                    "persist": cl.get("persist"),
                    "no_shared": _no_shared_move(cl, applied),
                    "catchall": catchall,
                    "catchall_pct": pct,
                    "last_residual_pct": cl.get("last_residual_pct"),
                    "ticker": m.get("ticker"),
                    "name": m.get("name") or m.get("ticker"),
                    "sector": sector_path(m.get("sector"), m.get("industry"))[0],
                    "industry": m.get("industry") or "",
                    "gics": path,
                    "theme_ids": m.get("theme_ids") or [],
                    "core": m.get("core"),
                    "market_cap": m.get("market_cap"),
                    "earnings_date": m.get("earnings_date"),
                    "index": m.get("index") or [],
                })
    return rows


# The fields of a member row that are its group's (#379 COM-3): the page gets them once per group.
_GROUP_KEYS = ("cut", "cluster_id", "cluster_label", "cluster_n", "mean_corr", "baseline_r", "significant",
               "persist", "no_shared", "catchall", "catchall_pct", "last_residual_pct")


def _pack_rows(rows: list[dict]) -> dict:
    """``_member_rows`` as the page gets them (#379 COM-3): ``rcUnpackRows`` rebuilds the same rows in the browser.

    Each group's fields once (``groups``, keyed by ``cluster_id``), each distinct name record once (``names``: a
    name sits in every cut), one ``[cluster_id, name index, core]`` triple per member, and the row ``keys`` in
    their order. The rows used to repeat all of it once per (cut, member).
    """
    groups: dict[str, dict] = {}
    names: list[dict] = []
    index: dict[str, int] = {}
    members = []
    for r in rows:
        gid = r["cluster_id"]
        groups.setdefault(gid, {k: r[k] for k in _GROUP_KEYS})
        name = {k: v for k, v in r.items() if k not in _GROUP_KEYS and k != "core"}
        key = json.dumps(name, sort_keys=True, default=str)
        if key not in index:
            index[key] = len(names)
            names.append(name)
        members.append([gid, index[key], r["core"]])
    return {"keys": list(rows[0]) if rows else [], "groups": groups, "names": names, "members": members}


def _n(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "—"


_JS_ID = re.compile(r"[A-Za-z0-9_-]+")


def _note_link(link: dict) -> str:
    """A funnel note's pointer to where its cause is shown (``link`` from ``core.funnel``).

    Inside the dashboard ``rcOpenTab`` switches to ``link["tab"]`` and lands on
    ``link["anchor"]`` in that tab; opened alone, the href opens the dashboard on the tab.
    Tab and anchor go into an inline handler, so anything but a plain id renders no link.
    """
    tab, anchor = str(link.get("tab") or ""), str(link.get("anchor") or "")
    if not _JS_ID.fullmatch(tab):
        return ""
    if not _JS_ID.fullmatch(anchor):
        anchor = ""
    text = _html.escape(str(link.get("label") or tab))
    return (f' <span class="meta">— see <a href="/#{tab}" target="_top" '
            f"onclick=\"return rcOpenTab('{tab}', '{anchor}')\">{text}</a></span>")


def _funnel_rows(payload: dict, f: dict, floor_txt: str, eff: dict, scanned,
                 clustered) -> list[tuple[str, str, int]]:
    """Rows of the About funnel: ``(css class, label HTML, count)``.

    From ``funnel_steps`` when the run has them (so the short-history split —
    *Listed recently* by design, *Missing price bars* fixable by Fetch — reads
    exactly as on the Settings card); the legacy drop counts otherwise.
    """
    steps = payload.get("funnel_steps") or []
    if steps:
        rows = []
        for st in steps:
            label = _html.escape(str(st.get("label") or st.get("key") or ""))
            n = int(st.get("count") or 0)
            kind = st.get("kind")
            if kind == "note":
                # Shown, never counted: a positive number and a pointer to the cause.
                rows.append(("note", f"⚠ {label}" + _note_link(st.get("link") or {}), n))
            elif kind == "drop":
                if st.get("by_design"):
                    label += ' <span class="meta">(by design)</span>'
                elif st.get("fix"):
                    label += (' <span class="meta">— '
                              f'{_html.escape(str(st["fix"].get("label") or "Fetch"))} '
                              'can fix this</span>')
                rows.append(("minus", label, -n))
            else:
                rows.append(("total" if kind == "result" else "", label, n))
        return rows
    rows = []
    if scanned is not None:
        rows.append(("", "Scanned universe (<code>equity_universe</code>, all caps)", scanned))
    if f.get("below_floor"):
        rows.append(("minus", f"Below the {floor_txt} classification floor", -f["below_floor"]))
    if f.get("no_market_cap"):
        rows.append(("minus", "No market cap on record", -f["no_market_cap"]))
    for key, label in (("no_prices", "No cached prices"),
                       ("short_history",
                        f"Short history (&lt; {eff['min_obs']} of {eff['window']} sessions)"),
                       ("flat", "Flat / halted prints")):
        if f.get(key):
            rows.append(("minus", label, -f[key]))
    rows.append(("total", "Clustered", clustered))
    return rows


def _about(payload: dict, horizon: int, benchmark: str, applied: bool,
           eff: dict) -> tuple[str, str]:
    """(header funnel line, "About this data" panel)."""
    f = dict(payload.get("funnel") or {})
    # A payload cached before the funnel existed still carries the drop lists.
    for key, lst in (("no_prices", "no_prices"), ("short_history", "dropped"),
                     ("flat", "flat_prints")):
        if key not in f and payload.get(lst):
            f[key] = len(payload[lst])
    if "classification_total" not in f and payload.get("universe_total"):
        f["classification_total"] = payload["universe_total"]
    src = payload.get("source") or {}
    scanned, op_n = f.get("scanned_total"), f.get("classification_total")
    # A payload from before the security-type filter has no ``floor_total``: its
    # "≥ floor" step is then ``classification_total`` and there is no
    # "operating companies" step.
    floor_n = f.get("floor_total")
    if floor_n is None:
        floor_n, op_n = op_n, None
    clustered = f.get("clustered", payload.get("eligible"))
    floor = src.get("floor_mc", DEFAULT_MC_MIN)
    floor_txt = f"${floor / 1e9:.1f}B" if floor >= 1e9 else f"${floor / 1e6:.0f}M"
    steps = []
    if scanned is not None:
        steps.append(f'<span class="step">Scanned universe <b class="num">{_n(scanned)}</b> · all market caps</span>')
    if floor_n is not None:
        steps.append(f'<span class="step">≥ {floor_txt} <b class="num">{_n(floor_n)}</b></span>')
    if op_n is not None:
        steps.append(f'<span class="step">operating companies <b class="num">{_n(op_n)}</b></span>')
    steps.append(f'<span class="step">clustered <b class="num">{_n(clustered)}</b></span>')
    funnel_line = ('<div class="universe" aria-label="Universe">'
                   + '<span class="arrow" aria-hidden="true">→</span>'.join(steps)
                   + '<span class="step" id="showing-step" style="display:none">showing '
                     '<b class="num" id="showing-n"></b></span>'
                   + '<button class="linkbtn" type="button" onclick="openAbout()">'
                     f'why {_n(clustered)}?</button></div>')

    rows = _funnel_rows(payload, f, floor_txt, eff, scanned, clustered)
    funnel = "".join(f'<div class="row {c}"><span>{lab}</span><span class="num">'
                     f'{("−" + _n(-v)) if isinstance(v, int) and v < 0 else _n(v)}</span></div>'
                     for c, lab, v in rows)

    ws = _html.escape(str(payload.get("window_start") or "?"))
    we = _html.escape(str(payload.get("window_end") or payload.get("prices_through") or "?"))
    oos = payload.get("oos_start")
    etf = BENCHMARK_ETFS.get(benchmark) or ""
    period = _html.escape(str(src.get("period", config.OHLC_PERIOD)))
    about = (
        '<details class="about" id="about"><summary>About this data — source, dates, '
        'horizon, universe</summary><div class="about-body">'
        '<div><h3>Where the prices come from</h3>Daily closes from the shared daily bars '
        '(<code>prices.duckdb → price_bars_daily</code>), which the nightly <b>Daily Bars</b> job '
        'downloads (Unusual Whales, Yahoo Finance as the fallback). Splits and dividends are applied '
        f'on read (adjusted closes); the board reads the last <b>{period}</b>. Neither <b>Fetch</b> nor '
        '<b>Refresh</b> downloads anything: Fetch stores a dated run, Refresh re-clusters the stored '
        'prices. A name the daily bars do not hold yet shows under no prices.</div>'
        f'<div><h3>Date range used</h3>Window <b class="num">{ws} → {we}</b> '
        f'({eff["window"]} trading sessions).'
        + (f' Benchmark <b>{etf}</b> is removed over the same window.' if applied and etf else '')
        + (f' Out-of-sample check: groups formed on the window ending <b class="num">'
           f'{_html.escape(str(oos))}</b>, tested on the <b>{eff["oos_holdout"]} sessions</b> '
           f'after it.' if oos else '')
        + '</div>'
        f'<div><h3>Horizon</h3>Each name\'s return over <b>n trading days</b>, measured every '
        f'day (overlapping). <b>1d</b> = daily moves (default; most observations, most stable '
        f'groups). Longer horizons catch slower joint drifts but hold only about '
        f'{eff["window"]} ÷ n independent observations, so groups are noisier. Now: '
        f'<b>{horizon}d</b>.</div>'
        f'<div id="about-funnel"><h3>Universe → clustered names</h3><div class="funnel">{funnel}</div></div>'
        f'<div><h3>What a group is — and is not</h3>{HELP_BODY}</div>'
        '</div></details>')
    return funnel_line, about


def _custom_html(eff: dict) -> str:
    """"Custom settings" badge + Reset: in the board settings row (AGENTS.md section 6
    keeps the top bar to title, subtitle and Last sync). Only when something is
    overridden in Settings."""
    if not eff["custom"]:
        return ""
    return ('<span class="custombox"><span class="badge custom" id="custom-badge" '
            'title="Some inputs differ from the defaults (Settings → Board inputs)">'
            'Custom settings</span>'
            '<button class="linkbtn" type="button" id="reset-settings">Reset to defaults</button>'
            '<span class="resetmsg" id="reset-msg" aria-live="polite"></span></span>')


def render_comovement_dashboard(payload: dict) -> str:
    payload = payload or {}
    cuts = [str(c) for c in (payload.get("cuts") or [])]
    cut_words = [_CUT_WORDS[i] if i < len(_CUT_WORDS) else f"Level {i + 1}"
                 for i in range(len(cuts))]
    horizon, benchmark = _resolve(payload)
    eff = _eff(payload)
    applied = bool(payload.get("benchmark_applied")) and benchmark != "off"
    members = _member_rows(payload, applied)
    sync = _friendly(payload.get("prices_through") or payload.get("as_of"))

    if not members:
        grid = ('<div class="empty">No clusters yet. The nightly <b>Daily Bars</b> job fills the daily '
                'prices; run <b>Fetch</b> to cluster them.</div>')
    else:
        grid = ""

    # S11: the one-line statement of what this board does and does not claim.
    # ``n_factors`` (eigenvalues above the Marchenko–Pastur edge) is missing on
    # a run persisted before 2026-10-01 — keep the claim, drop the number
    # rather than inventing one.
    try:
        n_fac_txt = f"{int(payload.get('n_factors'))} common moves above noise · "
    except (TypeError, ValueError):
        n_fac_txt = ""
    limits = f"Groups, not pairs · {n_fac_txt}membership moves week to week"

    etf, blabel = BENCHMARK_ETFS[benchmark], BENCHMARK_LABELS[benchmark]
    # Warnings are state, not explanations: always visible (visibility of status).
    alerts = []
    if applied and payload.get("benchmark_status") == "stale":
        w_end = _html.escape(str(payload.get("window_end")))
        p_thr = _html.escape(str(payload.get("prices_through")))
        alerts.append(f"<b>{etf} ends {w_end}</b>, earlier than the other prices ({p_thr}). "
                      f"Residual groups and the last-{horizon}d move stop at {w_end}. "
                      f"The nightly <b>Daily Bars</b> job fills it; <b>Refresh</b> then re-clusters.")
    if applied:
        sub = (f"Groups of stocks whose returns moved together <b>after removing "
               f"{blabel}</b> ({etf}) · {horizon}-day overlapping returns · "
               f"last {eff['window']} sessions")
    elif benchmark != "off":
        sub = (f"Remove market unavailable ({etf} has no usable prices) · "
               f"{horizon}-day overlapping returns")
        alerts.append(f"<b>{etf} has no usable prices in the store</b>, so the board fell "
                      f"back to raw returns (market still inside the groups). The nightly "
                      f"<b>Daily Bars</b> job adds it; <b>Refresh</b> then re-clusters.")
    else:
        sub = (f"Raw return clusters · {horizon}-day overlapping returns — market still "
               f"inside the groups")
        alerts.append("<b>Remove market is off.</b> This is the old board: the correlation "
                      "still contains beta. The largest card is usually the market moving, "
                      "not a theme. Switch <b>Remove market</b> to S&amp;P 500 (or Dow / "
                      "Nasdaq-100) to see idiosyncratic groups.")
    note = payload.get("short_window_note")
    if note:
        alerts.append(f"<b>Short window:</b> {_html.escape(str(note))}")
    vf = payload.get("verdicts_from")
    if vf:
        alerts.append(f"Significance and out-of-sample verdicts are from the Fetch on prices "
                      f"through <b>{_html.escape(str(vf))}</b>; groups were re-clustered on "
                      f"newer prices. Run <b>Fetch</b> to recompute them.")
    alerts_html = "".join(f'<div class="alert" role="status">{a}</div>' for a in alerts)
    funnel_line, about = _about(payload, horizon, benchmark, applied, eff)
    help_block = help_html(
        "Each group is a set of stocks whose daily returns moved together over the window. "
        "<b>Remove market</b> takes out the move of SPY, DIA or QQQ first, so a group is a shared "
        "story rather than the whole market going up or down. The <b>About this data</b> box below "
        "gives the source, dates and universe. Prices are the shared daily bars the nightly "
        "<b>Daily Bars</b> job stores. <b>Fetch</b> clusters them and stores a dated run; <b>Refresh</b> "
        "re-clusters the stored prices. Neither makes a network call.")

    market_now = (f"Now: residual vs <b>{etf}</b>. Nasdaq-100 here is QQQ, not the "
                  f"<b>Nasdaq-listed</b> index filter below." if applied
                  else "Now: <b>off</b> — raw returns, market still inside the groups.")
    board_row = (
        '<section class="rcf-boardrow" aria-labelledby="board-title">'
        '<h2 class="rcf-title" id="board-title">Co-movement settings <span class="rcf-rule"></span></h2>'
        '<div class="rcf-ctrls">'
        + control_html(
            "market", "Remove market",
            _radios("rcb-l-market", "market",
                    [(k, BENCHMARK_LABELS[k], BENCHMARK_ETFS[k]) for k in ("off", "spy", "dia", "qqq")],
                    benchmark),
            "Remove market",
            "Each stock's return is regressed on one index ETF (one factor, OLS) and only the "
            "<b>leftover</b> is clustered. Without it most stocks correlate simply because the "
            "whole market moves, and the biggest group is just “the market”. Groups "
            "can rally or sell off together; tightness is not “up is good”, and a "
            "card's colour is only the direction versus the ETF.",
            market_now)
        + control_html(
            "horizon", "Horizon",
            _radios("rcb-l-horizon", "horizon",
                    [(str(n), f"{n}d", "") for n in config.HORIZONS], str(horizon),
                    {str(n): {"title": _LOCK_REASON, "disabled": True}
                     for n in config.HORIZONS if n != 1} if eff["locked"] else None)
            + (f'<div class="lockmsg" id="horizon-lock">ⓘ {_html.escape(_LOCK_REASON)}</div>'
               if eff["locked"] else ""),
            "Horizon",
            "The length of the return that is compared: <b>1d</b> compares daily moves, "
            "<b>5d</b> compares overlapping 5-day moves. Prices are always daily closes. "
            f"Longer horizons have fewer independent data points (≈{eff['window']} ÷ n), so "
            "groups are less stable. Also sets the “last n days” move on each card.",
            f"Now: <b>{horizon}-day</b> returns over {eff['window']} sessions.")
        + control_html(
            "window", "Window",
            _radios("rcb-l-window", "window",
                    [(str(w), _WINDOW_LABELS.get(w, f"{w}d"), "") for w in eff["options"]],
                    str(eff["window"]),
                    {str(w): {"title": f"{w} trading sessions"} for w in eff["options"]}),
            "Window",
            "How many trading days of returns the groups are built from: <b>1m</b> = 21, "
            "<b>2m</b> = 42, <b>6m</b> = 126, <b>9m</b> = 189, <b>1y</b> = 252, "
            "<b>1.5y</b> = 378. A short window follows what is moving now but is noisy; a "
            "long one is steadier but slower to notice a change. Under 6 months the "
            "Horizon is fixed at 1 day and the out-of-sample test is not run. Changing it "
            "recomputes the board from stored prices (no download).",
            f"Now: <b>{eff['window']} sessions</b>.")
        + control_html(
            "cut", "Detail",
            '<div class="rcf-seg" role="radiogroup" aria-labelledby="rcb-l-cut" data-group="cut" id="cut-seg"></div>',
            "Detail",
            "How many groups the same correlation tree is cut into. <b>Coarse</b> = broad "
            "blocks; <b>Fine</b> = small, tight groups. The names are the same at every "
            "level; only the grouping changes. This is price behaviour, not the Sector or "
            "Theme level below.")
        + control_html(
            "find", "Find ticker or company",
            combobox_html("find", "rcb-l-find", "Type a ticker or name — e.g. AEM or Agnico"),
            "Find",
            "Lists every clustered name that passes the filters below. Type to narrow — "
            "ticker matches rank above name matches. <b>↑ ↓</b> move, <b>Enter</b> jumps to "
            "that name's group, <b>Esc</b> closes.")
        + _custom_html(eff)
        + '<label class="onlym"><input type="checkbox" id="only-m"> Only matching names</label>'
        + '<span class="count" id="count" aria-live="polite"></span>'
        '</div></section>')

    meta = filters_meta(
        "comovement",
        floor_mc=(payload.get("source") or {}).get("floor_mc", DEFAULT_MC_MIN),
        earnings_mode="mark",
        themes=payload.get("theme_catalog") or {},
    )

    JS = r"""
function rcUnpackRows(P){
  return P.members.map(function(m){
    var g = P.groups[m[0]], n = P.names[m[1]], r = {};
    P.keys.forEach(function(k){ r[k] = k === "core" ? m[2] : (Object.prototype.hasOwnProperty.call(g, k) ? g[k] : n[k]); });
    return r;
  });
}
var DATA = rcUnpackRows(__DATA__);
var CUTS = __CUTS__;
var CUT_WORDS = __CUT_WORDS__;
var BETA_SHARE = __BETA__;
var HORIZON = __HORIZON__;
var WINDOW = __WINDOW__;
var LOCKED = __LOCKED__;
var MARKET = __MARKET__;
var APPLIED = __APPLIED__;
var OOS = __OOS__;
var META = __META__;
var ETF = __ETF__;
var BOARD_KEY = "rc.board.comovement.v1";
var BOARD = {cut: CUTS.length ? CUTS[0] : "", only: false};
try { var _b = JSON.parse(localStorage.getItem(BOARD_KEY) || "null"); if(_b){ if(CUTS.indexOf(_b.cut) >= 0) BOARD.cut = _b.cut; BOARD.only = !!_b.only; } } catch(e) {}
function saveBoard(){ try { localStorage.setItem(BOARD_KEY, JSON.stringify(BOARD)); } catch(e) {} }
var HIT = null;
var RCF = null;
function isResidual(){ return MARKET !== "off" && APPLIED; }
var fmtCap = RCFilters.core.fmtCap;
function plural(n, word){ return n + " " + word + (n === 1 ? "" : "s"); }
function fmt(n){ return Number(n).toLocaleString("en-US"); }
function cutWord(k){ var i = CUTS.indexOf(String(k)); return (i >= 0 && CUT_WORDS[i]) ? CUT_WORDS[i] : "Level"; }
function announce(msg){ var b = document.getElementById("busy"); if(b) b.textContent = msg || ""; }
function rcOpenTab(tab, anchor){
  // Inside the dashboard: switch its tab and land on `anchor` in that tab's frame (a
  // frame not loaded yet loads straight onto it). Opened alone: the link's href.
  try {
    if(parent !== window && typeof parent.showTab === "function"){
      var f = anchor ? parent.document.querySelector('.frame[data-tab="' + tab + '"]') : null;
      if(f && !f.getAttribute("src")){
        f.setAttribute("data-src", String(f.getAttribute("data-src") || "").split("#")[0] + "#" + anchor);
      }
      parent.showTab(tab);
      var d = f && f.getAttribute("src") ? f.contentDocument : null;
      var el = d && d.getElementById(anchor);
      if(el && el.scrollIntoView) el.scrollIntoView({block: "start"});
      return false;
    }
  } catch(e) {}
  return true;
}
function openAbout(){ var d = document.getElementById("about"); d.open = true; var el = document.getElementById("about-funnel"); if(el && el.scrollIntoView) el.scrollIntoView({block:"nearest", behavior:"smooth"}); }
function boardQuery(over){
  // One builder for every reload: a change to one control carries the other two.
  var q = {window: WINDOW, horizon: HORIZON, benchmark: MARKET};
  for(var k in over) q[k] = over[k];
  return "?refresh=1&window=" + q.window + "&horizon=" + q.horizon + "&benchmark=" + q.benchmark;
}
function setMarket(id){
  if(id === MARKET) return;
  document.body.classList.add("loading");
  announce("Refreshing clusters at " + id + "…");
  location.search = boardQuery({benchmark: id});
}
function setHorizon(n){
  if(Number(n) === HORIZON || LOCKED) return;
  document.body.classList.add("loading");
  announce("Refreshing clusters at " + n + "d…");
  location.search = boardQuery({horizon: n});
}
function setWindow(n){
  if(Number(n) === WINDOW) return;
  document.body.classList.add("loading");
  announce("Recomputing clusters over " + n + " sessions…");
  location.search = boardQuery({window: Number(n)});
}
function resetSettings(btn){
  boardSettingsReset(btn, document.getElementById("reset-msg"), "module_comovement");
}
function setCut(k){ BOARD.cut = String(k); saveBoard(); render(); }
function buildCutSeg(){
  var c = document.getElementById("cut-seg");
  if(!c) return;
  c.innerHTML = CUTS.map(function(k){
    var ids = {}; DATA.forEach(function(x){ if(x.cut === k) ids[x.cluster_id] = 1; });
    var on = k === BOARD.cut;
    return '<button role="radio" type="button" data-v="' + rcEsc(k) + '" aria-checked="' + on + '" tabindex="' + (on ? 0 : -1) + '">' +
      cutWord(k) + '<span class="hint">' + fmt(Object.keys(ids).length) + ' groups</span></button>';
  }).join("");
}
// The unknown bucket: the identity master's "Unclassified" and the legacy "Other" on rows it has not reached.
function isUnk(v){ return v === "Unclassified" || v === "Other"; }
function industryOf(c){
  // The finest usable business label: industry → sector.
  var g = c.gics || [];
  for(var i = g.length - 1; i >= 0; i--){ var v = String(g[i] || "").trim(); if(v && !isUnk(v)) return v; }
  return "";
}
var SEG_COLORS = ["var(--seg1)", "var(--seg2)", "var(--seg3)", "var(--seg4)"];
function composition(items){
  // Segments at ONE level (the industry). Unclassified is its own
  // segment, so the parts always add up to the group's n.
  var by = {}, order = [], none = [];
  items.forEach(function(r){
    var s = r.industry && !isUnk(r.industry) ? r.industry : "";
    if(!s){ none.push(r); return; }
    if(!by[s]){ by[s] = []; order.push(s); }
    by[s].push(r);
  });
  order.sort(function(a, b){ return by[b].length - by[a].length || (a < b ? -1 : 1); });
  var segs = order.slice(0, 2).map(function(k, i){ return {key:"s" + i, label:k, rows:by[k], color:SEG_COLORS[i], subs:[[k, by[k]]]}; });
  var rest = order.slice(2);
  if(rest.length){
    var rows = []; rest.forEach(function(k){ rows = rows.concat(by[k]); });
    segs.push({key:"other", label:"other", rows:rows, color:SEG_COLORS[2], subs:rest.map(function(k){ return [k, by[k]]; }),
               note: rest.length + (rest.length === 1 ? " industry" : " industries")});
  }
  if(none.length) segs.push({key:"none", label:"unclassified", rows:none, color:SEG_COLORS[3], subs:[["No industry", none]], note:"no industry"});
  return segs;
}
function persistTxt(p){
  // Out-of-sample persistence (persistence.py): the best-matching group formed
  // on data ending a quarter ago, and whether it held on that unseen quarter.
  if(!p) return LOCKED ? '<span class="meta">not evaluated (short window)</span>' : '<span class="meta">not checked — run Fetch</span>';
  if(p.new) return '<span class="badge new">new this quarter</span> <span class="meta">no group a quarter ago shared half its names</span>';
  var same = Math.round(Number(p.jaccard || 0) * 100) + '% same names as a quarter ago';
  var nums = (p.oos_r == null || p.oos_base == null) ? "" :
    ' · last quarter r ' + Number(p.oos_r).toFixed(2) + ' vs random names ' + Number(p.oos_base).toFixed(2);
  if(p.held === true) return '<span class="badge ok">✓ held out-of-sample</span> <span class="meta">' + same + nums + '</span>';
  if(p.held === false) return '<span class="badge">✗ did not hold out-of-sample</span> <span class="meta">' + same + nums + '</span>';
  return '<span class="meta">' + same + '</span>';
}
function whyTxt(g){
  if(/^Cluster /.test(g.label))
    return 'No industry or theme is over-represented enough here, so the group keeps a neutral number instead of a made-up label.';
  return 'Named <b>' + rcEsc(g.label) + '</b>: the share and ×lift in the name are against all grouped names at this level (BH q < 0.01).';
}
function chipHtml(r, seg, dim, plain){
  var fringe = !plain && r.core === false;
  var cls = "chip" + (fringe ? " fringe" : "") + (dim ? " dim" : "") + (HIT === r.ticker ? " hit" : "") + (RCF && RCF.mark(r) ? " earn" : "");
  return '<button type="button" class="' + cls + '" style="border-left-color:' + seg.color + '" data-t="' + rcEsc(r.ticker) +
    '" data-g="' + rcEsc(r.cluster_id) + '" aria-label="' + rcEsc(r.ticker + ", " + (r.name || r.ticker)) + '" aria-describedby="tip">' + rcEsc(r.ticker) + '</button>';
}
function cardHtml(g){
  var res = isResidual(), b = ETF, n = g.items.length, filtered = RCF && RCF.active();
  var segs = composition(g.items), segOf = {};
  segs.forEach(function(s){ s.rows.forEach(function(r){ segOf[r.ticker] = s; }); });
  var mset = {}, matched = 0;
  g.items.forEach(function(r){ if(!RCF || RCF.pass(r)){ mset[r.ticker] = 1; matched++; } });
  var cid = rcEsc(g.id).replace(/[^A-Za-z0-9_-]/g, "_");
  var legend = segs.map(function(s){
    return '<button type="button" class="seg-btn' + (s.key === "other" || s.key === "none" ? ' muted' : '') + '" aria-expanded="false" aria-controls="p-' + cid + '-' + s.key + '"><span class="sw" style="background:' + s.color + '"></span><span class="n num">' +
      s.rows.length + '</span> ' + rcEsc(s.label) + (s.note ? ' <span class="meta">(' + rcEsc(s.note) + ')</span>' : '') + '</button>';
  }).join("");
  var bar = segs.map(function(s){ return '<span style="flex:' + s.rows.length + ';background:' + s.color + '" title="' + s.rows.length + ' ' + rcEsc(s.label) + '"></span>'; }).join("");
  var panels = segs.map(function(s){
    return '<div class="seg-panel" id="p-' + cid + '-' + s.key + '" hidden><h5>' + s.rows.length + ' ' + rcEsc(s.label === "other" ? "other names" : s.label) + (s.note ? ' · ' + rcEsc(s.note) : '') + '</h5>' +
      s.subs.map(function(pair){
        var t = pair[1][0];
        return '<div class="sub-grp"><div><span>' + rcEsc(pair[0]) + ' · ' + pair[1].length + '</span>' +
          (s.key !== "none" ? '<button class="linkbtn" type="button" data-filter-t="' + rcEsc(t.ticker) + '" data-filter-g="' + rcEsc(t.cluster_id) + '">Filter to this industry</button>' : '') + '</div>' +
          '<div class="namelist">' + pair[1].map(function(r){ return '<span><b>' + rcEsc(r.ticker) + '</b>' + rcEsc(r.name || "") + '</span>'; }).join("") + '</div></div>';
      }).join("") + '</div>';
  }).join("");
  var plain = res && g.catch_pct != null;
  var ordered = g.items.slice().sort(function(a, x){ return (mset[x.ticker] ? 1 : 0) - (mset[a.ticker] ? 1 : 0); });
  if(filtered && BOARD.only) ordered = ordered.filter(function(r){ return mset[r.ticker]; });
  var chips = ordered.map(function(r){ return chipHtml(r, segOf[r.ticker] || segs[0], filtered && !mset[r.ticker], plain); });
  var first = chips.slice(0, 18).join(""), rest = chips.slice(18).join("");
  var mean = g.mean, base = g.base;
  var rTxt = mean == null ? "—" : Number(mean).toFixed(2);
  var w = mean == null ? 0 : Math.max(2, Math.min(100, Math.round(mean * 100)));
  var tick = base == null ? "" : '<s style="left:' + Math.max(0, Math.min(100, Math.round(base * 100))) + '%"></s>';
  var tight = res
    ? '<dt>Tightness</dt><dd><span class="r">r ' + rTxt + '</span><span class="rscale">0</span><span class="rbar" role="img" aria-label="Correlation r ' + rTxt + ' on a 0 to 1 scale' + (base == null ? '' : ', noise level ' + Number(base).toFixed(2)) + '"><i style="width:' + w + '%"></i>' + tick + '</span><span class="rscale">1</span>' +
      (base == null ? '' : '<span class="meta">vs noise <span class="num">' + Number(base).toFixed(2) + '</span></span>') + '</dd>' +
      '<dt></dt><dd class="meta">Average pairwise correlation (r, from 0 = unrelated to 1 = move in lockstep) of ' + HORIZON + '-day returns after removing ' + rcEsc(b) + '. Not a percentage.' + (base == null ? '' : ' Amber tick = what pure-noise groups of this size reach.') + '</dd>'
    : '<dt>Tightness</dt><dd><span class="r">r ' + rTxt + '</span><span class="rscale">0</span><span class="rbar"><i style="width:' + w + '%"></i></span><span class="rscale">1</span><span class="meta">raw correlation, market included</span></dd>';
  var catchRow = plain ? '<dt>Catch-all</dt><dd><span class="badge">catch-all: ' + g.catch_pct + '% of names · weak shared move, not a theme</span></dd>' : '';
  var dir = "";
  if(g.last != null){
    var up = g.last >= 0;
    dir = '<dt>Last ' + HORIZON + 'd</dt><dd class="dir ' + (up ? 'up' : 'dn') + '">' + (up ? '▲ +' : '▼ ') + Number(g.last).toFixed(1) + ' log pts ' +
      (res ? 'vs ' + rcEsc(b) : '(raw, market included)') + ' <span class="meta">· average of the group’s members</span></dd>';
  }
  return '<div class="card' + (res && mean != null && mean >= 0.55 ? ' tight' : '') + '" id="card-' + cid + '" aria-labelledby="lbl-' + cid + '">' +
    '<div class="chead"><div class="left"><span class="clabel" id="lbl-' + cid + '">' + rcEsc(g.label) + '</span>' +
      '<span style="position:relative;align-self:center"><button class="rcf-info" type="button" aria-expanded="false" aria-controls="why-' + cid + '" aria-label="Why this name">i</button>' +
      '<span class="rcf-pop" id="why-' + cid + '" role="region" aria-label="Group name explained" hidden><h4>Why “' + rcEsc(g.label) + '”?</h4>' + whyTxt(g) + '</span></span>' +
      '<span class="meta num">' + plural(n, "name") + '</span>' + (filtered ? '<span class="match num">' + matched + ' of ' + n + ' match filters</span>' : '') + '</div></div>' +
    (segs.length ? '<div class="comp" aria-label="Composition by industry"><div class="compbar" aria-hidden="true">' + bar + '</div><div class="legend">' + legend + '</div>' + panels + '</div>' : '') +
    '<dl class="stats">' + tight + catchRow + (res ? '<dt>Stability</dt><dd class="col">' + persistTxt(g.persist) + '</dd>' : '') + dir + '</dl>' +
    '<div class="chips">' + first + '</div>' +
    (rest ? '<details class="members"><summary>show all ' + ordered.length + ' names</summary><div class="chips" style="margin-top:6px">' + rest + '</div></details>' : '') +
    '<div class="cfoot"><span>' + (plain ? '' : 'Dashed = joined since a quarter ago') + (RCF && RCF.state().earn !== "any" ? ' · amber dot = earnings in the chosen week' : '') + '</span>' +
      (filtered ? '<span>Numbers are for the whole group</span>' : '') + '</div></div>';
}
function noSharedCard(items){
  // S8/D5 — these names are still on the board; they are just not a group,
  // so they get one openable card.
  var chips = items.map(function(c){ return '<button type="button" class="chip" data-t="' + rcEsc(c.ticker) + '" data-g="' + rcEsc(c.cluster_id) + '" aria-label="' + rcEsc(c.ticker + ", " + (c.name || c.ticker)) + '" aria-describedby="tip">' + rcEsc(c.ticker) + '</button>'; }).join("");
  return '<div class="card quiet">' +
    '<div class="chead"><span class="clabel">No shared move beyond ' + rcEsc(ETF) + ' · ' + plural(items.length, "name") + '</span></div>' +
    '<div class="meta">These names did not move together more than the groups the same method finds in shuffled, pure-noise returns.</div>' +
    '<details class="members"><summary>show all ' + items.length + ' members</summary>' +
    '<div class="chips" style="margin-top:6px">' + chips + '</div></details></div>';
}
function renderOos(){
  var el = document.getElementById("oos");
  var o = OOS && OOS[String(BOARD.cut)];
  if(!o || !isResidual()){ el.hidden = true; return; }
  el.hidden = false;
  document.getElementById("oos-txt").textContent = "Out-of-sample: " + o.held + " of " + o.total + " groups formed a quarter ago still moved together over the last quarter (vs random names).";
}
function render(){
  buildCutSeg();
  var rows = DATA.filter(function(c){ return c.cut === BOARD.cut; });
  var filtered = RCF && RCF.active();
  var by = {}, order = [];
  rows.forEach(function(c){
    var k = c.cluster_id;
    if(!by[k]){ by[k] = {id:k, label:c.cluster_label, mean:c.mean_corr, base:c.baseline_r, no_shared:c.no_shared,
                         last:c.last_residual_pct, persist:c.persist, catch_pct:c.catchall ? c.catchall_pct : null, items:[]}; order.push(k); }
    by[k].items.push(c);
  });
  // Filters select groups (any member passes); members are dimmed, not removed.
  var keys = order.filter(function(k){ return !filtered || by[k].items.some(function(r){ return RCF.pass(r); }); });
  // Sort by residual tightness, not size: a size sort put the beta blob first.
  keys.sort(function(a, b){ return (by[b].mean || 0) - (by[a].mean || 0); });
  var loose = [];
  if(isResidual()) keys = keys.filter(function(k){ if(by[k].no_shared){ loose.push(k); return false; } return true; });
  var warn = document.getElementById("warn");
  warn.style.display = "none";
  if(!isResidual() && keys.length){
    var tot = keys.reduce(function(s, k){ return s + by[k].items.length; }, 0);
    var top = keys.reduce(function(a, b){ return by[b].items.length > by[a].items.length ? b : a; });
    var g = by[top];
    if(tot && g.items.length / tot > BETA_SHARE){
      warn.style.display = "block";
      warn.innerHTML = '⚠ <b>' + rcEsc(g.label) + '</b> holds ' + g.items.length + ' of ' + tot + ' names (' + Math.round(100 * g.items.length / tot) +
        '%) — that is the whole market moving together (beta), not a theme. Switch Remove market to S&P 500 (SPY).';
    }
  }
  var cards = keys.map(function(k){ return cardHtml(by[k]); });
  var looseItems = [];
  loose.forEach(function(k){ looseItems = looseItems.concat(by[k].items.filter(function(r){ return !filtered || RCF.pass(r); })); });
  if(looseItems.length) cards.push(noSharedCard(looseItems));
  document.getElementById("grid").innerHTML = cards.join("") ||
    (DATA.length ? '<div class="empty">No group has a name that passes these filters.</div>' : document.getElementById("grid").innerHTML);
  var names = {};
  keys.forEach(function(k){ by[k].items.forEach(function(r){ if(!filtered || RCF.pass(r)) names[r.ticker] = 1; }); });
  looseItems.forEach(function(r){ names[r.ticker] = 1; });
  var nn = Object.keys(names).length, groups = keys.length + (looseItems.length ? 1 : 0);
  document.getElementById("count").textContent = plural(nn, "name") + " in " + plural(groups, "group");
  var sn = document.getElementById("showing-n"), ss = document.getElementById("showing-step");
  if(sn && ss){ sn.textContent = fmt(nn); ss.style.display = filtered ? "" : "none"; }
  renderOos();
}
function jumpTo(r){
  HIT = r.ticker;
  render();
  var card = null, cards = document.querySelectorAll(".card");
  for(var i = 0; i < cards.length; i++){ if(cards[i].querySelector('.chip[data-t="' + r.ticker.replace(/"/g, "") + '"]')){ card = cards[i]; break; } }
  var strip = document.getElementById("strip");
  var g = DATA.filter(function(x){ return x.cut === BOARD.cut && x.cluster_id === r.cluster_id; });
  strip.style.display = "block";
  strip.innerHTML = '<b>' + rcEsc(r.ticker) + '</b> (' + rcEsc(r.name || r.ticker) + ') is in <b>' + rcEsc(r.cluster_label) + '</b> at the ' + rcEsc(cutWord(BOARD.cut)) + ' level, with ' + plural(Math.max(0, g.length - 1), "other name") + '.'
    + rcWatchRow(r.ticker);       // the found name's watch-list pair (PR 4); a chip is itself a button
  if(card){
    var d = card.querySelector('.chip[data-t="' + r.ticker.replace(/"/g, "") + '"]').closest("details");
    if(d) d.open = true;
    if(card.scrollIntoView) card.scrollIntoView({behavior:"smooth", block:"center"});
    card.classList.add("flash"); setTimeout(function(){ card.classList.remove("flash"); }, 1600);
  }
}
function init(){
  RCF = RCFilters.mount({root: "#rc-filters", rows: DATA, meta: META, onChange: function(){ render(); }});
  RCInfo.wire();
  var mk = document.querySelector('[data-group="market"]'), hz = document.querySelector('[data-group="horizon"]'), cs = document.getElementById("cut-seg");
  var wn = document.querySelector('[data-group="window"]'), rs = document.getElementById("reset-settings");
  if(mk) RCRadios.wire(mk, setMarket);
  if(hz) RCRadios.wire(hz, function(v){ setHorizon(Number(v)); });
  if(wn) RCRadios.wire(wn, function(v){ setWindow(Number(v)); });
  if(rs) rs.addEventListener("click", function(){ resetSettings(rs); });
  if(cs) RCRadios.wire(cs, setCut);
  var om = document.getElementById("only-m");
  if(om){ om.checked = BOARD.only; om.onchange = function(){ BOARD.only = om.checked; saveBoard(); render(); }; }
  var fi = document.getElementById("find");
  if(fi) RCCombobox.mount({input: fi, list: "#find-list",
    rows: function(){ return DATA.filter(function(c){ return c.cut === BOARD.cut; }); },
    filter: function(r){ return RCF.pass(r); },
    groupLabel: function(r){ return r.cluster_label; },
    onPick: jumpTo,
    onClear: function(){ HIT = null; document.getElementById("strip").style.display = "none"; render(); }});
  document.addEventListener("click", function(e){
    var c = e.target.closest && e.target.closest("[data-clear-for]");
    if(c){ var inp = document.getElementById(c.getAttribute("data-clear-for")); inp.value = ""; HIT = null; document.getElementById("strip").style.display = "none"; render(); inp.focus(); }
  });
  var grid = document.getElementById("grid");
  grid.addEventListener("click", function(e){
    var s = e.target.closest(".seg-btn");
    if(s){
      var id = s.getAttribute("aria-controls"), p = document.getElementById(id), open = p.hidden, card = s.closest(".card");
      card.querySelectorAll(".seg-panel").forEach(function(x){ x.hidden = true; });
      card.querySelectorAll(".seg-btn").forEach(function(x){ x.setAttribute("aria-expanded", "false"); });
      p.hidden = !open; s.setAttribute("aria-expanded", String(open)); return;
    }
    var f = e.target.closest("[data-filter-t]");
    if(f){
      var r = DATA.filter(function(x){ return x.cut === BOARD.cut && x.ticker === f.getAttribute("data-filter-t"); })[0];
      if(r){ RCF.setGics(r, 2); var fl = document.getElementById("rc-filters"); if(fl && fl.scrollIntoView) fl.scrollIntoView({behavior:"smooth", block:"start"}); }
    }
  });
  // Chip tooltip: company name first, on hover AND keyboard focus.
  var tip = document.getElementById("tip");
  function showTip(el){
    var t = el.getAttribute("data-t"), gid = el.getAttribute("data-g");
    var r = DATA.filter(function(x){ return x.ticker === t && x.cluster_id === gid; })[0];
    if(!r) return;
    var themes = (r.theme_ids || []).map(function(id){ return META.themes[id]; }).filter(function(x){ return x && x.level === "fine"; }).map(function(x){ return x.label; });
    tip.innerHTML = '<b>' + rcEsc(r.ticker) + ' — ' + rcEsc(r.name || r.ticker) + '</b><br>' + rcEsc(industryOf(r) || "No industry") + ' · ' + rcEsc(fmtCap(r.market_cap)) +
      ((r.index || []).length ? '<br>' + rcEsc(r.index.map(function(x){ return x === "Nasdaq Composite" ? "Nasdaq-listed" : x; }).join(" · ")) : '') +
      '<br>Theme: ' + (themes.length ? rcEsc(themes.join(", ")) : 'none (not profiled)') +
      '<br>Earnings: ' + rcEsc(RCF.earnLabel(r)) +
      (r.core === false ? '<br><span style="color:var(--amber)">Joined this group since a quarter ago</span>' : '');
    tip.hidden = false;
    var b = el.getBoundingClientRect(), tr = tip.getBoundingClientRect();
    var x = Math.min(window.innerWidth - tr.width - 8, Math.max(8, b.left)), y = b.bottom + 6;
    if(y + tr.height > window.innerHeight - 8) y = b.top - tr.height - 6;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  ["mouseover", "focusin"].forEach(function(ev){ document.addEventListener(ev, function(e){ var c = e.target.closest && e.target.closest(".chip"); if(c) showTip(c); }); });
  ["mouseout", "focusout"].forEach(function(ev){ document.addEventListener(ev, function(e){ if(e.target.closest && e.target.closest(".chip")) tip.hidden = true; }); });
  render();
}
"""

    # One pass, and JSON with < > & escaped: a label holding "<!--<script>" or
    # spelling another placeholder cannot break the script (F3, #144).
    JS = fill(JS, {"__DATA__": script_json(_pack_rows(members)),
                   "__CUTS__": script_json(cuts),
                   "__CUT_WORDS__": script_json(cut_words),
                   "__BETA__": str(BETA_SHARE),
                   "__HORIZON__": str(horizon),
                   "__WINDOW__": str(eff["window"]),
                   "__LOCKED__": "true" if eff["locked"] else "false",
                   "__MARKET__": script_json(benchmark),
                   "__APPLIED__": "true" if applied else "false",
                   "__OOS__": script_json(payload.get("oos") or {}),
                   "__META__": script_json(meta),
                   "__ETF__": script_json(etf)})

    oos_info = control_html(
        "oos", "", "", "Out-of-sample",
        f"Groups are re-formed on data ending a quarter ago, then tested on the "
        f"{eff['oos_holdout']} sessions they never saw. “Held” means the names "
        f"kept moving together more than random names would.")
    oos_note = payload.get("oos_note")
    oos_note_html = (f'<div class="oos" id="oos-note" role="status">'
                     f'<span>{_html.escape(str(oos_note))}</span></div>' if oos_note else "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Co-movement</title>
<style>{CSS}</style>
{FILTERS_CSS}
</head>
<body>
<div class="wrap">
  <header class="topbar">
    <div>
      <h1>Co-movement</h1>
      <div class="sub">{sub}</div>
      {funnel_line}
      <div class="limits">{limits}</div>
    </div>
    <div class="sync">Prices through <b>{_html.escape(sync)}</b></div>
  </header>
  {provider_health_html(payload.get("provider_health"))}
  {help_block}
  {about}
  {board_row}
  {filters_bar_html("rc-filters")}
  {alerts_html}
  <span id="busy" class="sr-only" role="status" aria-live="polite"></span>
  <div class="oos" id="oos" hidden><span id="oos-txt"></span>{oos_info}</div>
  {oos_note_html}
  <div class="warn" id="warn"></div>
  <div class="strip" id="strip"></div>
  <div class="grid" id="grid">{grid}</div>
</div>
<div class="tip" id="tip" role="tooltip" hidden></div>
<script>{FILTERS_JS}</script>
{watchlist_html("comovement")}
<script>{JOB_JS}
{JS}
init();
</script>
</body>
</html>
"""
