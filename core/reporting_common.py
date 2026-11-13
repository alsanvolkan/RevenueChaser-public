"""Shared dashboard chrome — the filter kit, help, health banner and board writers.

Every board's universal filters come from the shared kit in ``core/static/rc_filters.{js,css}``,
inlined as ``FILTERS_JS`` / ``FILTERS_CSS`` and mounted with ``filters_bar_html`` + ``filters_meta``.
See AGENTS.md ("Universal filters") and ``docs/dashboard_ui.md`` §2b.
"""
from __future__ import annotations

import gzip
import html
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from RevenueChaser.core.html_safe import ESC_JS

# ── Job poller + board-settings reset, shared by Settings and Co-movement ──
# One truthful verdict for every status of ``/api/job/<id>`` (#37): a job that
# is unknown, cancelled or failed never reads as done. ``settingsPoll`` reloads
# the board with ``?refresh=1`` only once the job is really done.
# ``boardSettingsReset`` POSTs ``{reset: true}`` and hands the job to the poller.
JOB_JS = r"""
function settingsJobVerdict(d){
  if(!d || !d.ok || !d.job){
    return {state:'error', text:'Job not found (the server may have restarted). Press Retry.'};
  }
  var job = d.job, st = job.status;
  if(st === 'queued') return {state:'poll', text:'Queued…'};
  if(st === 'running') return {state:'poll', text:'Running…'};
  if(st === 'done') return {state:'done', text: job.message || 'Done'};
  if(st === 'error') return {state:'error', text: job.error || 'Error'};
  if(st === 'cancelled') return {state:'error', text:'Cancelled'};
  return {state:'error', text:'Unknown job status: ' + st};
}
function settingsPoll(btn, out, jobId){
  fetch('/api/job/' + encodeURIComponent(jobId)).then(function(r){ return r.json(); }).then(function(d){
    var v = settingsJobVerdict(d);
    if(out) out.textContent = v.text;
    if(v.state === 'poll'){ setTimeout(function(){ settingsPoll(btn, out, jobId); }, 2000); return; }
    btn.disabled = false;
    if(v.state === 'error'){ btn.textContent = 'Retry'; return; }
    location.search = '?refresh=1';
  }).catch(function(e){
    btn.disabled = false; btn.textContent = 'Retry'; if(out) out.textContent = String(e);
  });
}
function boardSettingsReset(btn, out, board){
  btn.disabled = true;
  if(out) out.textContent = 'Resetting…';
  fetch('/api/settings/board/' + encodeURIComponent(board), {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-RC-Panel': 'board-settings'},
    body: JSON.stringify({reset: true})
  }).then(function(r){ return r.json(); }).then(function(d){
    if(!d.ok || !d.job_id){
      btn.disabled = false;
      if(out) out.textContent = (d.messages && d.messages.join('; ')) || d.error || 'failed to reset';
      return;
    }
    try{ parent.postMessage({type:'rc-job-submitted', job_id: d.job_id}, location.origin); }catch(e){}
    settingsPoll(btn, out, d.job_id);
  }).catch(function(e){
    btn.disabled = false; if(out) out.textContent = String(e);
  });
}
"""

# ── Theme tokens: one palette per scheme, defined here only (#184) ──
# The shell (templates/dashboard.html) renders ``THEME_CSS`` and ``THEME_JS``; a board adopts them in its own PR
# (one per board) by using these names instead of its own palette. Dark is the default on every device (avo,
# 25 Oct 2026). The palette is the warm-neutral "full design system" avo picked on 12 Nov 2026 (13F thread,
# option D of four skill mocks) in the "Mürekkep" colours he picked the same day from three families: warm paper
# neutrals in light, a navy-black "Gece" dark (his second pick, 12 Nov 13:50 Amsterdam, of three dark options), one
# ink-blue ``accent`` for selection, sort and focus, muted ``pos`` / ``neg`` / ``amber``,
# and ``data-1`` / ``data-2`` for chart series. Light applies when the reader chooses Light, or chooses Auto on a
# device that prefers light. The choice is ``localStorage[THEME_STORAGE_KEY]`` ("light" / "auto"; absent = dark),
# set as ``data-theme`` on ``<html>`` ("dark" / "light" / "auto"). The shell copies that attribute onto every
# board iframe (same origin).
THEME_STORAGE_KEY = "rc-theme"
THEME_TOKENS = {
    "dark": {
        "bg": "#11161D", "fg": "#D5DBE3", "bright": "#F1F4F8", "card": "#192029", "card-2": "#1F2731",
        "border": "rgba(213,219,227,.10)", "sidebar": "#141A22", "pos": "#6CC497", "neg": "#E98A7F",
        "amber": "#E0B060", "sky": "#8DB8DA", "muted": "#98A2AF", "accent": "#8DB8DA",
        "accent-soft": "rgba(141,184,218,.14)", "pos-soft": "rgba(108,196,151,.12)",
        "neg-soft": "rgba(233,138,127,.12)", "amber-soft": "rgba(224,176,96,.12)", "focus": "#8DB8DA",
        "input-bg": "#1E2631", "input-border": "#323C49", "hover": "#212A35", "topbar": "rgba(17,22,29,.85)",
        "nav-fg": "rgba(241,244,248,.72)", "nav-on": "#26303C", "hover-faint": "rgba(255,255,255,.03)",
        "frame-bg": "#11161D", "overlay": "rgba(17,22,29,.72)", "scroll-thumb": "#3C4655",
        "scroll-thumb-hover": "#4C5767", "scroll-track": "#141A22", "primary-bg": "#E6EBF1",
        "primary-fg": "#11161D", "oi-bg": "rgba(25,32,41,.6)", "scrim": "rgba(0,0,0,.5)",
        "shadow": "rgba(0,0,0,.35)", "badge-blue": "#8DB8DA", "badge-gray": "#A3ACB8",
        "hover-soft": "rgba(255,255,255,.05)", "hover-day": "rgba(255,255,255,.08)",
        "gex-pos-bg": "rgba(108,196,151,.20)", "gex-pos-fg": "#CDEEDC", "gex-neg-bg": "rgba(233,138,127,.20)",
        "gex-neg-fg": "#F6D3CE", "data-1": "#4F93D2", "data-2": "#D9773F", "grid": "rgba(213,219,227,.07)",
    },
    "light": {
        "bg": "#F6F5F1", "fg": "#2E2C28", "bright": "#1A1916", "card": "#FFFFFF", "card-2": "#F3F1EC",
        "border": "rgba(40,36,30,.12)", "sidebar": "#EFEDE7", "pos": "#2F7A55", "neg": "#B0443A",
        "amber": "#9A6212", "sky": "#2F6690", "muted": "#67635B", "accent": "#2F6690",
        "accent-soft": "rgba(47,102,144,.10)", "pos-soft": "rgba(47,122,85,.10)",
        "neg-soft": "rgba(176,68,58,.09)", "amber-soft": "rgba(154,98,18,.10)", "focus": "#2F6690",
        "input-bg": "#FFFFFF", "input-border": "#D6D2C8", "hover": "#EFECE5", "topbar": "rgba(246,245,241,.85)",
        "nav-fg": "#4A4741", "nav-on": "#E9E5DC", "hover-faint": "rgba(40,36,30,.04)", "frame-bg": "#F6F5F1",
        "overlay": "rgba(246,245,241,.75)", "scroll-thumb": "#C4BFB3", "scroll-thumb-hover": "#A9A396",
        "scroll-track": "#EEEBE4", "primary-bg": "#2E2C28", "primary-fg": "#FAF9F6",
        "oi-bg": "rgba(255,255,255,.7)", "scrim": "rgba(30,28,24,.32)", "shadow": "rgba(30,28,24,.10)",
        "badge-blue": "#2F6690", "badge-gray": "#67635B", "hover-soft": "rgba(40,36,30,.06)",
        "hover-day": "rgba(40,36,30,.09)", "gex-pos-bg": "rgba(47,122,85,.14)", "gex-pos-fg": "#1F5A3D",
        "gex-neg-bg": "rgba(176,68,58,.12)", "gex-neg-fg": "#8A3128", "data-1": "#2A6FB0", "data-2": "#C0602E",
        "grid": "rgba(40,36,30,.08)",
    },
}


