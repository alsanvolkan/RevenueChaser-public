"""The Health tab's "Board freshness" part (#566): did each board's last run read the newest build of every source
DuckDB it reads?

The data is ``core/board_sources.freshness`` over ``core/history.last_runs``; this module only draws it, in the
``#needs`` frame and palette (``static/needs.css``). One card per board, the boards that need a look first. Under
600 px the cards are one column; every line wraps rather than scroll sideways.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape as esc
from zoneinfo import ZoneInfo

#: avo's clock (CLAUDE.md: Europe/Amsterdam).
LOCAL = ZoneInfo("Europe/Amsterdam")

#: state -> (chip text, chip class of needs.css, sort order)
STATES = {
    "behind": ("behind", "st-stale", 0),
    "error": ("error", "st-unreadable", 1),
    "missing": ("file missing", "st-unreadable", 2),
    "rolled_back": ("rolled back", "st-stale", 3),
    "old_store": ("old store", "st-missing", 4),
    "not_recorded": ("not recorded yet", "st-missing", 5),
    "current": ("current", "st-fresh", 6),
    "not_opened": ("not opened", "st-missing", 7),
}


def collect() -> dict:
    """``{"boards": [...]}`` read now, or ``{"error": ...}``: an unreadable file is a line on the page, never a
    blank tab."""
    try:
        from RevenueChaser.core import board_sources, history
        return {"boards": board_sources.freshness(history.last_runs())}
    except Exception as exc:  # noqa: BLE001 - the part says what failed; the rest of the tab still draws
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def _at(ts) -> str:
    """``9 Nov 14:05`` in Amsterdam from epoch seconds; ``—`` when empty."""
    if ts is None:
        return "—"
    try:
        d = datetime.fromtimestamp(float(ts), timezone.utc).astimezone(LOCAL)
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"
    return f"{d.day} {d:%b %H:%M}"


def _day(value) -> str:
    """``6 Nov 2026`` from ``2026-11-06``; the text as is when it is not a date."""
    if not value:
        return "—"
    try:
        d = datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        return str(value)
    return f"{d.day} {d:%b %Y}"


def _chip(state: str) -> str:
    text, cls, _ = STATES.get(state, (state, "st-missing", 9))
    return f'<span class="st {cls}">{esc(text)}</span>'


def _label(board: str, labels: dict[str, str]) -> str:
    if board in labels:
        return labels[board]
    name = board[len("module_"):] if board.startswith("module_") else board
    return name.replace("_", " ").title()


def _source_line(s: dict) -> str:
    file = f'<code>{esc(str(s.get("file") or s.get("kind") or "?"))}</code>'
    state = s.get("state") or ""
    newest = f' · newest day {esc(_day(s.get("newest_day")))}' if s.get("newest_day") else ""
    if s.get("error"):
        words = f'could not be recorded: {esc(str(s["error"]))}'
    elif s.get("reads") == "not_opened":
        words = (f'this run did not open it; reads of this file are set to the old store ({esc(str(s["old_store"]))})'
                 if s.get("old_store") else "the board says it reads this file, but this run did not open it")
    elif state == "missing":
        words = f'read build <b>{esc(_at(s.get("built_at")))}</b> · the file is gone now'
    else:
        parts = [f'read build <b>{esc(_at(s.get("built_at")))}</b>']
        if s.get("rebuilt_during_run"):
            parts.append(f'read {len(s.get("build_ids") or [])} builds: a new build landed during the run')
        elif state == "behind":
            parts.append(f'live build now <b>{esc(_at(s.get("live_built_at")))}</b>')
        elif state == "rolled_back":
            parts.append(f'live build now older: <b>{esc(_at(s.get("live_built_at")))}</b>')
        words = " · ".join(parts) + newest
    return f'<li class="bf-src">{file} {_chip(state)}<span class="bf-words">{words}</span></li>'


def _board_card(b: dict, labels: dict[str, str]) -> str:
    state = b.get("state") or ""
    run = f'last run {esc(_at(b.get("run_at")))}'
    if b.get("quality"):
        run += f' · {esc(str(b["quality"]))}'
    if state == "not_opened" and b.get("sources"):
        body = ('<p class="bf-note">This run opened no builder DuckDB file.</p><ul class="bf-srcs">'
                + "".join(_source_line(s) for s in b["sources"]) + "</ul>")
    elif state == "not_recorded":
        body = ('<p class="bf-note">This run was recorded before the board kept which builds it read. '
                'The next Fetch of the board records them.</p>')
    elif not b.get("sources"):
        body = '<p class="bf-note">This run opened no builder DuckDB file and the board declares none.</p>'
    else:
        body = '<ul class="bf-srcs">' + "".join(_source_line(s) for s in b["sources"]) + "</ul>"
    return (f'<article class="bf-board bf-{esc(state)}" data-board="{esc(str(b.get("board") or ""))}">'
            f'<div class="bf-head"><b>{esc(_label(str(b.get("board") or ""), labels))}</b>{_chip(state)}</div>'
            f'<div class="bf-run">{run}</div>{body}</article>')


def section(data: dict | None, labels: dict[str, str] | None = None) -> str:
    """The part's HTML. ``labels`` maps a board key to the name its tab shows."""
    labels = labels or {}
    data = data or {}
    head = ('<section class="part" id="board-freshness" aria-labelledby="board-freshness-h">'
            '<h2 id="board-freshness-h">Board freshness{secs}</h2>'
            '<p class="sub">Did each board\'s last run read the newest build of the DuckDB files it reads? '
            'Only files the run really opened count. <b>Behind</b>: a newer build is live now than the one the run '
            'read, so a Fetch of the board would read newer data. Times are Amsterdam.</p>')
    if data.get("error"):
        return head.format(secs="") + f'<p class="warn">Could not be read: {esc(str(data["error"]))}</p></section>'
    boards = data.get("boards") or []
    if not boards:
        return head.format(secs="") + '<p class="empty">No board run is recorded yet.</p></section>'
    counts: dict[str, int] = {}
    for b in boards:
        counts[b.get("state")] = counts.get(b.get("state"), 0) + 1
    secs = " · ".join(f"{n} {STATES.get(s, (s,))[0]}" for s, n in sorted(
        counts.items(), key=lambda kv: STATES.get(kv[0], ("", "", 9))[2]))
    order = sorted(boards, key=lambda b: (STATES.get(b.get("state"), ("", "", 9))[2],
                                          _label(str(b.get("board") or ""), labels)))
    return (head.format(secs=f' <span class="secs">{esc(secs)}</span>')
            + '<div class="bf-list">' + "".join(_board_card(b, labels) for b in order) + "</div></section>")
