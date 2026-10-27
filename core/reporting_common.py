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


def _theme_vars(scheme: str) -> str:
    return ";".join(f"--{k}:{v}" for k, v in THEME_TOKENS[scheme].items()) + f";color-scheme:{scheme}"


# Bare CSS (no <style>), so a page puts it inside its own style block.
THEME_CSS = (
    f":root{{{_theme_vars('dark')}}}\n"
    f"@media (prefers-color-scheme: light){{:root[data-theme=\"auto\"]{{{_theme_vars('light')}}}}}\n"
    f":root[data-theme=\"light\"]{{{_theme_vars('light')}}}\n"
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
    "JOB_JS",
    "HELP_CSS", "help_html",
    "THEME_TOKENS", "THEME_CSS", "THEME_JS", "THEME_STORAGE_KEY",
    "universe_provenance_html",
    "provider_health_html",
    "notice_html",
    "write_artifact", "write_board", "payload_path", "PAYLOAD_JS",
    "recorded_sessions_html",
    "open_issues_payload",
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


def open_issues_payload(board_keys) -> dict:
    """The shell's "Open issues" data for these boards, ready for one embedded JSON block (#383, #21).

    Parent chrome, so the payload is built when the page renders and shipped as JSON; the shell re-renders it in
    ``showTab`` with no page load (``templates/dashboard.html``). No HTML here — the shell owns the markup. The
    registry is loaded **once** per call and every entry is shipped **once**, under ``entries``; each board names the
    entries it shows by key (``core.open_issues.panel_payload``), because the shared entries would otherwise be
    repeated on all 15 data boards.

    Raises ``open_issues.RegistryError`` when the registry is missing or malformed; the caller renders that as a
    message inside the group, never as a failed page (the ``recorded_sessions_html`` convention).
    """
    from RevenueChaser.core import open_issues

    return open_issues.panel_payload(board_keys)


def universe_provenance_html(generated_at, refreshed_by, partial: bool = False) -> str:
    """Shared-universe provenance line for a report header.

    Shows which board last refreshed the shared universe cache and when, so every
    board that reuses that data can display the same attribution. ``partial`` adds
    "partial fetch": the last Yahoo pull did not complete, so the universe was
    upserted and names it missed were kept (#146). Empty when no provenance is
    recorded (legacy payload / first run).
    """
    flag = (' · <b style="color:var(--amber,#f1c40f)">partial fetch</b>' if partial else "")
    if not refreshed_by:
        return ('<div style="color:var(--muted);font-size:11px;margin-top:3px">Universe' + flag + '</div>'
                if partial else "")
    ts = ""
    try:
        dt = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        ts = dt.strftime("%b %d, %Y %H:%M") + " UTC"
    except (ValueError, TypeError):
        ts = str(generated_at or "")
    return (
        '<div style="color:var(--muted);font-size:11px;margin-top:3px">'
        'Universe refreshed by <b style="color:var(--text)">' + html.escape(refreshed_by) + '</b>'
        + ((" · " + html.escape(ts)) if ts else "") + flag + '</div>'
    )


# Banner names of the providers whose health a board shows (K3); any other ``provider`` is shown as given.
PROVIDER_NAMES = {"unusual_whales": "Unusual Whales", "yfinance": "Yahoo Finance"}


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