# The scales that go with the palette, the same in both schemes (theme D, avo 12 Nov 2026): one type scale, a 4 px
# spacing scale, three radii and three elevations (0 = border only, 1 = card, 2 = dialog / popover). A board uses
# the names (``var(--fs-base)``, ``var(--sp-4)``, ``var(--elev-1)``) instead of its own pixel values.
# #764 landed a parallel set the same day (``--s1``.., ``--f-*``, ``--e-*``); those names stay as aliases of D's,
# so a board written against either set gets the same values, and D's names are the ones to use. ``--focus`` is
# a colour token per scheme here (it follows ``--accent``), not a scale token.
# ``--c1``..``--c6`` (#764) is the categorical chart ramp for more than two series: a fixed order, never cycled — a
# seventh series is "other"; reordering it fails the deutan check, so the order is the contract. Two series use
# ``--data-1`` / ``--data-2``.
THEME_SCALE = {
    "font-sans": "Inter,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif",
    "fs-xs": "11px", "fs-sm": "12.5px", "fs-base": "14px", "fs-md": "16px", "fs-lg": "20px", "fs-xl": "26px",
    "sp-1": "4px", "sp-2": "8px", "sp-3": "12px", "sp-4": "16px", "sp-5": "24px", "sp-6": "32px", "sp-7": "48px",
    "r-sm": "6px", "r-md": "10px", "r-lg": "14px", "r-pill": "999px",
    "elev-1": "0 1px 2px var(--shadow)", "elev-2": "0 12px 32px var(--shadow)",
    "touch": "44px",
    # #764's names, as aliases of D's (the space ladder keeps its own values, which are D's, plus 64 px).
    "s1": "4px", "s2": "8px", "s3": "12px", "s4": "16px",
    "s5": "24px", "s6": "32px", "s7": "48px", "s8": "64px",
    "f-xs": "var(--fs-xs)", "f-sm": "var(--fs-sm)", "f-md": "var(--fs-base)", "f-lg": "var(--fs-md)",
    "f-xl": "var(--fs-lg)",
    "w-reg": "400", "w-bold": "600",
    "lh-body": "1.55", "lh-head": "1.2",
    "e-flat": "0 0 0 1px var(--border)", "e-float": "var(--elev-2)",
    "motion": "150ms ease", "tap": "var(--touch)",
    "c1": "#0C87CC", "c2": "#C77C09", "c3": "#0A9E6E",
    "c4": "#8B5CF6", "c5": "#E8445F", "c6": "#2F9E8F",
}


def _theme_vars(scheme: str) -> str:
    return ";".join(f"--{k}:{v}" for k, v in THEME_TOKENS[scheme].items()) + f";color-scheme:{scheme}"


def _scale_vars() -> str:
    return ";".join(f"--{k}:{v}" for k, v in THEME_SCALE.items())


# Bare CSS (no <style>), so a page puts it inside its own style block.
THEME_CSS = (
    # Inter, served by the dashboard itself (app/static/fonts, SIL OFL 1.1 in Inter-OFL.txt): no font host, so the
    # boards look the same offline and on a phone, which would otherwise fall back to its own sans.
    "@font-face{font-family:Inter;font-style:normal;font-weight:100 900;font-display:swap;"
    "src:url(/static/fonts/InterVariable.woff2) format('woff2')}\n"
    f":root{{{_scale_vars()};{_theme_vars('dark')}}}\n"
    f"@media (prefers-color-scheme: light){{:root[data-theme=\"auto\"]{{{_theme_vars('light')}}}}}\n"
    f":root[data-theme=\"light\"]{{{_theme_vars('light')}}}\n"
    "@media (prefers-reduced-motion: reduce){:root{--motion:0ms}}\n"
)

# Runs in <head>, before the page draws, so a light choice never flashes dark. ``rcThemeGet`` is the stored
# choice ("dark" when none, or when storage is unavailable); ``rcThemeSet`` applies one and remembers it if
# it can (dark, the default, is remembered as nothing). Every storage access is in its own try/catch: a
# private window or blocked site data throws.
THEME_JS = """
var RC_THEME_KEY = '""" + THEME_STORAGE_KEY + """';
function rcThemeGet(){
  var v = null;
  try{ v = localStorage.getItem(RC_THEME_KEY); }catch(e){ v = null; }
  return (v === 'light' || v === 'auto') ? v : 'dark';
}
function rcThemeApply(choice){
  document.documentElement.setAttribute('data-theme', (choice === 'light' || choice === 'auto') ? choice : 'dark');
}
function rcThemeSet(choice){
  if(choice !== 'light' && choice !== 'auto') choice = 'dark';
  rcThemeApply(choice);
  if(choice === 'dark'){ try{ localStorage.removeItem(RC_THEME_KEY); }catch(e){} }
  else { try{ localStorage.setItem(RC_THEME_KEY, choice); }catch(e){} }
  return choice;
}
rcThemeApply(rcThemeGet());
"""

