"""The Activity board (``/view/activity``), which takes the place of Logs and Runs in the menu (avo, 10 Nov 2026:
"logs ve runs da kendine ait taba gitsin", "RC activity yi dashboardlara gore ayirsan daha kolay olur").

Three tabs:

- **Runs** and **Logs** are the Grafana boards ``rc-activity`` and ``rc-logs`` (home-server homeops), framed as the
  Settings page frames RC Fetch: the scheduled jobs, their switches and their run logs live in homeops, so they are
  drawn there. Each groups its jobs by the board they feed, from ``GET /api/activity/boards`` here.
- **Watch lists** is this app's own: the two lists as tables, a name added by its own box (never a toggle, which
  would take off a name someone else just added), and a Remove on each row.

``job_boards`` is worked out from the code on each call, so a new board or job lands in place with no list to keep:
a scheduled job feeds a board when a table it writes (``AutomationEntry.tables_written``) is named in that board's
code. Settings reads every table, so it is left out; a job that feeds no board, or writes no table, is ``System``
unless ``BY_NAME`` places it.
"""
from __future__ import annotations

import functools
import re
from datetime import datetime
from pathlib import Path

from RevenueChaser.core.html_safe import esc
from RevenueChaser.core.reporting_common import THEME_CSS, THEME_JS

ROOT = Path(__file__).resolve().parent.parent
SYSTEM = "System"
#: A job that writes no table but plainly serves one board.
BY_NAME = {"RC-SPX-Gamma-Fetch": "module_spx_gamma"}
#: The builder, the backup and the checks: they serve every board, so they are System, whatever they write.
SYSTEM_JOBS = frozenset({"RC-Build", "RC-Backup-Weekly", "RC-IBKR-Login-Check"})
#: Settings reads every table, so naming it would put every job under it.
NOT_A_FEED = frozenset({"module_settings"})


@functools.lru_cache(maxsize=1)
def _board_code() -> dict[str, str]:
    """Each board's own code (tests left out), keyed by its tab key. Read once a process: the code does not change
    under a running dashboard."""
    from RevenueChaser.app import dashboard
    out = {}
    for t in dashboard.TABS:
        key = t.get("key") or ""
        folder = ROOT / key
        if t.get("type") == "tab" and key.startswith("module_") and key not in NOT_A_FEED and folder.is_dir():
            out[key] = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in sorted(folder.rglob("*.py"))
                                 if "tests" not in p.parts)
    return out


def job_boards() -> dict:
    """``{"boards": [{"key", "label", "jobs": [registry names]}], "jobs": {name: {"unit", "label", "boards"}}}``
    in menu order, ``System`` last. A job is listed under every board it feeds."""
    from RevenueChaser.app import dashboard
    from RevenueChaser.core import automations
    code = _board_code()
    labels = {t["key"]: t["label"] for t in dashboard.TABS if t.get("type") == "tab"}
    jobs, by_board = {}, {k: [] for k in code}
    by_board[SYSTEM] = []
    for e in automations.REGISTRY:
        if e.name in SYSTEM_JOBS:
            feeds = []
        elif e.name in BY_NAME:
            feeds = [BY_NAME[e.name]]
        else:
            names = [re.compile(rf"""['"\s.]{re.escape(t)}['"\s]""") for t in e.tables_written]
            feeds = [k for k, src in code.items() if any(n.search(src) for n in names)]
        for k in feeds or [SYSTEM]:
            by_board[k].append(e.name)
        jobs[e.name] = {"unit": e.unit, "label": e.label, "boards": feeds or [SYSTEM]}
    boards = [{"key": k, "label": labels.get(k, k), "jobs": v} for k, v in by_board.items() if v and k != SYSTEM]
    boards.append({"key": SYSTEM, "label": SYSTEM, "jobs": by_board[SYSTEM]})
    return {"boards": boards, "jobs": jobs}


def pressed_by_board(rows: list[dict]) -> dict[str, dict]:
    """The dashboard's own job runs (``runview.read``'s rows), per board key: how many, the newest start and how
    many failed or were lost. A run whose module is not a board is ``System``."""
    from RevenueChaser.app import dashboard
    keys = {t["key"] for t in dashboard.TABS if t.get("type") == "tab"}
    out: dict[str, dict] = {}
    for r in rows:
        key = r.get("module") if r.get("module") in keys else SYSTEM
        s = out.setdefault(key, {"runs": 0, "last": None, "bad": 0})
        s["runs"] += 1
        s["last"] = max(filter(None, (s["last"], r.get("started_at"))), default=None)
        s["bad"] += r.get("status") in ("failed", "lost")
    return out


# ── The page ─────────────────────────────────────────────────────────────────────────────────────────────────

