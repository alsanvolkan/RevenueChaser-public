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
# The one rule for how long a watch-list name may be lives with the store that keeps it (#774); the menu's
# ``maxlength`` is that same number, so the input cannot offer what the route would refuse.
from RevenueChaser.rcdata.ops import WATCH_NAME_MAX

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
# 25 Oct 2026) and is the shell's look before #184, value for value. Light applies when the reader chooses
# Light, or chooses Auto on a device that prefers light. The choice is ``localStorage[THEME_STORAGE_KEY]``
# ("light" / "auto"; absent = dark), set as ``data-theme`` on ``<html>`` ("dark" / "light" / "auto"). The
# shell copies that attribute onto every board iframe (same origin).
THEME_STORAGE_KEY = "rc-theme"
THEME_TOKENS = {
    "dark": {
        "bg": "#05111F", "fg": "#C5C6C8", "bright": "#F0F0F0", "card": "#0B1828",
        "border": "rgba(197,198,200,.15)", "sidebar": "#0B1828", "pos": "#34D399", "neg": "#F87171",
        "amber": "#FBBF24", "sky": "#0EA5E9", "muted": "#9C9DA1",
        "input-bg": "#101F33", "input-border": "#2F434C", "hover": "#18293a", "topbar": "rgba(12,20,24,.8)",
        "nav-fg": "rgba(224,233,235,.75)", "nav-on": "#101F33", "hover-faint": "rgba(255,255,255,.04)",
        "frame-bg": "#12131c", "overlay": "rgba(5,17,31,.7)", "scroll-thumb": "#5b7a8c",
        "scroll-thumb-hover": "#6f93a7", "scroll-track": "#0b1620", "primary-bg": "#E2E3E5",
        "primary-fg": "#0A1420", "oi-bg": "rgba(11,24,40,.55)", "scrim": "rgba(0,0,0,.55)",
        "shadow": "rgba(0,0,0,.6)", "badge-blue": "#38BDF8", "badge-gray": "#94A3B8",
        "hover-soft": "rgba(255,255,255,.06)", "hover-day": "rgba(255,255,255,.08)",
        "gex-pos-bg": "rgba(52,211,153,.35)", "gex-pos-fg": "#eafff5",
        "gex-neg-bg": "rgba(248,113,113,.35)", "gex-neg-fg": "#ffd9d9",
    },
    "light": {
        "bg": "#F4F6F9", "fg": "#2A3340", "bright": "#0B1220", "card": "#FFFFFF",
        "border": "rgba(15,23,42,.14)", "sidebar": "#FFFFFF", "pos": "#047857", "neg": "#B91C1C",
        "amber": "#B45309", "sky": "#0369A1", "muted": "#5B6472",
        "input-bg": "#FFFFFF", "input-border": "#C3CCD8", "hover": "#E8EDF3", "topbar": "rgba(255,255,255,.85)",
        "nav-fg": "#3A4452", "nav-on": "#E3EEF8", "hover-faint": "rgba(15,23,42,.05)",
        "frame-bg": "#FFFFFF", "overlay": "rgba(244,246,249,.75)", "scroll-thumb": "#9AA8B6",
        "scroll-thumb-hover": "#7D8D9E", "scroll-track": "#E6EBF0", "primary-bg": "#0B1220",
        "primary-fg": "#FFFFFF", "oi-bg": "rgba(255,255,255,.7)", "scrim": "rgba(15,23,42,.35)",
        "shadow": "rgba(15,23,42,.18)", "badge-blue": "#0369A1", "badge-gray": "#475569",
        "hover-soft": "rgba(15,23,42,.06)", "hover-day": "rgba(15,23,42,.09)",
        "gex-pos-bg": "rgba(4,120,87,.16)", "gex-pos-fg": "#065F46",
        "gex-neg-bg": "rgba(185,28,28,.14)", "gex-neg-fg": "#991B1B",
    },
}