# The shared panels' summaries: 9 px padding is a 34 px target, so a phone or a touch screen gets 44 px.
# Padding, not display:flex: a <summary> draws its open/closed arrow only while it is display:list-item.
_SUMMARY_TOUCH = """@media (max-width: 900px), (pointer: coarse){
%s summary{min-height:44px;padding-top:12px;padding-bottom:12px;box-sizing:border-box}
}"""

# ── "What am I looking at?" help box (every tab) ──
# ``help_html`` wraps a one-paragraph explanation in the shared collapsible
# chrome, so every board renders the same help affordance. ``body`` is trusted
# HTML (the caller writes <b>/<code>); it is not escaped.
# Fallbacks: a board without the standard tokens (Earnings Radar, SPX Gamma, LEAPS) still gets the dark box
# (#383 C3).
HELP_CSS = """<style>
.help{background:var(--card,#12161e);border:1px solid var(--border,#232a38);border-radius:10px;margin-bottom:14px;color:var(--muted,#8a93a6);font-size:12.5px}
.help summary{cursor:pointer;padding:9px 14px;font-weight:600;color:var(--sky,#0EA5E9)}
.help .body{padding:0 14px 12px;line-height:1.7}
.help b{color:var(--fg,#d7dce5)}
.help code{color:var(--pos,#00e6a8)}
""" + _SUMMARY_TOUCH % ".help" + """
</style>"""

# ── Shared filter bar (ADR-2; .hermes/plans_claude/2026-09-30_plan_filters_ux.md) ──
# The component lives in real files (core/static/rc_filters.{js,css}) so it can
# be linted and unit-tested; boards inline them, so artifacts stay
# self-contained. Every board mounts this bar; the old per-board snippets are gone (#383 C1d).
_STATIC = Path(__file__).resolve().parent / "static"
# rc_filters.js escapes with the shared ``rcEsc`` (html_safe.ESC_JS), so the two travel together.
FILTERS_JS = ESC_JS + (_STATIC / "rc_filters.js").read_text(encoding="utf-8")
FILTERS_CSS = "<style>" + (_STATIC / "rc_filters.css").read_text(encoding="utf-8") + "</style>"


def filters_bar_html(el_id: str = "rc-filters") -> str:
    """The container ``RCFilters.mount`` fills (Index → Market cap → Sector →
    Theme → Earnings, with an ⓘ explanation on each)."""
    return f'<section class="rcf-bar" id="{html.escape(el_id, quote=True)}"></section>'


def control_html(key: str, label: str, control: str, explain_title: str,
                 explain_html: str, now_html: str = "") -> str:
    """One board-specific control with its ⓘ explanation beside the label.

    ``control`` and ``explain_html`` are trusted markup built by the board;
    ``key``/``label``/``explain_title`` are escaped. The popover opens on click
    (never hover), one at a time, Esc closes it (rc_filters.js ``RCInfo``).
    """
    k = html.escape(key, quote=True)
    lab = html.escape(label)
    now = f'<div class="rcf-now">{now_html}</div>' if now_html else ""
    return (f'<div class="rcf-ctrl"><div class="rcf-lbl"><span id="rcb-l-{k}">{lab}</span>'
            f'<button class="rcf-info" type="button" aria-expanded="false" '
            f'aria-controls="rcb-i-{k}" aria-label="About {html.escape(label, quote=True)}">i</button></div>'
            f'{control}'
            f'<div class="rcf-pop" id="rcb-i-{k}" role="region" '
            f'aria-label="{html.escape(explain_title, quote=True)} explained" hidden>'
            f'<h4>{html.escape(explain_title)}</h4>{explain_html}{now}</div></div>')


def combobox_html(input_id: str, label_id: str, placeholder: str) -> str:
    """Input + listbox for ``RCCombobox.mount`` (ARIA 1.2 editable combobox)."""
    i = html.escape(input_id, quote=True)
    return (f'<div class="rcf-combo"><input id="{i}" type="text" role="combobox" '
            f'aria-labelledby="{html.escape(label_id, quote=True)}" aria-autocomplete="list" '
            f'aria-expanded="false" aria-controls="{i}-list" autocomplete="off" spellcheck="false" '
            f'placeholder="{html.escape(placeholder, quote=True)}">'
            f'<button class="rcf-clear" type="button" data-clear-for="{i}" aria-label="Clear search">×</button>'
            f'<ul id="{i}-list" class="rcf-list" role="listbox" aria-label="Matching names" hidden></ul></div>')


# ── The "add to list" button (#786 step 5; was the Stock / Option pair of PR 4, D2) ──
# One 44 px bookmark next to a ticker. Pressing it opens a menu of every watch list: avo's own named lists first
# ("Your lists"), then the two the jobs read ("Read by the jobs": the stock list LEAPS, prices and Earnings take, the
# option list the options build takes). Each row is a checkbox with the list's size; a tick puts the ticker on that
# list, an untick takes it off (``POST /api/watchlists/<id>/tick/<ticker>``, ``module_watchlists/menu.py``). The
# button shows how many lists hold the ticker. A board renders it with ``watch_buttons_html`` (server side) or
# ``rcWatchButtons`` (its JS) and mounts ``watchlist_html(board)`` once; the board becomes ``X-RC-Panel``, the
# rows' ``added_by``. One delegated click handler reads ``data-ticker`` (never an interpolated value, ADR-X1); which
# lists hold which tickers is read live (``GET /api/watchlists``) when the page loads. A ticker the dashboard's rule
# (``app/dashboard.py`` ``_TICKER_RE``) refuses gets no button. On a phone the menu is a bottom sheet with 44 px
# rows; the button is ``aria-haspopup``, the rows ``menuitemcheckbox``, Escape closes and focus returns.
WATCH_TICKER_PATTERN = r"[A-Z0-9][A-Z0-9.\-]{0,9}"
WATCH_ICON = ('<svg class="rc-watch-ico" viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" '
              'focusable="false"><path d="M4 2h8v12l-4-3-4 3z" fill="var(--rc-watch-fill,none)" '
              'stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>')
_WATCH_TICKER_RE = re.compile(WATCH_TICKER_PATTERN)