TABS = (("runs", "Runs"), ("logs", "Logs"), ("watch", "Watch lists"))
#: The Grafana board each framed tab shows (home-server grafana/make_dashboards.py).
BOARDS = {"runs": "rc-activity", "logs": "rc-logs"}
WATCH = {"option": ("Option watch list", "The names the options build covers besides SPX (D2).", "Opt"),
         "stock": ("Stock watch list", "The names LEAPS pins and scans, and Earnings treats as watched.", "Stk")}

CSS = """
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:var(--page-max,1280px);margin:0 auto;padding:16px}
h1{font-size:20px;margin:0 0 4px;color:var(--bright)}
.sub{color:var(--muted);font-size:13px;margin:0 0 12px}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--border);margin:0 0 12px}
.tabs button{min-height:44px;padding:0 16px;border:0;border-bottom:2px solid transparent;background:none;color:inherit;
  font:inherit;cursor:pointer}
.tabs button[aria-selected="true"]{border-color:var(--sky);color:var(--sky);font-weight:600}
.tabs button:focus-visible,button:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--sky);
  outline-offset:2px}
.frame iframe{display:block;width:100%;height:calc(100vh - 150px);min-height:560px;border:0;background:var(--bg)}
.frame a{display:inline-flex;align-items:center;min-height:44px;color:var(--sky);font-size:13px}
.wl{margin:0 0 24px}
.wl h2{font:700 15px/1.3 ui-monospace,Menlo,Consolas,monospace;letter-spacing:.06em;text-transform:uppercase;
  margin:0 0 4px;color:var(--bright)}
.wl h2 span{margin-left:8px;color:var(--muted)}
.wl p{margin:0 0 8px;color:var(--muted);font-size:13px}
.add{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 8px}
.add input{min-height:44px;width:180px;padding:0 10px;border-radius:6px;border:1px solid var(--input-border);
  background:var(--input-bg);color:var(--fg);font:inherit;text-transform:uppercase}
.btn{min-height:44px;padding:0 14px;border-radius:6px;border:1px solid var(--input-border);background:transparent;
  color:inherit;font:inherit;cursor:pointer}
.btn.go{border-color:var(--sky);color:var(--sky)}
.btn:disabled{opacity:.5;cursor:wait}
table{width:100%;border-collapse:collapse}
th{text-align:left;font:700 12px/1.25 ui-monospace,Menlo,Consolas,monospace;letter-spacing:.06em;text-transform:uppercase;
  padding:6px 8px;color:var(--muted)}
td{padding:6px 8px;border-top:1px solid var(--border);vertical-align:middle}
td.t{font:600 14px ui-monospace,Menlo,Consolas,monospace;color:var(--bright)}
td.r{text-align:right}
.empty{color:var(--muted)}
.msg{min-height:20px;font-size:13px;color:var(--muted)}.msg.bad{color:var(--neg)}
@media (max-width:760px){thead{display:none}tr{display:grid;grid-template-columns:1fr auto;padding:6px 0;
  border-top:1px solid var(--border)}td{border:0;padding:2px 8px}td.r{grid-row:1 / span 3;grid-column:2;align-self:center}}
"""

JS = """
(function(){
  var KEY = 'rc.activity.tab', tabs = [].slice.call(document.querySelectorAll('[role="tab"]'));
  function theme(){
    var t = document.documentElement.getAttribute('data-theme');
    if (t === 'light' || t === 'dark') return t;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  function load(panel){
    // a framed tab gets its address only when it opens, so a Watch lists visit never loads Grafana
    var f = panel.querySelector('iframe[data-board]'), a = panel.querySelector('a[data-board]');
    if (!f) return;
    var base = location.protocol + '//' + location.hostname + ':3000/d/' + f.dataset.board + '/' + f.dataset.board;
    var want = base + '?theme=' + theme() + '&kiosk';
    if (f.getAttribute('src') !== want) f.setAttribute('src', want);
    if (a) a.href = base + '?theme=' + theme();
  }
  function show(name){
    tabs.forEach(function(t){
      var on = t.dataset.tab === name, p = document.getElementById(t.getAttribute('aria-controls'));
      t.setAttribute('aria-selected', on ? 'true' : 'false'); t.tabIndex = on ? 0 : -1; p.hidden = !on;
      if (on) load(p);
    });
    try { localStorage.setItem(KEY, name); } catch (e) {}
  }
  tabs.forEach(function(t, i){
    t.addEventListener('click', function(){ show(t.dataset.tab); });
    t.addEventListener('keydown', function(e){
      var d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
      if (!d) return;
      var n = tabs[(i + d + tabs.length) % tabs.length]; n.focus(); show(n.dataset.tab);
    });
  });
  var start = 'runs';
  try { start = localStorage.getItem(KEY) || 'runs'; } catch (e) {}
  if (!tabs.some(function(t){ return t.dataset.tab === start; })) start = 'runs';
  show(start);

  function post(url, form){
    var msg = form.querySelector('.msg'), btns = form.querySelectorAll('button');
    btns.forEach(function(b){ b.disabled = true; });
    return fetch(url, {method: 'POST', headers: {'X-RC-Panel': 'activity'}})
      .then(function(r){ return r.json().then(function(j){ return {ok: r.ok && j.ok, j: j}; }); })
      .then(function(x){
        if (!x.ok) { msg.textContent = x.j.error || 'refused'; msg.className = 'msg bad'; return; }
        location.reload();
      })
      .catch(function(err){ msg.textContent = 'The dashboard did not answer: ' + err; msg.className = 'msg bad'; })
      .then(function(){ btns.forEach(function(b){ b.disabled = false; }); });
  }
  [].forEach.call(document.querySelectorAll('form.add'), function(form){
    form.addEventListener('submit', function(e){
      e.preventDefault();
      var t = form.querySelector('input').value.trim().toUpperCase();
      if (t) post('/api/watchlist/' + form.dataset.list + '/add/' + encodeURIComponent(t), form);
    });
  });
  [].forEach.call(document.querySelectorAll('button[data-remove]'), function(b){
    b.addEventListener('click', function(){
      var sec = b.closest('section');
      if (!window.confirm('Take ' + b.dataset.remove + ' off the ' + sec.dataset.title + '?')) return;
      post('/api/watchlist/' + sec.dataset.list + '/remove/' + encodeURIComponent(b.dataset.remove), sec);
    });
  });
})();
"""