# ── Scale tokens: one set, both schemes (#764) ──
# Colour is per scheme; size, rhythm and timing are not, so these live outside ``THEME_TOKENS`` and are
# written into the base ``:root`` only. Two of them point back at a colour token (``--focus`` at ``--sky``,
# the float shadow at ``--shadow``), which is how they stay right in light without a second copy.
# ``--c1``..``--c6`` is the categorical chart ramp: a fixed order, never cycled — a seventh series is
# "other". The ramp was checked against both board surfaces for lightness band, chroma floor, colour-vision
# separation of adjacent pairs and 3:1 contrast; reordering it fails the deutan check, so the order is the
# contract, not a preference.
THEME_SCALE = {
    "s1": "4px", "s2": "8px", "s3": "12px", "s4": "16px",
    "s5": "24px", "s6": "32px", "s7": "48px", "s8": "64px",
    "r-sm": "6px", "r-md": "10px", "r-pill": "999px",
    "f-xs": "12px", "f-sm": "13px", "f-md": "14px", "f-lg": "17px", "f-xl": "24px",
    "w-reg": "400", "w-bold": "600",
    "lh-body": "1.55", "lh-head": "1.2",
    "e-flat": "0 0 0 1px var(--border)", "e-float": "0 10px 24px var(--shadow)",
    "focus": "var(--sky)", "motion": "150ms ease", "tap": "44px",
    "c1": "#0C87CC", "c2": "#C77C09", "c3": "#0A9E6E",
    "c4": "#8B5CF6", "c5": "#E8445F", "c6": "#2F9E8F",
}


def _theme_vars(scheme: str) -> str:
    return ";".join(f"--{k}:{v}" for k, v in THEME_TOKENS[scheme].items()) + f";color-scheme:{scheme}"


def _scale_vars() -> str:
    return ";".join(f"--{k}:{v}" for k, v in THEME_SCALE.items())