WATCHLIST_CSS = """<style>
.rc-watch{display:inline-flex;align-items:center;vertical-align:middle;margin:0 6px 0 0;flex:none}
.rc-watch button{position:relative;display:flex;align-items:center;justify-content:center;gap:2px;min-width:44px;
  min-height:44px;padding:0 6px;box-sizing:border-box;cursor:pointer;border:1px solid var(--input-border,#2F434C);
  border-radius:8px;background:var(--input-bg,#101F33);color:var(--muted,#9C9DA1);
  transition:color 150ms ease,background-color 150ms ease}
.rc-watch button:hover{color:var(--fg,#C5C6C8);background:var(--hover,#18293a)}
.rc-watch button[data-on]:not([data-on="0"]){color:var(--sky,#0EA5E9);--rc-watch-fill:currentColor;
  background:color-mix(in oklab, var(--sky,#0EA5E9) 16%, var(--input-bg,#101F33))}
.rc-watch button[aria-expanded="true"]{outline:2px solid var(--sky,#0EA5E9);outline-offset:-2px}
.rc-watch button:focus-visible{outline:2px solid var(--focus,var(--sky,#0EA5E9));outline-offset:-2px}
.rc-watch-n{font:600 10.5px/1 system-ui,sans-serif;font-variant-numeric:tabular-nums}
.rc-watch-n:empty{display:none}
.rc-watch-row{display:flex;margin:4px 0 2px}
.rc-watch-row .rc-watch{margin-left:0}
.rc-wm{position:fixed;z-index:1000;box-sizing:border-box;width:320px;max-width:calc(100vw - 16px);max-height:min(70vh,520px);
  overflow-y:auto;overscroll-behavior:contain;padding:6px 0;border:1px solid var(--border,#232a38);border-radius:10px;
  background:var(--card,#12161e);color:var(--fg,#d7dce5);box-shadow:0 10px 30px rgba(0,0,0,.35);
  font:13px/1.3 system-ui,sans-serif}
.rc-wm[hidden]{display:none}
.rc-wm-top{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:4px 12px 6px}
.rc-wm-top b{font-size:13.5px}
.rc-wm-x{min-width:44px;min-height:44px;border:0;background:none;color:var(--muted,#9C9DA1);font-size:20px;cursor:pointer;
  display:none}
.rc-wm-h{padding:8px 12px 4px;color:var(--muted,#9C9DA1);font-size:11px;font-weight:600;letter-spacing:.04em;
  text-transform:uppercase}
.rc-wm-i{display:flex;align-items:center;gap:10px;width:100%;min-height:36px;padding:6px 12px;box-sizing:border-box;
  border:0;background:none;color:inherit;font:inherit;text-align:left;cursor:pointer}
.rc-wm-i:hover,.rc-wm-i:focus-visible{background:var(--hover,#18293a);outline:none}
.rc-wm-i:disabled{opacity:.6;cursor:progress}
.rc-wm-box{flex:none;width:16px;height:16px;box-sizing:border-box;border:1.5px solid var(--muted,#9C9DA1);border-radius:4px;
  display:flex;align-items:center;justify-content:center}
.rc-wm-i[aria-checked="true"] .rc-wm-box{background:var(--sky,#0EA5E9);border-color:var(--sky,#0EA5E9)}
.rc-wm-i[aria-checked="true"] .rc-wm-box::after{content:"";width:8px;height:4px;margin-top:-2px;border:solid var(--card,#12161e);
  border-width:0 0 2px 2px;transform:rotate(-45deg)}
.rc-wm-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rc-wm-c{flex:none;color:var(--muted,#9C9DA1);font-variant-numeric:tabular-nums;font-size:12px}
.rc-wm-i[data-error] .rc-wm-c{color:var(--neg,#F87171)}
.rc-wm-msg{margin:6px 12px 2px;color:var(--neg,#F87171);font-size:12px}
.rc-wm-msg:empty{display:none}
.rc-wm-foot{display:block;margin-top:4px;padding:10px 12px 6px;border-top:1px solid var(--border,#232a38);
  color:var(--sky,#0EA5E9);text-decoration:none;font-size:12.5px}
.rc-wm-empty{padding:8px 12px;color:var(--muted,#9C9DA1)}
.rc-wm-back{position:fixed;inset:0;z-index:999;background:rgba(0,0,0,.4)}
.rc-wm-back[hidden]{display:none}
@media (max-width:600px){
  .rc-wm{left:0 !important;right:0;top:auto !important;bottom:0;width:100%;max-width:100%;max-height:75vh;
    border-radius:14px 14px 0 0;padding-bottom:max(8px, env(safe-area-inset-bottom))}
  .rc-wm-x{display:block}
  .rc-wm-i{min-height:44px}
  .rc-wm-foot{min-height:44px;box-sizing:border-box;display:flex;align-items:center}
}
</style>"""