def _when(epoch) -> str:
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%d %b %Y %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"


def _watch(kind: str, rows: list[dict] | None, error: str | None) -> str:
    title, what, button = WATCH[kind]
    if error:
        body = f'<tr><td colspan="5" class="empty">The list could not be read: {esc(error)}</td></tr>'
    elif not rows:
        body = f'<tr><td colspan="5" class="empty">No names yet. Add one here, or press {button} next to a ticker on any board.</td></tr>'
    else:
        body = "".join(
            f'<tr><td class="t">{esc(r.get("ticker"))}</td><td>{esc(_when(r.get("added_at")))}</td>'
            f'<td>{esc(r.get("added_by") or "—")}</td><td>{esc(r.get("note") or "")}</td><td class="r">'
            f'<button type="button" class="btn" data-remove="{esc(r.get("ticker"))}" aria-label="Remove '
            f'{esc(r.get("ticker"))} from the {esc(title.lower())}">Remove</button></td></tr>' for r in rows)
    return (f'<section class="wl" data-list="{kind}" data-title="{esc(title.lower())}" aria-labelledby="wl-{kind}">'
            f'<h2 id="wl-{kind}">{esc(title)}<span>{len(rows or [])}</span></h2><p>{esc(what)}</p>'
            f'<form class="add" data-list="{kind}"><input aria-label="A ticker to add to the {esc(title.lower())}" '
            f'placeholder="Ticker" maxlength="12" autocomplete="off"><button class="btn go" type="submit">Add</button>'
            f'<span class="msg" role="status"></span></form>'
            '<table><thead><tr><th>Ticker</th><th>Added</th><th>By</th><th>Note</th><th></th></tr></thead>'
            f'<tbody>{body}</tbody></table></section>')


def watch_lists() -> dict[str, tuple[list[dict] | None, str | None]]:
    from RevenueChaser.rcdata import ops
    out = {}
    for kind in WATCH:
        try:
            out[kind] = (ops.watch_rows(kind), None)
        except Exception as e:  # noqa: BLE001 - one unreadable list never takes the page down
            out[kind] = (None, f"{type(e).__name__}: {e}"[:200])
    return out


def render_page(lists: dict | None = None) -> str:
    lists = lists if lists is not None else watch_lists()
    strip = "".join(f'<button type="button" role="tab" id="tab-{k}" data-tab="{k}" aria-controls="panel-{k}" '
                    f'aria-selected="false" tabindex="-1">{w}</button>' for k, w in TABS)
    panels = []
    for k, _ in TABS:
        if k in BOARDS:
            b = BOARDS[k]
            inner = (f'<div class="frame"><iframe data-board="{b}" title="{"Runs by board" if k == "runs" else "Run logs by board"} '
                     f'(Grafana)" loading="lazy"></iframe><a data-board="{b}" target="_blank" rel="noopener">Open in '
                     'Grafana ↗</a></div>')
        else:
            inner = "".join(_watch(kind, *lists.get(kind, (None, "not read"))) for kind in WATCH)
        panels.append(f'<div id="panel-{k}" role="tabpanel" aria-labelledby="tab-{k}" hidden>{inner}</div>')
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Activity</title>'
            f'<style>{THEME_CSS}{CSS}</style><script>{THEME_JS}</script></head><body><div class="wrap">'
            '<h1>Activity</h1><p class="sub">Every run and log, by the board it feeds, and the two watch lists.</p>'
            f'<nav aria-label="Activity tabs"><div class="tabs" role="tablist">{strip}</div></nav>{"".join(panels)}'
            f'</div><script>{JS}</script></body></html>')