# Bare CSS (no <style>), so a page puts it inside its own style block.
THEME_CSS = (
    f":root{{{_theme_vars('dark')};{_scale_vars()}}}\n"
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
.help{background:var(--panel,#12161e);border:1px solid var(--border,#232a38);border-radius:10px;margin-bottom:14px;color:var(--muted,#8a93a6);font-size:12.5px}
.help summary{cursor:pointer;padding:9px 14px;font-weight:600;color:var(--accent,#0EA5E9)}
.help .body{padding:0 14px 12px;line-height:1.7}
.help b{color:var(--text,#d7dce5)}
.help code{color:var(--neon,#00e6a8)}
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


# ── The watch-list symbol and its dropdown (PR 4, D2; the dropdown is #774) ──
# One 44 px symbol next to a ticker opens a menu of named lists in two groups: Options (what the options build covers
# besides SPX) and Stocks (LEAPS, Earnings). Each list is a checkbox; "+ New list" names a new one and puts the
# ticker on it at once. A job reads the union of a group's lists (avo, 12 Nov 2026), so a board only has to show
# whether the ticker is on the group at all: that is what the symbol's filled state says.
# A board renders the symbol with ``watch_buttons_html`` (server-side) or ``rcWatchButtons`` (its JS) — the names
# from PR 4, so no board changed — and mounts ``watchlist_html(board)`` once. One delegated click handler reads
# ``data-rc-watch-*`` and ``data-ticker`` (never an interpolated value, ADR-X1), POSTs with the board as
# ``X-RC-Panel`` (the row's ``added_by``), and re-marks every symbol of that ticker on the page. The lists and their
# members are read live (``GET /api/watchlists``) when the page loads, not baked in when the board was rendered.
# A ticker the dashboard's rule (``app/dashboard.py`` ``_TICKER_RE``) refuses gets no symbol.
WATCH_TICKER_PATTERN = r"[A-Z0-9][A-Z0-9.\-]{0,9}"
#: The two groups as the menu heads them, in menu order.
WATCH_GROUP_LABELS = {"option": "Options", "stock": "Stocks"}
#: The bookmark: hollow while the ticker is on no list of either group, filled by the CSS once it is.
WATCH_ICON = ('<svg class="rc-watch-ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
              '<path d="M6 3.75h12a1 1 0 0 1 1 1V21l-7-4.2L5 21V4.75a1 1 0 0 1 1-1z"/></svg>')
_WATCH_TICKER_RE = re.compile(WATCH_TICKER_PATTERN)

WATCHLIST_CSS = """<style>
.rc-watch{display:inline-flex;align-items:center;vertical-align:middle;margin:0 6px 0 0;flex-wrap:nowrap}
.rc-watch button{min-width:var(--tap,44px);min-height:var(--tap,44px);padding:0 6px;box-sizing:border-box;
  display:inline-flex;align-items:center;justify-content:center;cursor:pointer;
  border-radius:var(--r-sm,6px);border:1px solid var(--input-border,#2F434C);background:var(--input-bg,#101F33);
  color:var(--muted,#9C9DA1);font:600 12px/1 system-ui,-apple-system,"Segoe UI",sans-serif}
.rc-watch button[aria-expanded="true"]{border-color:var(--sky,#0EA5E9)}
.rc-watch button[data-rc-watch-on="1"]{color:var(--sky,#0EA5E9);border-color:var(--sky,#0EA5E9)}
.rc-watch button:focus-visible{outline:2px solid var(--focus,var(--sky,#0EA5E9));outline-offset:2px}
.rc-watch button:disabled{opacity:.6;cursor:progress}
.rc-watch button[data-rc-watch-error]{border-color:var(--neg,#F87171)}
.rc-watch-ico{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.6}
.rc-watch button[data-rc-watch-on="1"] .rc-watch-ico{fill:currentColor}
.rc-watch-row{display:flex;margin:4px 0 2px}
.rc-watch-row .rc-watch{margin-left:0}
/* The menu: one element for the whole page, moved to the pressed symbol. */
.rc-watch-menu{position:absolute;z-index:70;min-width:15em;max-width:min(22em,calc(100vw - 2*var(--s4,16px)));
  max-height:min(70vh,26em);overflow:auto;box-sizing:border-box;padding:var(--s2,8px);
  background:var(--card,#0B1828);color:var(--fg,#C5C6C8);border:1px solid var(--border,rgba(197,198,200,.15));
  border-radius:var(--r-md,10px);box-shadow:var(--e-float,0 10px 24px rgba(0,0,0,.6));
  font:400 13px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.rc-watch-menu[hidden]{display:none}
.rc-watch-menu h4{margin:var(--s2,8px) 0 2px;padding:0 var(--s2,8px);font-size:11px;font-weight:600;
  letter-spacing:.06em;text-transform:uppercase;color:var(--muted,#9C9DA1)}
.rc-watch-menu h4:first-child{margin-top:0}
.rc-watch-tick{margin:0 0 var(--s2,8px);padding:0 var(--s2,8px);font-weight:600;color:var(--bright,#F0F0F0)}
.rc-watch-menu button{display:flex;align-items:center;gap:var(--s2,8px);width:100%;min-height:var(--tap,44px);
  padding:0 var(--s2,8px);box-sizing:border-box;text-align:left;cursor:pointer;background:none;border:0;
  border-radius:var(--r-sm,6px);color:inherit;font:inherit}
.rc-watch-menu button:hover{background:var(--hover-faint,rgba(255,255,255,.04))}
.rc-watch-menu button:focus-visible{outline:2px solid var(--focus,var(--sky,#0EA5E9));outline-offset:-2px}
.rc-watch-menu button[disabled]{opacity:.6;cursor:progress}
.rc-watch-menu .rc-box{flex:0 0 auto;width:18px;height:18px;border-radius:4px;
  border:1px solid var(--input-border,#2F434C);background:var(--input-bg,#101F33)}
.rc-watch-menu button[aria-checked="true"] .rc-box{background:var(--sky,#0EA5E9);border-color:var(--sky,#0EA5E9)}
.rc-watch-menu .rc-watch-count{margin-left:auto;color:var(--muted,#9C9DA1);font-size:11px}
.rc-watch-menu .rc-watch-add{color:var(--sky,#0EA5E9);font-weight:600}
.rc-watch-menu .rc-watch-err{margin:2px var(--s2,8px) 0;color:var(--neg,#F87171);font-size:11px}
.rc-watch-new{display:flex;gap:var(--s1,4px);align-items:center;padding:0 var(--s2,8px) var(--s1,4px)}
.rc-watch-new input{flex:1 1 auto;min-width:0;min-height:var(--tap,44px);box-sizing:border-box;padding:0 8px;
  border-radius:var(--r-sm,6px);border:1px solid var(--input-border,#2F434C);background:var(--input-bg,#101F33);
  color:inherit;font:inherit}
.rc-watch-new button{width:auto;flex:0 0 auto;padding:0 10px;border:1px solid var(--input-border,#2F434C)}
@media (max-width:480px){
  /* One phone-width sheet instead of a menu pinned to a symbol that may sit at the edge of the screen. */
  .rc-watch-menu{position:fixed;left:var(--s4,16px);right:var(--s4,16px);bottom:var(--s4,16px);top:auto;
    width:auto;max-width:none;max-height:72vh}
}
</style>"""

WATCHLIST_JS = ESC_JS + r"""
var RC_WATCH = {board: 'board', groups: {option: [], stock: []}, lists: {}, loaded: false, toggled: {},
                open: null, menu: null, adding: null, error: ''};
var RC_WATCH_RE = /^""" + WATCH_TICKER_PATTERN + r"""$/;
var RC_WATCH_GROUPS = """ + json.dumps(WATCH_GROUP_LABELS) + r""";
var RC_WATCH_KINDS = """ + json.dumps(list(WATCH_GROUP_LABELS)) + r""";
var RC_WATCH_ICON = """ + json.dumps(WATCH_ICON) + r""";
var RC_WATCH_NAME_MAX = """ + str(WATCH_NAME_MAX) + r""";
function rcWatchTicker(t){ t = String(t == null ? '' : t).trim().toUpperCase(); return RC_WATCH_RE.test(t) ? t : ''; }
// Is the ticker on any list of this group? That is what the symbol shows, and what a job reads (the union).
function rcWatchOn(kind, t){
  var lists = RC_WATCH.groups[kind] || [];
  for(var i = 0; i < lists.length; i++){
    var m = RC_WATCH.lists[lists[i].id];
    if(m && m[t]) return true;
  }
  return false;
}
function rcWatchAnyOn(t){
  for(var i = 0; i < RC_WATCH_KINDS.length; i++) if(rcWatchOn(RC_WATCH_KINDS[i], t)) return true;
  return false;
}
// On a card, the symbol gets its own line under the ticker (a ticker line that clips a long name must not clip it).
function rcWatchRow(ticker){ var b = rcWatchButtons(ticker); return b ? '<div class="rc-watch-row">' + b + '</div>' : ''; }
function rcWatchButtons(ticker){
  var t = rcWatchTicker(ticker);
  if(!t) return '';
  var on = rcWatchAnyOn(t);
  return '<span class="rc-watch"><button type="button" data-rc-watch-open="1" data-ticker="' + rcEsc(t) + '"' +
    (on ? ' data-rc-watch-on="1"' : '') + ' aria-haspopup="true" aria-expanded="false" aria-label="' +
    rcEsc(t + ' watch lists') + '" title="' + rcEsc(t + ' watch lists') + '">' + RC_WATCH_ICON + '</button></span>';
}
function rcWatchMark(btn){
  var t = rcWatchTicker(btn.getAttribute('data-ticker'));
  if(!t) return;
  if(rcWatchAnyOn(t)) btn.setAttribute('data-rc-watch-on', '1'); else btn.removeAttribute('data-rc-watch-on');
}
function rcWatchSync(root){
  var all = (root || document).querySelectorAll('[data-rc-watch-open]');
  for(var i = 0; i < all.length; i++) rcWatchMark(all[i]);
  if(RC_WATCH.open) rcWatchDraw();
}
function rcWatchLoad(d){
  var groups = (d && d.groups) || {};
  RC_WATCH_KINDS.forEach(function(kind){
    var lists = groups[kind] || [];
    RC_WATCH.groups[kind] = lists.map(function(l){ return {id: l.id, name: String(l.name == null ? '' : l.name)}; });
    lists.forEach(function(l){
      // A toggle that answered before this read is newer than it: keep the toggle's state for that pair.
      var mine = RC_WATCH.lists[l.id] || {}, next = {};
      (l.tickers || []).forEach(function(t){ next[String(t)] = true; });
      Object.keys(RC_WATCH.toggled).forEach(function(key){
        var at = key.indexOf(':'), t = key.slice(at + 1);
        if(key.slice(0, at) !== String(l.id)) return;
        if(mine[t]) next[t] = true; else delete next[t];
      });
      RC_WATCH.lists[l.id] = next;
    });
  });
  RC_WATCH.loaded = true;
}
function rcWatchInit(board){
  RC_WATCH.board = String(board || 'board');
  return fetch('/api/watchlists', {cache: 'no-store', credentials: 'same-origin'}).then(function(r){ return r.json(); })
    .then(function(d){
      if(!d || !d.ok) return;
      rcWatchLoad(d);
      rcWatchSync(document);
    }).catch(function(){});
}
// ── The menu ──
function rcWatchMenu(){
  if(!RC_WATCH.menu){
    var el = document.createElement('div');
    el.className = 'rc-watch-menu';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-label', 'Watch lists');
    el.hidden = true;
    document.body.appendChild(el);
    RC_WATCH.menu = el;
  }
  return RC_WATCH.menu;
}
function rcWatchMenuHtml(t){
  var out = '<p class="rc-watch-tick">' + rcEsc(t) + '</p>';
  RC_WATCH_KINDS.forEach(function(kind){
    out += '<h4>' + rcEsc(RC_WATCH_GROUPS[kind]) + '</h4>';
    (RC_WATCH.groups[kind] || []).forEach(function(l){
      var m = RC_WATCH.lists[l.id] || {}, on = !!m[t];
      out += '<button type="button" role="menuitemcheckbox" data-rc-watch-list="' + rcEsc(String(l.id)) +
        '" data-ticker="' + rcEsc(t) + '" aria-checked="' + (on ? 'true' : 'false') + '" title="' +
        rcEsc((on ? 'Remove ' : 'Add ') + t + (on ? ' from ' : ' to ') + l.name) +
        '"><span class="rc-box"></span><span class="rc-watch-name">' + rcEsc(l.name) +
        '</span><span class="rc-watch-count">' + rcEsc(String(Object.keys(m).length)) + '</span></button>';
    });
    if(RC_WATCH.adding === kind){
      out += '<div class="rc-watch-new"><input type="text" data-rc-watch-name="' + rcEsc(kind) +
        '" maxlength="' + RC_WATCH_NAME_MAX + '" placeholder="List name" aria-label="' +
        rcEsc('New ' + RC_WATCH_GROUPS[kind] + ' list') + '"><button type="button" data-rc-watch-create="' +
        rcEsc(kind) + '" data-ticker="' + rcEsc(t) + '">Add</button></div>';
    } else {
      out += '<button type="button" class="rc-watch-add" data-rc-watch-new="' + rcEsc(kind) +
        '" data-ticker="' + rcEsc(t) + '">+ New list</button>';
    }
  });
  if(RC_WATCH.error) out += '<p class="rc-watch-err">' + rcEsc(RC_WATCH.error) + '</p>';
  return out;
}
function rcWatchDraw(){
  var t = RC_WATCH.open;
  if(!t) return;
  var el = rcWatchMenu();
  el.innerHTML = rcWatchMenuHtml(t);
  el.hidden = false;
}
function rcWatchPlace(btn){
  var el = rcWatchMenu();
  if(!el.style) return;
  // Below 480 px the stylesheet pins the menu as a sheet across the bottom; an inline top/left would beat it,
  // so the placement is cleared first and skipped at that width. An unknown width counts as wide.
  el.style.top = '';
  el.style.left = '';
  var w = window.innerWidth || 0;
  if((w && w <= 480) || !btn.getBoundingClientRect) return;
  var r = btn.getBoundingClientRect();
  var top = r.bottom + (window.pageYOffset || 0) + 4;
  var left = Math.max(8, r.left + (window.pageXOffset || 0));
  el.style.top = top + 'px';
  el.style.left = left + 'px';
}
function rcWatchOpen(btn){
  var t = rcWatchTicker(btn.getAttribute('data-ticker'));
  if(!t) return;
  var same = RC_WATCH.open === t && !rcWatchMenu().hidden;
  rcWatchClose();
  if(same) return;
  RC_WATCH.open = t;
  RC_WATCH.adding = null;
  RC_WATCH.error = '';
  rcWatchDraw();
  rcWatchPlace(btn);
  btn.setAttribute('aria-expanded', 'true');
}
function rcWatchClose(){
  RC_WATCH.open = null;
  RC_WATCH.adding = null;
  RC_WATCH.error = '';
  if(RC_WATCH.menu){ RC_WATCH.menu.hidden = true; RC_WATCH.menu.innerHTML = ''; }
  var open = document.querySelectorAll('[data-rc-watch-open][aria-expanded="true"]');
  for(var i = 0; i < open.length; i++) open[i].setAttribute('aria-expanded', 'false');
}
// ── The writes ──
function rcWatchFail(what){
  RC_WATCH.error = 'Not changed: ' + String(what);
  rcWatchDraw();
}
function rcWatchToggle(btn){
  var id = String(btn.getAttribute('data-rc-watch-list') || '');
  var t = rcWatchTicker(btn.getAttribute('data-ticker'));
  if(!t || !/^[0-9]+$/.test(id)) return null;
  btn.disabled = true;
  return fetch('/api/watchlists/' + encodeURIComponent(id) + '/toggle/' + encodeURIComponent(t),
               {method: 'POST', headers: {'X-RC-Panel': RC_WATCH.board}, credentials: 'same-origin'})
    .then(function(r){ return r.json().then(function(d){ return {status: r.status, d: d || {}}; }); })
    .then(function(res){
      btn.disabled = false;
      if(!res.d.ok){ rcWatchFail(res.d.error || ('HTTP ' + res.status)); return; }
      RC_WATCH.error = '';
      RC_WATCH.toggled[id + ':' + t] = true;
      var m = RC_WATCH.lists[id] || (RC_WATCH.lists[id] = {});
      if(res.d.on) m[t] = true; else delete m[t];
      rcWatchSync(document);
    }).catch(function(e){ btn.disabled = false; rcWatchFail(e); });
}
function rcWatchCreate(btn){
  var kind = String(btn.getAttribute('data-rc-watch-create') || '');
  var t = rcWatchTicker(btn.getAttribute('data-ticker'));
  if(!RC_WATCH_GROUPS[kind]) return null;
  var box = RC_WATCH.menu && RC_WATCH.menu.querySelector('[data-rc-watch-name="' + kind + '"]');
  var name = box ? String(box.value || '').replace(/\s+/g, ' ').trim() : '';
  if(!name){ rcWatchFail('a list needs a name'); return null; }
  btn.disabled = true;
  return fetch('/api/watchlists', {method: 'POST', credentials: 'same-origin',
      headers: {'X-RC-Panel': RC_WATCH.board, 'Content-Type': 'application/json'},
      body: JSON.stringify({kind: kind, name: name})})
    .then(function(r){ return r.json().then(function(d){ return {status: r.status, d: d || {}}; }); })
    .then(function(res){
      btn.disabled = false;
      if(!res.d.ok || !res.d.list){ rcWatchFail(res.d.error || ('HTTP ' + res.status)); return; }
      var list = res.d.list;
      var known = (RC_WATCH.groups[kind] || []).some(function(l){ return String(l.id) === String(list.id); });
      if(!known) RC_WATCH.groups[kind] = (RC_WATCH.groups[kind] || []).concat([{id: list.id, name: list.name}]);
      if(!RC_WATCH.lists[list.id]) RC_WATCH.lists[list.id] = {};
      RC_WATCH.adding = null;
      RC_WATCH.error = '';
      rcWatchDraw();
      // The ticker the menu was opened on goes straight onto the new list: that is why the list was made.
      if(!t || RC_WATCH.lists[list.id][t]) { rcWatchSync(document); return; }
      var item = RC_WATCH.menu && RC_WATCH.menu.querySelector('[data-rc-watch-list="' + list.id + '"]');
      if(item) return rcWatchToggle(item);
      rcWatchSync(document);
    }).catch(function(e){ btn.disabled = false; rcWatchFail(e); });
}
// Capture phase: a row or card that opens on click must not also open when its ticker's symbol is pressed.
document.addEventListener('click', function(ev){
  var el = ev.target && ev.target.closest ? ev.target : null;
  if(!el) return;
  var open = el.closest('[data-rc-watch-open]');
  var item = el.closest('[data-rc-watch-list]');
  var fresh = el.closest('[data-rc-watch-new]');
  var make = el.closest('[data-rc-watch-create]');
  if(!open && !item && !fresh && !make){
    if(RC_WATCH.open && !el.closest('.rc-watch-menu')) rcWatchClose();
    return;
  }
  ev.preventDefault();
  ev.stopPropagation();
  if(open){ if(!open.disabled) rcWatchOpen(open); return; }
  if(item){ if(!item.disabled) rcWatchToggle(item); return; }
  if(fresh){ RC_WATCH.adding = fresh.getAttribute('data-rc-watch-new'); RC_WATCH.error = ''; rcWatchDraw();
             var box = RC_WATCH.menu && RC_WATCH.menu.querySelector('[data-rc-watch-name]');
             if(box && box.focus) box.focus();
             return; }
  if(make && !make.disabled) rcWatchCreate(make);
}, true);
document.addEventListener('keydown', function(ev){
  if(ev.key === 'Escape' && RC_WATCH.open){ rcWatchClose(); return; }
  if(ev.key !== 'Enter' || !RC_WATCH.adding) return;
  var el = ev.target;
  if(!el || !el.getAttribute || !el.getAttribute('data-rc-watch-name')) return;
  ev.preventDefault();
  var make = RC_WATCH.menu && RC_WATCH.menu.querySelector('[data-rc-watch-create]');
  if(make && !make.disabled) rcWatchCreate(make);
}, true);
"""


def watch_buttons_html(ticker, on=()) -> str:
    """The watch symbol for one ticker, rendered on the server. ``on`` names the groups the board already knows the
    ticker is on (LEAPS knows its stock list); ``rcWatchInit`` re-marks every symbol from the live lists anyway, and
    the menu itself is built in the browser from ``GET /api/watchlists``."""
    t = str(ticker if ticker is not None else "").strip().upper()
    if not _WATCH_TICKER_RE.fullmatch(t):
        return ""
    e = html.escape(t, quote=True)
    mark = ' data-rc-watch-on="1"' if any(k in on for k in WATCH_GROUP_LABELS) else ""
    return (f'<span class="rc-watch"><button type="button" data-rc-watch-open="1" data-ticker="{e}"{mark} '
            f'aria-haspopup="true" aria-expanded="false" aria-label="{e} watch lists" '
            f'title="{e} watch lists">{WATCH_ICON}</button></span>')


def watchlist_html(board: str) -> str:
    """Mount the symbol and its menu on a board once: the CSS, the snippet and ``rcWatchInit(board)``. ``board``
    becomes the ``X-RC-Panel`` value, so the list rows say which board added them."""
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
    "WATCHLIST_JS", "WATCHLIST_CSS", "WATCH_GROUP_LABELS", "WATCH_ICON", "watch_buttons_html",
    "watchlist_html",
    "JOB_JS",
    "HELP_CSS", "help_html",
    "THEME_TOKENS", "THEME_CSS", "THEME_JS", "THEME_STORAGE_KEY",
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
.rc-history{background:var(--panel,#12161e);border:1px solid var(--border,#232a38);border-radius:10px;margin:14px 0;color:var(--muted,#8a93a6);font-size:12.5px}
.rc-history summary{cursor:pointer;padding:9px 14px;font-weight:600;color:var(--accent,#0EA5E9)}
.rc-history .body{padding:0 14px 12px;line-height:1.6;overflow-x:auto}
.rc-history table{border-collapse:collapse;width:100%;font-size:12px}
.rc-history th,.rc-history td{text-align:left;padding:4px 8px;border-top:1px solid var(--border,#232a38);vertical-align:top}
.rc-history th{color:var(--text,#d7dce5);font-weight:600}
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