WATCHLIST_JS = ESC_JS + r"""
var RC_WATCH = {board: 'board', lists: [], loaded: false, touched: {}, open: null};
var RC_WATCH_RE = /^""" + WATCH_TICKER_PATTERN + r"""$/;
var RC_WATCH_ICON = """ + json.dumps(WATCH_ICON) + r""";
var RC_WATCH_GROUPS = ['Your lists', 'Read by the jobs'];
function rcWatchTicker(t){ t = String(t == null ? '' : t).trim().toUpperCase(); return RC_WATCH_RE.test(t) ? t : ''; }
// On a card, the button gets its own line under the ticker (a ticker line that clips a long name must not clip it).
function rcWatchRow(ticker){ var b = rcWatchButtons(ticker); return b ? '<div class="rc-watch-row">' + b + '</div>' : ''; }
function rcWatchCount(t){
  var n = 0;
  RC_WATCH.lists.forEach(function(l){ if(l.has[t]) n++; });
  return n;
}
function rcWatchTitle(t, n){
  return n ? t + ' is on ' + n + (n === 1 ? ' watch list' : ' watch lists') : 'Add ' + t + ' to a watch list';
}
function rcWatchButtons(ticker){
  var t = rcWatchTicker(ticker);
  if(!t) return '';
  var n = rcWatchCount(t);
  return '<span class="rc-watch"><button type="button" data-rc-watch-menu="" data-ticker="' + rcEsc(t) +
    '" data-on="' + n + '" aria-haspopup="menu" aria-expanded="false" aria-label="' + rcEsc(rcWatchTitle(t, n)) +
    '" title="' + rcEsc(rcWatchTitle(t, n)) + '">' + RC_WATCH_ICON + '<span class="rc-watch-n" aria-hidden="true">' +
    (n ? n : '') + '</span></button></span>';
}
function rcWatchMark(btn){
  var t = rcWatchTicker(btn.getAttribute('data-ticker'));
  if(!t) return;
  var n = rcWatchCount(t), title = rcWatchTitle(t, n);
  btn.setAttribute('data-on', String(n));
  btn.setAttribute('aria-label', title);
  btn.setAttribute('title', title);
  var badge = btn.querySelector ? btn.querySelector('.rc-watch-n') : null;
  if(badge) badge.textContent = n ? String(n) : '';
}
function rcWatchSync(root){
  var all = (root || document).querySelectorAll('[data-rc-watch-menu]');
  for(var i = 0; i < all.length; i++) rcWatchMark(all[i]);
}
function rcWatchInit(board){
  RC_WATCH.board = String(board || 'board');
  return fetch('/api/watchlists', {cache: 'no-store', credentials: 'same-origin'}).then(function(r){ return r.json(); })
    .then(function(d){
      if(!d || !d.ok) return;
      var before = {};
      RC_WATCH.lists.forEach(function(l){ before[l.id] = l.has; });
      RC_WATCH.lists = (d.lists || []).map(function(l){
        var has = {};
        (l.tickers || []).forEach(function(t){ has[String(t)] = true; });
        // A tick that answered before this read is newer than it: keep the tick's state for that ticker.
        Object.keys(RC_WATCH.touched).forEach(function(key){
          var at = key.lastIndexOf('|'), id = key.slice(0, at), t = key.slice(at + 1);
          if(id !== String(l.id)) return;
          if(RC_WATCH.touched[key]) has[t] = true; else delete has[t];
        });
        return {id: String(l.id), name: String(l.name || l.id), group: String(l.group || ''),
                count: Number(l.count) || 0, has: has};
      });
      RC_WATCH.loaded = true;
      rcWatchSync(document);
      if(RC_WATCH.open) rcWatchDraw();
    }).catch(function(){});
}
function rcWatchMenuEl(){
  var m = document.getElementById('rc-watch-menu');
  if(m) return m;
  var back = document.createElement('div');
  back.className = 'rc-wm-back'; back.id = 'rc-watch-back'; back.hidden = true;
  m = document.createElement('div');
  m.className = 'rc-wm'; m.id = 'rc-watch-menu'; m.setAttribute('role', 'menu'); m.hidden = true;
  document.body.appendChild(back);
  document.body.appendChild(m);
  return m;
}
function rcWatchDraw(){
  var o = RC_WATCH.open, m = rcWatchMenuEl();
  if(!o) return;
  var t = o.ticker, html = '<div class="rc-wm-top"><b>' + rcEsc(t) + ' · Watch lists</b>' +
    '<button type="button" class="rc-wm-x" data-rc-wm-close="" aria-label="Close">×</button></div>';
  if(!RC_WATCH.loaded) html += '<div class="rc-wm-empty">Reading the lists…</div>';
  RC_WATCH_GROUPS.forEach(function(g){
    var rows = RC_WATCH.lists.filter(function(l){ return l.group === g; });
    if(!rows.length) return;
    html += '<div role="group" aria-label="' + rcEsc(g) + '"><div class="rc-wm-h" aria-hidden="true">' + rcEsc(g) +
      '</div>' + rows.map(function(l){
        var on = !!l.has[t], err = o.errors[l.id];
        return '<button type="button" class="rc-wm-i" role="menuitemcheckbox" aria-checked="' + (on ? 'true' : 'false') +
          '" data-rc-wm-list="' + rcEsc(l.id) + '"' + (o.busy[l.id] ? ' disabled' : '') +
          (err ? ' data-error="1"' : '') + ' title="' + rcEsc((on ? 'Take ' + t + ' off ' : 'Put ' + t + ' on ') + l.name) +
          '"><span class="rc-wm-box" aria-hidden="true"></span><span class="rc-wm-name">' + rcEsc(l.name) +
          '</span><span class="rc-wm-c">' + (err ? 'not changed' : l.count) + '</span></button>';
      }).join('') + '</div>';
  });
  if(RC_WATCH.loaded && !RC_WATCH.lists.length) html += '<div class="rc-wm-empty">No watch lists yet.</div>';
  html += '<div class="rc-wm-msg" role="status">' + rcEsc(o.msg || '') + '</div>' +
    '<a class="rc-wm-foot" href="/view/module_watchlists">Open Watch lists</a>';
  m.innerHTML = html;
}
function rcWatchPlace(m, btn){
  var r = btn.getBoundingClientRect(), w = m.offsetWidth || 320, h = m.offsetHeight || 0;
  var vw = window.innerWidth || document.documentElement.clientWidth, vh = window.innerHeight || 800;
  var left = Math.max(8, Math.min(r.right - w, vw - w - 8));
  var top = r.bottom + 4;
  if(top + h > vh - 8 && r.top - h - 4 >= 8) top = r.top - h - 4;
  m.style.left = left + 'px';
  m.style.top = Math.max(8, top) + 'px';
}
function rcWatchItems(){ return Array.prototype.slice.call(rcWatchMenuEl().querySelectorAll('.rc-wm-i')); }
function rcWatchOpen(btn){
  var t = rcWatchTicker(btn.getAttribute('data-ticker'));
  if(!t) return;
  if(RC_WATCH.open && RC_WATCH.open.btn === btn){ rcWatchClose(true); return; }
  rcWatchClose(false);
  RC_WATCH.open = {btn: btn, ticker: t, busy: {}, errors: {}, msg: ''};
  btn.setAttribute('aria-expanded', 'true');
  var m = rcWatchMenuEl();
  m.setAttribute('aria-label', 'Watch lists for ' + t);
  rcWatchDraw();
  m.hidden = false;
  document.getElementById('rc-watch-back').hidden = !(window.matchMedia && window.matchMedia('(max-width:600px)').matches);
  rcWatchPlace(m, btn);
  var first = rcWatchItems()[0];
  if(first && first.focus) first.focus();
}
function rcWatchClose(refocus){
  var o = RC_WATCH.open;
  if(!o) return;
  RC_WATCH.open = null;
  o.btn.setAttribute('aria-expanded', 'false');
  var m = document.getElementById('rc-watch-menu'), back = document.getElementById('rc-watch-back');
  if(m) m.hidden = true;
  if(back) back.hidden = true;
  if(refocus && o.btn.focus) o.btn.focus();
}
function rcWatchTick(listId){
  var o = RC_WATCH.open, t = o ? o.ticker : '', list = null;
  RC_WATCH.lists.forEach(function(l){ if(l.id === listId) list = l; });
  if(!o || !t || !list || o.busy[listId]) return null;
  var want = !list.has[t];
  o.busy[listId] = true; delete o.errors[listId]; o.msg = '';
  rcWatchDraw();
  return fetch('/api/watchlists/' + encodeURIComponent(listId) + '/tick/' + encodeURIComponent(t),
               {method: 'POST', credentials: 'same-origin',
                headers: {'X-RC-Panel': RC_WATCH.board, 'Content-Type': 'application/json'},
                body: JSON.stringify({on: want})})
    .then(function(r){ return r.json().then(function(d){ return {status: r.status, d: d || {}}; }); })
    .then(function(res){
      delete o.busy[listId];
      if(!res.d.ok){
        o.errors[listId] = true;
        o.msg = 'Not changed: ' + String(res.d.error || ('HTTP ' + res.status));
      } else {
        var on = !!res.d.on;
        if(on && !list.has[t]) list.count++;
        if(!on && list.has[t]) list.count = Math.max(0, list.count - 1);
        if(on) list.has[t] = true; else delete list.has[t];
        RC_WATCH.touched[listId + '|' + t] = on;
      }
      rcWatchSync(document);
      if(RC_WATCH.open === o){ rcWatchDraw(); rcWatchRefocus(listId); }
    }).catch(function(e){
      delete o.busy[listId];
      o.errors[listId] = true;
      o.msg = 'Not changed: ' + String(e);
      if(RC_WATCH.open === o){ rcWatchDraw(); rcWatchRefocus(listId); }
    });
}
function rcWatchRefocus(listId){
  rcWatchItems().forEach(function(b){ if(b.getAttribute('data-rc-wm-list') === listId && b.focus) b.focus(); });
}
// Capture phase: a row or card that opens on click must not also open when its ticker's button is pressed.
document.addEventListener('click', function(ev){
  var el = ev.target && ev.target.closest ? ev.target : null;
  if(!el) return;
  var btn = el.closest('[data-rc-watch-menu]');
  if(btn){ ev.preventDefault(); ev.stopPropagation(); rcWatchOpen(btn); return; }
  var item = el.closest('[data-rc-wm-list]');
  if(item){ ev.preventDefault(); ev.stopPropagation(); rcWatchTick(item.getAttribute('data-rc-wm-list')); return; }
  if(el.closest('[data-rc-wm-close]')){ ev.preventDefault(); rcWatchClose(true); return; }
  if(RC_WATCH.open && !el.closest('#rc-watch-menu')) rcWatchClose(false);
}, true);
document.addEventListener('keydown', function(ev){
  if(!RC_WATCH.open) return;
  if(ev.key === 'Escape'){ ev.preventDefault(); rcWatchClose(true); return; }
  if(ev.key === 'Tab'){ rcWatchClose(false); return; }
  if(ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp' && ev.key !== 'Home' && ev.key !== 'End') return;
  var items = rcWatchItems();
  if(!items.length) return;
  ev.preventDefault();
  var at = items.indexOf(document.activeElement), next;
  if(ev.key === 'Home') next = 0;
  else if(ev.key === 'End') next = items.length - 1;
  else next = (at + (ev.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
  items[next].focus();
});
window.addEventListener('resize', function(){ if(RC_WATCH.open) rcWatchPlace(rcWatchMenuEl(), RC_WATCH.open.btn); });
window.addEventListener('scroll', function(){ if(RC_WATCH.open) rcWatchPlace(rcWatchMenuEl(), RC_WATCH.open.btn); }, true);
"""


def watch_buttons_html(ticker, on=()) -> str:
    """The "add to list" button for one ticker, rendered on the server. ``on`` names lists the board already
    knows hold the ticker (LEAPS knows its stock list), so the count shows before the live read; ``rcWatchInit``
    re-marks every button from the live lists anyway. With ``on`` empty this is exactly ``rcWatchButtons``'s."""
    t = str(ticker if ticker is not None else "").strip().upper()
    if not _WATCH_TICKER_RE.fullmatch(t):
        return ""
    e = html.escape(t, quote=True)
    n = len(tuple(on or ()))
    title = (f"{e} is on {n} watch list{'' if n == 1 else 's'}" if n else f"Add {e} to a watch list")
    return (f'<span class="rc-watch"><button type="button" data-rc-watch-menu="" data-ticker="{e}" data-on="{n}" '
            f'aria-haspopup="menu" aria-expanded="false" aria-label="{title}" title="{title}">{WATCH_ICON}'
            f'<span class="rc-watch-n" aria-hidden="true">{n if n else ""}</span></button></span>')


def watchlist_html(board: str) -> str:
    """Mount the menu on a board once: the CSS, the snippet and ``rcWatchInit(board)``. ``board`` becomes the
    ``X-RC-Panel`` value, so the list rows say which board added them."""
    from .html_safe import script_json
    return f"{WATCHLIST_CSS}\n<script>{WATCHLIST_JS}\nrcWatchInit({script_json(str(board))});\n</script>"


def filters_meta(board: str, **fields) -> dict:
    """The ``meta`` a board passes to ``RCFilters.mount`` (K1, the filter contract).

    ``board`` and the board's own fields (``floor_mc``, ``earnings_mode``, ``themes``, ``facets``, ``explain``,
    ``now``) are passed through; the index list comes from the registry (``index_registry.filter_labels``), so
    the component carries no literal list and a stored S&P SmallCap 600 shows up without a code change.
    """
    from .index_registry import SHORT_LABELS, filter_labels

    labels = filter_labels()
    return {"board": board, **fields, "indexes": labels,
            "index_labels": {k: v for k, v in SHORT_LABELS.items() if k in labels}}


__all__ = [
    "FILTERS_JS", "FILTERS_CSS", "filters_bar_html", "control_html", "combobox_html",
    "filters_meta",
    "WATCHLIST_JS", "WATCHLIST_CSS", "WATCH_ICON", "watch_buttons_html", "watchlist_html",
    "JOB_JS",
    "HELP_CSS", "help_html",
    "THEME_TOKENS", "THEME_SCALE", "THEME_CSS", "THEME_JS", "THEME_STORAGE_KEY",
    "universe_provenance_html",
    "provider_health_html",
    "notice_html",
    "write_artifact", "write_board", "payload_path", "PAYLOAD_JS",
    "recorded_sessions_html",
]


def help_html(body: str) -> str:
    """The shared collapsible "What am I looking at?" box for a board."""
    return (HELP_CSS
            + '\n<details class="help"><summary>What am I looking at?</summary>'
            + f'\n<div class="body">{body}</div></details>')


# ── "Recorded sessions": what the result history holds for a board (#383 C7, K2) ──
HISTORY_CSS = """<style>
.rc-history{background:var(--card,#12161e);border:1px solid var(--border,#232a38);border-radius:10px;margin:14px 0;color:var(--muted,#8a93a6);font-size:12.5px}
.rc-history summary{cursor:pointer;padding:9px 14px;font-weight:600;color:var(--sky,#0EA5E9)}
.rc-history .body{padding:0 14px 12px;line-height:1.6;overflow-x:auto}
.rc-history table{border-collapse:collapse;width:100%;font-size:12px}
.rc-history th,.rc-history td{text-align:left;padding:4px 8px;border-top:1px solid var(--border,#232a38);vertical-align:top}
.rc-history th{color:var(--fg,#d7dce5);font-weight:600}
""" + _SUMMARY_TOUCH % ".rc-history" + """
</style>"""
_HISTORY_NOTES = 2          # recorded warnings shown per row; the rest are counted


def _utc(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _history_notes(h: dict) -> str:
    notes = [str(n) for n in (h.get("quality_note"), h.get("error")) if n]
    warnings = [str(w) for w in (h.get("warnings") or []) if w]
    notes += warnings[:_HISTORY_NOTES]
    if len(warnings) > _HISTORY_NOTES:
        notes.append(f"and {len(warnings) - _HISTORY_NOTES} more")
    return "; ".join(notes)


def _history_params(h: dict) -> str:
    p = h.get("params")
    text = ", ".join(f"{k}={v}" for k, v in p.items()) if isinstance(p, dict) else str(p or "")
    return text if len(text) <= 80 else text[:79] + "…"


def _history_rows(h: dict) -> int:
    counts = h.get("row_counts")
    return sum(int(v or 0) for v in counts.values()) if isinstance(counts, dict) else 0


_HISTORY_READ_NOTE = "Read when this page was rendered, so a Fetch shows itself from the next Refresh."


def recorded_sessions_html(board: str, limit: int = 10, *, panel_id: str = "history-panel",
                           read_note: str | None = None) -> str:
    """The shared "Recorded sessions" block: the board's result history, newest first (K2 readers).

    A session board lists its session snapshots (every Fetch of the session counted, the snapshot's rows kept); a run
    board lists its runs. Read when the page renders, so a Fetch shows itself from the next render. A read never
    creates the history file, waits only briefly for another RevenueChaser process that holds it, and a failed read is
    shown here, never a failed render. Every string is escaped.

    ``panel_id`` is the block's element id (a second block on one page needs its own). ``read_note`` replaces the
    sentence on when the block is read, for a board whose page is not re-rendered after its Fetch is recorded
    (Earnings) or that lives on another board's page (Money Flow); it is also added to the "nothing recorded" line.
    """
    from RevenueChaser.core import history, rawstore

    session = history.is_session_board(board)
    title = "Recorded sessions (result history)" if session else "Recorded runs (result history)"
    head = (HISTORY_CSS + f'<details class="rc-history" id="{html.escape(panel_id)}"><summary>{title}</summary>'
            '<div class="body">')
    try:
        with rawstore.brief_wait():
            found = history.sessions(board, limit) if session else history.runs(board, limit)
    except Exception as exc:  # noqa: BLE001 - the board renders without its history rows
        first = str(exc).splitlines()[0] if str(exc) else ""
        return head + f"<p>History could not be read: {html.escape(f'{type(exc).__name__}: {first}')}</p></div></details>"
    if not found:
        empty = ("No session recorded yet: the first complete Fetch after a session close records one." if session
                 else "No run recorded yet: the next Fetch records one.")
        if read_note:
            empty += " " + read_note
        return head + f"<p>{html.escape(empty)}</p></div></details>"
    e = html.escape
    if session:
        intro = ("One snapshot per session: the first complete Fetch after the close (later Fetches of the session "
                 "are counted, their rows are not kept).")
        cols = "<tr><th>Session</th><th>Fetches</th><th>Recorded</th><th>Rows kept</th><th>Notes</th></tr>"
        rows = "".join(
            f"<tr><td>{e(str(h.get('session_date') or ''))}</td><td>{e(str(h.get('runs', 0)))}</td>"
            f"<td>{e(_utc(h.get('run_at')))}</td><td>{_history_rows(h)}</td><td>{e(_history_notes(h))}</td></tr>"
            for h in found)
    else:
        intro = "Every Fetch is recorded with its rows."
        cols = ("<tr><th>Recorded</th><th>Session</th><th>Quality</th><th>Rows</th><th>Params</th>"
                "<th>Notes</th></tr>")
        rows = "".join(
            f"<tr><td>{e(_utc(h.get('run_at')))}</td><td>{e(str(h.get('session_date') or ''))}</td>"
            f"<td>{e(str(h.get('quality') or ''))}</td><td>{_history_rows(h)}</td>"
            f"<td>{e(_history_params(h))}</td>"
            f"<td>{e(_history_notes(h))}</td></tr>"
            for h in found)
    return (head + f"<p>{intro} {html.escape(read_note or _HISTORY_READ_NOTE)}</p>"
            f"<table>{cols}{rows}</table></div></details>")


def universe_provenance_html(generated_at, refreshed_by, partial: bool = False) -> str:
    """Shared-universe provenance line for a report header.

    **On the identity spine** (``universe.spine_provenance``, build plan PR 6) the line names the spine, the sources it
    is built from, the day of the SEC file and when the live spine file was built. The names no longer come from the
    shared universe cache, so ``refreshed_by`` / ``generated_at`` describe only what that cache still feeds, the market
    caps, and ``partial`` says those caps are partly stale (the last Yahoo pull did not complete, #146).

    **On the old store** it shows which board last refreshed the shared universe cache and when, so every board that
    reuses that data can display the same attribution; ``partial`` adds "partial fetch": the universe was upserted and
    names the pull missed were kept. Empty when no provenance is recorded (legacy payload / first run).
    """
    from .universe import has_market_caps, spine_provenance

    ts = ""
    if refreshed_by:
        try:
            dt = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
            ts = dt.strftime("%b %d, %Y %H:%M") + " UTC"
        except (ValueError, TypeError):
            ts = str(generated_at or "")
    spine = spine_provenance()
    if spine is not None:
        parts = ['Universe: <b style="color:var(--text)">identity spine</b> '
                 "(SEC company tickers, OpenFIGI, Compustat, index fund files)"]
        if spine.get("source_as_of"):
            parts.append("SEC file of " + html.escape(str(spine["source_as_of"])))
        if spine.get("built_at"):
            parts.append("built " + html.escape(str(spine["built_at"])))
        if not has_market_caps():
            # Opus review N5: no cap stored (until G2 brings market data), so every screened board is empty; the
            # header says why instead of a "refreshed by" that refreshed nothing.
            parts.append('<b style="color:var(--amber,#f1c40f)">market caps: none loaded yet</b>')
        elif refreshed_by:
            parts.append('market caps refreshed by <b style="color:var(--text)">' + html.escape(refreshed_by) + "</b>"
                         + ((" " + html.escape(ts)) if ts else ""))
        if partial:
            parts.append('<b style="color:var(--amber,#f1c40f)">market caps partly stale</b>')
        return '<div style="color:var(--muted);font-size:11px;margin-top:3px">' + " · ".join(parts) + "</div>"
    flag = (' · <b style="color:var(--amber,#f1c40f)">partial fetch</b>' if partial else "")
    if not refreshed_by:
        return ('<div style="color:var(--muted);font-size:11px;margin-top:3px">Universe' + flag + '</div>'
                if partial else "")
    return (
        '<div style="color:var(--muted);font-size:11px;margin-top:3px">'
        'Universe refreshed by <b style="color:var(--text)">' + html.escape(refreshed_by) + '</b>'
        + ((" · " + html.escape(ts)) if ts else "") + flag + '</div>'
    )


# Banner names of the providers whose health a board shows (K3); any other ``provider`` is shown as given.
PROVIDER_NAMES = {"unusual_whales": "Unusual Whales", "yfinance": "Yahoo Finance", "ibkr": "Interactive Brokers"}


def provider_health_html(health: dict | None) -> str:
    """Amber "data incomplete" banner for a provider-backed board (MB3 / ADR-1, Common K3).

    ``health`` is a provider's health captured by the runner (``UWClient.health()``, ``yfinance.health()``):
    ``{provider, calls, failed, auth_error, failures[]}``; one without ``provider`` is Unusual Whales', whose
    text is unchanged. Empty when the run was clean (or no health was recorded); otherwise an always-visible
    alert naming the provider, with the failure count and one example, so an outage cannot pass for a quiet
    market. Self-contained inline style; every provider string escaped.
    """
    if not health:
        return ""
    failed = int(health.get("failed") or 0)
    auth = health.get("auth_error")
    if failed <= 0 and not auth:
        return ""
    provider = health.get("provider") or "unusual_whales"
    name = html.escape(PROVIDER_NAMES.get(provider, str(provider)))
    parts = []
    if auth and provider == "unusual_whales":
        parts.append("Unusual Whales rejected the API key (HTTP "
                     + html.escape(str(auth.get("status", "?"))) + ") — check "
                     "UNUSUAL_WHALES_API_KEY in .env.")
    elif auth:
        parts.append(f"{name} rejected the request (HTTP " + html.escape(str(auth.get("status", "?"))) + ").")
    if failed > 0:
        calls = int(health.get("calls") or failed)
        line = f"{failed} of {calls} {name} calls failed"
        first = (health.get("failures") or [None])[0]
        if first:
            why = first.get("status") or first.get("error") or "error"
            line += " (e.g. " + html.escape(f"{first.get('endpoint', '')}: {why}") + ")"
        parts.append(line + ".")
    return (
        '<div class="rc-provider-health" role="alert" style="margin:8px 0 12px;'
        'padding:10px 12px;border:1px solid var(--amber,#f1c40f);border-radius:8px;'
        'background:rgba(241,196,15,.08);color:var(--amber,#f1c40f);font-size:13px;'
        'line-height:1.45"><b>Data incomplete.</b> ' + " ".join(parts)
        + " This board may be missing rows — run Fetch again later.</div>"
    )


def notice_html(text: str | None) -> str:
    """One-line amber note above a board (escaped); empty for no text."""
    if not text:
        return ""
    return (
        '<div class="rc-notice" role="status" style="margin:8px 0 12px;padding:10px 12px;'
        'border:1px solid var(--amber,#f1c40f);border-radius:8px;'
        'background:rgba(241,196,15,.08);color:var(--amber,#f1c40f);font-size:13px;'
        'line-height:1.45">' + html.escape(str(text)) + "</div>"
    )


def write_artifact(path, text: str) -> Path:
    """Write a rendered board atomically: temp file in the same folder, then rename.

    A Refresh renders in a request thread while the job worker may write the same
    ``*_latest.html``; with a plain ``write_text`` a reader could see a half-written
    page, or two writers interleave. ``os.replace`` swaps the whole file at once.
    """
    return _write_atomic(path, text.encode("utf-8"))


def _write_atomic(path, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ── K10: a board's data outside its HTML (#383 C6a, card F) ──
# The page loads the HTML (chrome and scripts), then fetches its rows from
# ``GET /view/<mod>/payload.json``. The file is stored gzip-compressed and served
# as it is with ``Content-Encoding: gzip``: card F counts what one board view
# downloads, compressed, and JSON rows compress about tenfold.
PAYLOAD_SUFFIX = ".payload.json.gz"

# The page side: ``rcPayload("module_x").then(function(data){ ... })``.
PAYLOAD_JS = r"""
function rcPayload(mod){
  return fetch("/view/" + encodeURIComponent(mod) + "/payload.json", {cache: "no-store", credentials: "same-origin"})
    .then(function(r){ if(!r.ok) throw new Error("payload " + r.status); return r.json(); });
}
"""


def payload_path(html_path) -> Path:
    """The board's payload file: next to its HTML, ``<stem>.payload.json.gz``."""
    p = Path(html_path)
    return p.with_name(p.stem + PAYLOAD_SUFFIX)


def write_board(html_path, html_text: str, payload=None) -> Path:
    """Write a board's HTML and, with ``payload``, its payload file (K10); without one, drop a stale payload.

    The payload goes first, so a page loaded from the new HTML never fetches the old rows. ``mtime=0`` keeps the
    gzip bytes the same for the same data.
    """
    pp = payload_path(html_path)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
        _write_atomic(pp, gzip.compress(data, compresslevel=6, mtime=0))
    path = write_artifact(html_path, html_text)
    if payload is None:
        pp.unlink(missing_ok=True)
    return path
