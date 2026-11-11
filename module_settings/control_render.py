"""The Settings / Data control board's HTML (design v28), from ``control.build``'s model.

Every string goes through ``html_safe.esc``; the page's own data (the flow diagram, the calendars, the universe)
rides in one ``script_json`` block the script reads. The board is the Data tab's panel (``render_data``;
``render_fetch`` is an empty alias); the styles are ``static/control.css`` and ``static/data_tree.css``, scoped to
``.ctl``, and the behaviour ``static/control.js`` and ``static/data_tree.js``. Nothing a press does here fetches by itself: Fetch and Pause put one request in
the builder's queue, exactly as before; Apply saves the universe; Fill and "Fill this range" stay off unless the
server's ``RC_FILL_ENABLED`` is set (avo's word); "Refresh the example rows" reads five rows of the live file; and
"Take the disk picture" (in Fetch, where presses live) queues the storage-picture job, which walks the HDD trees
off the request thread (#434).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from RevenueChaser.core import board_settings
from RevenueChaser.core.html_safe import esc, script_json
from RevenueChaser.module_settings import control
from RevenueChaser.rcdata import backup
from RevenueChaser.rcdata.needs import screen

_STATIC = Path(__file__).resolve().parent / "static"
TREE_CSS = (_STATIC / "data_tree.css").read_text(encoding="utf-8")
TREE_JS = (_STATIC / "data_tree.js").read_text(encoding="utf-8")
CTL_CSS = (_STATIC / "control.css").read_text(encoding="utf-8") + "\n" + TREE_CSS
CTL_JS = (_STATIC / "control.js").read_text(encoding="utf-8")

#: The routes a press uses; the script reads them from the page's data, so a test sees every route in one place.
ROUTES = {"request": "/api/build/request/", "schedule": "/api/build/schedule/", "queue": "/api/build/queue",
          "automation_ds": "/api/build/automation-ds/",
          "switch": "/api/build/switch/", "timer": "/api/build/timer",
          "settings": "/api/settings/board/module_settings", "sample": "/api/settings/sample/",
          "job": "/api/job/", "picture": "/run/storage-picture"}
#: After this long the page says it is stale and offers a refresh (the design: "refreshes after 60 minutes").
STALE_MINUTES = 60
#: Past this the disk picture's line says how old it is. The walk left the render in #434, so a three-week-old
#: picture would otherwise read exactly like this morning's.
PICTURE_STALE_HOURS = 25
#: The universe bar's offered sets, darkest last (the design's greens).
SET_COLORS = ("#1f863d", "#18652e", "#10441f")


def _cal_key(dataset: str) -> str:
    return dataset.replace("/", "-")


def _size(nbytes) -> str:
    if nbytes is None:
        return "—"
    n = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return "—"


def _icon(path: str, color: str, title: str, cls: str = "sic sm") -> str:
    return (f'<span class="{cls}" title="{esc(title)}" style="background:{esc(color)}">'
            f'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="{esc(path)}"/></svg></span>')


def _meter(kind: str, cls: str, label: str, share, text: str, target=None) -> str:
    if share is None:
        return (f'<div class="meter {kind} {esc(cls)}"><span class="ml">{esc(label)}</span><span class="track"></span>'
                f'<span class="mv">{esc(text)}</span></div>')
    tick = (f'<span class="tick" style="left:{target * 100:.0f}%" title="target {target:.0%}"></span>'
            if target is not None else ('<span class="tick" style="left:100%"></span>' if kind == "age" else ""))
    return (f'<div class="meter {kind} {esc(cls)}"><span class="ml">{esc(label)}</span><span class="track">'
            f'<span class="fill" style="width:{max(0.0, min(1.0, share)) * 100:.1f}%"></span>{tick}</span>'
            f'<span class="mv">{esc(text)}</span></div>')


# ── Header and Health ─────────────────────────────────────────────────────────────────────────────────────────

WIAL = ("<p>This page is the control board for every dataset the system holds. It answers four questions for each "
        "one: <b>where the data comes from</b> (which provider), <b>what the builder does with it</b>, <b>which DuckDB "
        "file and tables it is written to</b>, and <b>which boards read it</b>.</p>"
        "<p>The numbers are read on every render: health, row counts and run history come from <code>ops.sqlite</code>; "
        "row counts, sizes and example rows come from the serving files themselves, read once an hour. A value that "
        "could not be read is written as unknown, never shown as zero.</p>"
        "<p><b>What a press does.</b> Fetch puts one request in the builder's queue (its ▾ asks for raw only, or "
        "raw and Parquet); Pause cancels a queued request, stops a running fetch before its next provider call and "
        "holds the dataset until Resume. Apply saves the universe. Fill and "
        "&ldquo;Fill this range&rdquo; stay off until your word. Refresh the example rows reads five rows of the live "
        "file and calls no provider.</p>")


def _header(view: dict, title: str = "Database") -> str:
    return (f'<h1>Data/Health · {esc(title)}</h1>'
            f'<p class="sub"><b>Read {esc(view["read_at"])}</b> (Amsterdam) · source: <code>ops.sqlite</code> + the '
            f'serving files · after {STALE_MINUTES} minutes a strip on top says the numbers are old</p>'
            f'<div class="stalebar"{' id="ctl-stale"' if title == "Database" else ""} hidden><b>Stale</b> · this page was read at {esc(view["read_at"])} · '
            '<button type="button" class="lnk" data-reload>Refresh</button></div>')


def _switch(name: str, label: str, st: dict | None) -> str:
    st = st or {}
    on = st.get("on")
    if on is None:
        return (f'<span class="swt none" data-name="{name}"><b>{label}: not known</b>'
                '<span class="dim">the ops store could not be read</span></span>')
    who = "" if on else f'<span class="dim">{esc(st.get("by") or "?")} {esc(st.get("at") or "")}</span>'
    return (f'<button type="button" class="swt {"ok" if on else "bad"}" data-job="switch" data-name="{name}" '
            f'data-on="{1 if on else 0}" aria-pressed="{"true" if on else "false"}" '
            f'title="{label} switch: press to turn it {"off" if on else "on"}"><b>{label}: {"on" if on else "off"}'
            f'</b>{who}</button>')


def _topbar(view: dict, msg_id: str = "sw-msg") -> str:
    """#527: always at the top of both tabs: the fetch switch, the backup switch and the IBKR gateway's state with
    its login link (``RC_IBKR_LOGIN_URL``; no link when it is unset). ``msg_id`` keeps each copy's status line its
    own id."""
    sw = view.get("switches") or {}
    g = view.get("gateway") or {}
    # the old IBKR login card's link, its id kept on the Data tab's copy (one id a page)
    lid = ' id="ibkr-login-link"' if msg_id == "sw-msg" else ""
    login = (f'<a class="lnk"{lid} href="{esc(g["login"])}" target="_blank" rel="noopener noreferrer" '
             'title="Opens the IBKR gateway login screen. The address comes from RC_IBKR_LOGIN_URL; this page '
             'makes no call to it">log in</a>' if g.get("login") else "")
    gate = (f'<span class="swt gw {esc(g.get("cls") or "none")}"><b>IBKR gateway: {esc(g.get("n") or "unknown")}</b>'
            f'<span class="dim">{esc((g.get("label") or "").replace("IBKR gateway · ", ""))}</span>{login}</span>')
    return (f'<div class="topbar" role="group" aria-label="Switches and the IBKR gateway">'
            f'{_switch("fetch", "Fetch", sw.get("fetch"))}{_switch("backup", "Backup", sw.get("backup"))}'
            f'{_timer_box(view)}{gate}'
            f'<span role="status" class="dim" id="{esc(msg_id)}"></span></div>')


def _timer_box(view: dict) -> str:
    """#527 step 11: the hourly builder timer's state; its on / off press is the Grafana ops panel's timer button
    (``life-ctl life-rc-builder.timer on|off``), linked from ``RC_BUILDER_TIMER_URL``. The dashboard itself runs with
    NoNewPrivileges, so it never switches the timer."""
    st = (view.get("timer") or {}).get("state")
    cls = {"on": "ok", "off": "none"}.get(st, "warn")
    word = st if st in ("on", "off") else "not known"
    why = {"on": "the hourly pass runs on its own", "off": "nothing runs on its own; a Fetch still queues"}.get(
        st, "its state could not be read")
    url = view.get("timer_url")
    link = (f'<a class="lnk" href="{esc(url)}" target="_blank" rel="noopener noreferrer">on / off in Grafana ↗</a>'
            if url else "")
    # the press (avo chose it, 6 Nov 2026): POST /api/build/timer runs life-ctl life-rc-builder.timer on|off;
    # no press while the state is not known, so it never guesses which way to switch
    press = (f'<button type="button" class="btn sm" data-job="timer" data-on="{1 if st == "on" else 0}">'
             f'{"Turn off" if st == "on" else "Turn on"}</button>' if st in ("on", "off") else "")
    return (f'<span class="swt tmr {cls}"><b>Builder timer: {esc(word)}</b><span class="dim">{esc(why)}</span>'
            f'{press}{link}</span>')


#: The Health groups (#492 C1): three side by side, then Data at full width.
HEALTH_GROUPS = (("sources", "Sources"), ("scheduled", "Scheduled run"), ("build", "Build"))
FILE_TOTAL = 6


def _tile(t: dict) -> str:
    title = f'{t["n"]} · {t["label"]}'
    inner = f'<span class="n">{esc(t["n"])}</span><span class="l">{esc(t["label"])}</span>'
    if t.get("filter"):
        return (f'<button type="button" class="tile {esc(t["cls"])}" data-f="{esc(t["filter"])}" '
                f'title="{esc(title)}">{inner}</button>')
    return f'<div class="tile {esc(t["cls"])} static" title="{esc(title)}">{inner}</div>'


def _segbar(counts) -> str:
    """A file's fresh / late / stale / never bar, with its counts."""
    c = counts if isinstance(counts, dict) else {}
    total = c.get("total")
    if not total:
        return '<span class="segbar none" title="no dataset counted"></span><span class="segn dim">no dataset</span>'
    segs = "".join(f'<i class="seg {k}" style="flex-grow:{int(c.get(k) or 0)}" title="{int(c.get(k) or 0)} {w}"></i>'
                   for k, w in (("fresh", "fresh"), ("late", "late"), ("stale", "stale"), ("never", "never ran"))
                   if c.get(k))
    words = " · ".join(f'{int(c.get(k) or 0)} {w}' for k, w in (("fresh", "fresh"), ("late", "late"),
                                                               ("stale", "stale"), ("never", "never ran")))
    return f'<span class="segbar">{segs}</span><span class="segn">{esc(words)}</span>'


def _byfile(view: dict) -> str:
    rows = (view.get("health") or {}).get("files")
    if rows is None:
        rows = [{"name": f["name"], "state": f.get("state")} for f in view.get("files") or []]
    if not rows:
        return ""
    on_disk = sum(1 for r in rows if isinstance(r.get("state"), dict) and r["state"].get("exists"))
    known = any(isinstance(r.get("state"), dict) and r["state"].get("exists") is not None for r in rows)
    head = f"{on_disk} of {FILE_TOTAL} files on the disk" if known else "files on the disk: not known"
    lines = []
    for r in rows:
        st = r.get("state") if isinstance(r.get("state"), dict) else {}
        words = _fstate_words(st or None)
        lines.append(f'<button type="button" class="bfl" data-pick="{esc(r.get("name"))}"><span class="bfn">'
                     f'{esc(r.get("name"))}</span>{_segbar(st.get("counts"))}<span class="bfs">'
                     f'{"🦆 " if st.get("exists") else ""}{esc(words)}</span></button>')
    return f'<div class="byfile"><h4 class="hgh">{esc(head)}</h4>{"".join(lines)}</div>'


def _health(view: dict) -> str:
    h = view["health"]
    by_group: dict[str, list[str]] = {}
    for t in h["tiles"]:
        g = t.get("group") if t.get("group") in ("sources", "scheduled", "build") else "data"
        by_group.setdefault(g, []).append(_tile(t))
    total = view.get("datasets_total")
    if total is None:
        total = sum(len(f["datasets"]) for f in view.get("files") or [])
    top = "".join(f'<div class="hgrp" data-group="{k}"><h3 class="hgh">{w}</h3><div class="strip">'
                  f'{"".join(by_group.get(k, [])) or """<span class="dim">no tile yet</span>"""}</div></div>'
                  for k, w in HEALTH_GROUPS)
    data = (f'<div class="hgrp hdata" data-group="data"><h3 class="hgh">Data · {total} datasets</h3>'
            f'<div class="strip">{"".join(by_group.get("data", []))}</div>{_byfile(view)}</div>')
    c = h["counts"]
    summ = (f'<b class="s-ok">{c["fresh"]} fresh</b> · <b class="s-warn">{c["late"]} late</b> · '
            f'<b class="s-bad">{c["stale"]} stale</b> · {c["never"]} never ran · {h["problems"]} open problems')
    al = h.get("alerts")
    if al is None:                      # a view built before #527: the tiles as they were
        return ('<details class="sec" id="sec-health" open><summary><h2>Health right now</h2>'
                f'<span class="secs">{summ}</span></summary><div class="hgrps">{top}</div>{data}</details>')
    cards = ("".join(f'<div class="tile {esc(a["cls"])} static alert" data-key="{esc(a["key"])}">'
                     f'<span class="n">{esc(a["n"])}</span><span class="l">{esc(a["label"])}</span></div>' for a in al)
             if al else '<p class="calm s-ok"><b>Nothing needs you right now.</b></p>')
    return ('<details class="sec" id="sec-health" open><summary><h2>Health right now</h2>'
            f'<span class="secs">{len(al)} to look at</span></summary>'
            f'<div class="alerts">{cards}</div><p class="allgood"><b>All good:</b> {esc(h.get("allgood") or "")}</p>'
            f'<details class="htiles"><summary>Every tile</summary><div class="hgrps">{top}</div>{data}</details>'
            '</details>')


# ── Fetch: the universe ───────────────────────────────────────────────────────────────────────────────────────

def _universe(view: dict) -> str:
    u = view["universe"]
    on = [s for s in u["sets"] if s["on"]]
    summary = " + ".join(s["label"] for s in on) + f" · {control.count(u['size'])} names"
    segs = []
    shades = dict(zip(("A", "E", "R"), SET_COLORS))
    for s in u["sets"]:
        n = s["n"] or 0
        label = f'{s["label"]}, {control.count(s["n"])} names: {s["words"]}'
        if s["offered"]:
            segs.append(
                f'<button type="button" class="useg" data-tier="{esc(s["key"])}" data-label="{esc(s["label"])}" '
                f'data-n="{int(n)}" aria-pressed="{"true" if s["on"] else "false"}" data-on="{1 if s["on"] else 0}" '
                f'aria-label="{esc(label)}" data-tip="{esc(s["label"] + " · " + s["words"])}" '
                f'style="--c:{shades.get(s["key"], SET_COLORS[0])};flex-grow:{max(int(n), 1)}"><b>{esc(s["label"])}</b> '
                f'<span class="un">{control.count(s["n"])}</span> <span class="uw">{esc(s["words"])}</span></button>')
        else:
            segs.append(
                f'<span class="useg uoff" role="img" data-tier="{esc(s["key"])}" data-n="{int(n)}" tabindex="0" '
                f'aria-label="{esc(label)}" data-tip="{esc(s["label"] + " · " + s["words"])}" '
                f'style="flex-grow:{max(int(n), 1)}"><b>{esc(s["label"])}</b> <span class="un">'
                f'{control.count(s["n"])}</span> <span class="uoffl">not offered</span> <span class="uw">{esc(s["words"])}'
                '</span></span>')
    inc = "".join(
        f'<button type="button" class="chipb uinc" data-inc="{esc(g["key"])}" aria-pressed="'
        f'{"true" if g["on"] else "false"}" data-on="{1 if g["on"] else 0}">{esc(g["words"])}</button>'
        for g in u["include"])
    spine = next((d for f in view["files"] for d in f["datasets"] if d["dataset"] == "spine/security"), None)
    spine_words = (f'spine/security · {spine["cadence"]} · last check {control.day_words(spine["checked_on"])}'
                   if spine else "spine/security")
    filled = u.get("filled")
    live = (f'<b>{control.count(filled)} names have a bar on the newest session.</b> ' if filled is not None
            else "<b>No session in prices yet.</b> ")
    return (
        '<div class="uni" id="universe"><div class="uhd"><span class="ulab">Universe · which names the next fetches '
        f'and fills use</span><span class="uval">runs on <b id="uni-sum">{esc(summary)}</b></span>'
        '<span class="uact"><button type="button" class="btn go" data-job="fetch" data-dataset="spine/security">'
        f'Fetch the universe</button><span class="dim">{esc(spine_words)}</span></span></div>'
        f'<div class="ubar" role="group" aria-label="Tier sets in the universe">{"".join(segs)}</div>'
        f'<p class="ulive">{live}C and D are shown for the whole picture, not offered. All tiers: {control.count(u.get("total"))} tickers on the spine'
        + (f' of {esc(control.day_words(u["as_of"]))}' if u.get("as_of") else "") + '.</p>'
        f'<div class="uinch"><span class="dim">A Fetch of only some tier sets also brings each group whose chip is on '
        f'(a Fetch of the whole universe brings all three anyway):</span>{inc}</div>'
        '<div class="udraft" id="uni-draft" hidden></div>'
        '<div class="uhand"><label for="uni-names">Names by hand</label>'
        '<input id="uni-names" type="text" placeholder="e.g. TSM, ASML · comma separated" autocomplete="off">'
        '<button type="button" class="btn" id="uni-add">Add</button><span class="uhchips" id="uni-hchips"></span>'
        '<span class="dim" id="uni-handn">no names added</span></div>'
        '<p class="dim uhnote">Names typed by hand stay in this browser until Apply sends them with the next press; '
        'they are not saved on the server.</p>'
        f'{_uni_cards(view)}{_funnel(view)}</div>')


def _legend(view: dict) -> str:
    fill = ("the Fill buttons are on (RC_FILL_ENABLED is set on this server)" if view["fill_enabled"]
            else view["fill_off_why"])
    # #527 step 8b: one line visible, the rest behind a "?"
    return ('<div class="flegend"><span class="lg2 s-notpicked">a tier set: in the universe or not</span>'
            '<span class="lg2 s-nodata">no tier choice · no button</span>'
            '<details class="lgq"><summary class="q" aria-label="What a Fill cell shows">?</summary>'
            f'<span class="dim">A Fill cell shows its set and whether the universe holds it. Where a dataset has days to pick (alpaca/bars), a press opens '
            f'its Backfill calendar with that set ticked, and the calendar says names, calls and nights before you '
            f'start. Fill: {esc(fill)}.</span>'
            '<div class="lgrow"><b>After a press</b> the button shows its request:<span class="lg3 st-q">queued</span>'
            '<span class="lg3 st-r">running</span><span class="lg3 st-d">done</span>'
            '<span class="lg3 st-f">failed</span><span class="lg3 st-p">paused</span></div></details></div>')


# ── Fetch: one row a dataset ──────────────────────────────────────────────────────────────────────────────────

def _fill_row(d: dict, view: dict) -> str:
    fill = d["fill"]
    if not fill["cells"]:
        return f'<p class="fillno">No tier choice: {esc(fill["why"])}</p>'
    key = _cal_key(d["dataset"])
    cells = []
    for c in fill["cells"]:
        # The cell says whether its set is in the universe; how full the set is lives in the calendar, so no cell
        # is painted "filled" on a guess.
        state = "s-notpicked"
        words = "in the universe" if c["picked"] else "not in the universe"
        if fill.get("calendar"):
            cells.append(
                f'<button type="button" class="fc {state} fopen" data-bf="{esc(key)}" data-g="{esc(c["key"])}" '
                f'aria-label="{esc(c["label"])}: {words}. Press to open the Backfill calendar with this set ticked">'
                f'<span class="fch">{esc(c["label"])}</span><span class="fst">{words}</span>'
                f'<span class="fpre">{control.count(c["n"])} names</span>'
                '<span class="fpk" aria-hidden="true">Pick days ▸</span></button>')
        else:
            dis = "" if view["fill_enabled"] else " disabled"
            cells.append(
                f'<button type="button" class="fc {state}" data-job="fill" data-dataset="{esc(d["dataset"])}" '
                f'data-g="{esc(c["key"])}"{dis} aria-label="{esc(c["label"])}: {words}. Fill this set">'
                f'<span class="fch">{esc(c["label"])}</span><span class="fst">{words}</span>'
                f'<span class="fpre">{control.count(c["n"])} names</span></button>')
    off = "" if view["fill_enabled"] else f' · <b>off:</b> {esc(view["fill_off_why"])}'
    head = ("a press opens the Backfill calendar with that set ticked: pick the days there" if fill.get("calendar")
            else "a press fills that set of names")
    return (f'<div class="fillrow"><div class="fillh"><b>Fill by tier set</b> <span class="dim">{head}{off}</span>'
            f'</div><div class="fcells">{"".join(cells)}</div></div>')


def _problems(d: dict) -> str:
    if not d["issues_open"]:
        return ""
    items = "".join(
        f'<li><code>{esc(i.get("kind"))}</code><span class="pw">{esc(i.get("detail") or "")}</span>'
        f'<span class="pk">{esc(i.get("key") or "")} · since {esc(control.day_words(i.get("found_on")))}</span></li>'
        for i in d["issues"])
    more = d["issues_open"] - len(d["issues"])
    if more > 0:
        items += f'<li class="dim">… {more:,} more in <code>build_issues</code></li>'
    return (f'<details class="probs"><summary>Open problems <b>{d["issues_open"]:,}</b> <span class="dim">one per row '
            f'· <code>build_issues</code></span></summary><ol>{items}</ol></details>')


def _acts(d: dict, pause: bool = True) -> str:
    parts, cost = [], []
    if d["calendar"]:
        key = _cal_key(d["dataset"])
        parts.append(f'<button type="button" class="btn bfbtn" data-bf="{esc(key)}" aria-expanded="false" '
                     f'aria-controls="bf-{esc(key)}">Backfill… <span class="bfc">calendar</span></button>')
        cost.append('<b>Backfill</b> the calendar picks the days and the sets; "Fill this range" stays off until '
                    'your word')
    if pause and not d["attended"] and d["fetch"]["self"]:
        paused = d["paused"]
        parts.append(f'<button type="button" class="btn" data-job="pause" data-dataset="{esc(d["dataset"])}" '
                     f'aria-pressed="{"true" if paused else "false"}">{"Resume" if paused else "Pause"}</button>')
        cost.append("<b>Pause</b> 0 calls: the builder skips it until Resume")
    if d["cross_check"]:
        parts.append('<button type="button" class="btn" data-goto="crosscheck">Cross-check</button>')
        cost.append("<b>Cross-check</b> 0 calls: it shows the report that exists")
    if not parts:
        return ""
    cal = (f'<div class="bfcal" id="bf-{esc(_cal_key(d["dataset"]))}" data-bf="{esc(_cal_key(d["dataset"]))}" hidden>'
           '</div>' if d["calendar"] else "")
    return f'<div class="acts">{"".join(parts)}<p class="cost">{" · ".join(cost)}</p></div>{cal}'


# ── Stage 1: Sources, one panel a file (#492 C1) ─────────────────────────────────────────────────────────────

#: The one button's legend line (#492 PR (ii): a Pause stops the dataset for real).
BUTTON_LEGEND = ('One button for each dataset: <b>Fetch</b> asks for a whole run (raw, Parquet and the served DuckDB '
                 'file); its <b>▾</b> asks for <b>Fetch raw</b> (the provider\'s answer into HDD raw only) or '
                 '<b>Fetch raw + Parquet</b> (the served file stays as it is), and the next hourly pass builds the '
                 'rest when this file\'s automation is raw + Parquet + DuckDB · <b>Pause</b> cancels a queued run, '
                 'stops a running one before its next provider call and holds the dataset · <b>Resume</b> lets the '
                 'hourly pass run it again')
#: The line the button legend shows; the whole of ``BUTTON_LEGEND`` is behind its "?" (#527 step 8b).
BUTTON_LINE = "One button for each dataset: <b>Fetch</b> · <b>Pause</b> · <b>Resume</b>"


def _button_legend() -> str:
    return (f'<div class="blegend">{BUTTON_LINE}<details class="lgq"><summary class="q" '
            f'aria-label="What the buttons do">?</summary><p class="dim">{BUTTON_LEGEND}</p></details></div>')


#: The two partial runs of a Fetch press's ▾ menu (``app/dashboard.py`` ``REQUEST_STAGES``).
FETCH_STAGES = (("raw", "Fetch raw", "the provider's answer into HDD raw only; Parquet and DuckDB stay; the next "
                 "hourly pass builds them when this file's automation is raw + Parquet + DuckDB"),
                ("parquet", "Fetch raw + Parquet", "also stages Parquet; the served DuckDB file stays as it is "
                 "until the next pass, when this file's automation is raw + Parquet + DuckDB"))
#: The automation a served file's hourly pass runs (``ops.KIND_MODES``), as its select shows them.
AUTOMATION_MODES = (("off", "off · Fetch only"), ("raw", "raw"), ("parquet", "raw + Parquet"),
                    ("full", "raw + Parquet + DuckDB"))
PROVIDERS_SOURCE = ('day caps and request windows: <code>rcdata/caps.py</code> (<code>DAY_CAPS</code>, '
                    '<code>WINDOWS</code>) · what each provider brings, our pace and its rule: '
                    '<code>rcdata/needs/screen.py</code> (<code>PROVIDERS</code>) · used today: '
                    '<code>ops.provider_calls_daily</code>')


def _pcolor(view: dict, pid: str) -> str:
    """A provider's colour, the same one its row in Providers and daily limits wears."""
    for i, p in enumerate(view.get("providers") or []):
        if p.get("id") == pid:
            return control.SOURCE_COLORS[i % len(control.SOURCE_COLORS)]
    return control.SOURCE_COLORS[0]


def _written(at) -> str:
    if not at:
        return "not known"
    d = control.day_words(at)
    t = control._clock(at)
    return d if t == "—" else f"{d} {t}"


def _fstate_words(st) -> str:
    """"on the disk · 9.8 MB · written 5 Nov 19:43 · 9 of 9 tables hold rows · empty: a, b", or "no file yet"."""
    if not isinstance(st, dict) or st.get("exists") is None:
        return "not known"
    if not st.get("exists"):
        return "no file yet"
    size = _size(st.get("bytes")) if st.get("bytes") is not None else "size not known"
    tables = (f'{st["tables_with_rows"]} of {st["tables"]} tables hold rows'
              if st.get("tables") is not None and st.get("tables_with_rows") is not None
              else "tables not counted yet")
    empty = st.get("empty") or []
    return (f'on the disk · {size} · written {_written(st.get("written_at"))} · {tables}'
            + (f' · {st["uncounted"]} not counted' if st.get("uncounted") else "")
            + (f' · empty: {", ".join(str(e) for e in empty)}' if empty else ""))


def _active(view: dict, name: str):
    """The queue row holding ``name`` (queued or running), else None."""
    for r in (view.get("queue") or {}).get("active") or []:
        if r.get("dataset") == name and r.get("status") in ("queued", "running"):
            return r
    return None


def _one_button(d: dict, view: dict) -> str:
    """Fetch, Pause or Resume: one button a dataset, in the state the dataset is in."""
    name = d["dataset"]
    if d["attended"]:
        return (f'<button type="button" class="btn" data-job="copy" data-cmd="{esc(d["command"] or "")}">'
                'Copy the command</button><button type="button" class="btn" disabled>Fetch</button>')
    if not d["fetch"]["self"]:
        return ('<span class="dbnote">fetched by <button type="button" class="lnk" data-goto="universe">Fetch the '
                'universe</button></span>')
    act = _active(view, name)
    if d["paused"]:
        state, word, pill = "resume", "Resume", ""
    elif act is not None:
        state, word = "pause", "Pause"
        st = act.get("status")
        pill = f'<span class="lg3 {"st-r" if st == "running" else "st-q"}">{esc(st)}</span>'
    else:
        state, word, pill = "fetch", "Fetch", ""
    cls = "btn go" if state == "fetch" else "btn"
    # always drawn, hidden away from Fetch, so the script can show it again after a Resume or a finished run
    items = "".join(f'<button type="button" role="menuitem" class="btn" data-job="stage" data-stage="{k}" '
                    f'title="{esc(why)}">{esc(label)}</button>' for k, label, why in FETCH_STAGES)
    menu = (f'<button type="button" class="btn fmenu" data-job="fmenu" aria-haspopup="menu" aria-expanded="false" '
            f'aria-label="Fetch only part of the run: {esc(name)}"{"" if state == "fetch" else " hidden"}>▾</button>'
            f'<span class="fmenul" role="menu" hidden>{items}</span>')
    # #527 decision A: Pause on an idle dataset too, so the hourly pass skips it until Resume; a queued or running
    # dataset's one button is Pause already, and a paused one's is Resume
    idle = (f'<button type="button" class="btn sm idlep" data-job="stop" data-dataset="{esc(name)}" '
            'title="Hold this dataset: the hourly pass skips it until Resume">Pause</button>'
            '<span class="dim" role="status"></span>' if state == "fetch" else "")
    return (f'{pill}<span class="onewrap"><button type="button" class="{cls} one" data-job="one" data-state="{state}" '
            f'data-dataset="{esc(name)}" data-scope="{1 if d["fetch"]["honours_scope"] else 0}">{word}</button>'
            f'{menu}</span>{idle}')


def _local_clock(at) -> str:
    return control._local(datetime.fromtimestamp(float(at), timezone.utc)).strftime("%d %b %H:%M")


def _ds_automation(d: dict, key: str) -> str:
    """#527: this source's own Automated select; ``off`` when it has no row (avo, 10 Nov 2026: no file-level mode)."""
    mine = d.get("automation_own") or "off"
    opts = "".join(f'<option value="{m}"{" selected" if m == mine else ""}>{esc(w)}</option>'
                   for m, w in AUTOMATION_MODES)
    return ('<div class="schedset">'
            f'<label for="dsauto-{key}">Automated</label><select id="dsauto-{key}" data-dsauto>{opts}</select>'
            f'<button type="button" class="btn" data-job="automation" data-dataset="{esc(d["dataset"])}">Save</button>'
            '<span class="dim" role="status"></span></div>')


def _sched_fields(d: dict) -> str:
    """#472, moved from the old Fetch card: how often the schedule runs the dataset and a hold until a day, sent
    together by one press (``/api/build/schedule/<ds>/cadence``), so a schedule is never half applied."""
    cadence = str(d.get("cadence_effective") or "daily")
    key = esc(str(d["dataset"]).replace("/", "-"))          # a dataset name carries slashes; an id must not
    hold = d.get("next_due")
    opts = "".join(f'<option value="{o}"{" selected" if o == cadence else ""}>{o}</option>'
                   for o in ("daily", "weekly", "monthly"))
    own = "" if d.get("cadence_own") else '<span class="dim">the default</span>'
    return (_ds_automation(d, key) + '<div class="schedset">'
            f'<label for="cad-{key}">How often</label><select id="cad-{key}" data-cadence>{opts}</select>{own}'
            f'<label for="hold-{key}">Hold until</label><input type="date" id="hold-{key}" data-hold'
            + (f' value="{esc(hold)}"' if hold else "") + '>'
            f'<button type="button" class="btn" data-job="cadence" data-dataset="{esc(d["dataset"])}">Save schedule'
            '</button><span class="dim" role="status"></span></div>')


def _sbox(d: dict, view: dict) -> str:
    """One source box: five slots in the same place in every box (head, state, meters, counts, the rest)."""
    provs = list(d["providers"])
    badges = "".join(
        f'<span class="srcb" style="background:{_pcolor(view, p)}"><svg viewBox="0 0 24 24" aria-hidden="true">'
        f'<path d="{esc(control.ICONS.get(p, ""))}"/></svg>'
        f'{esc(screen.PROVIDERS[p].name if p in screen.PROVIDERS else p)}</span>' for p in provs)
    edge = _pcolor(view, provs[0]) if provs else "var(--mut)"
    word = control.STATUS_PILL.get(d["status"], ("p-bad", d["status"]))[1]
    tags = ""
    if d["attended"]:
        tags += '<span class="pill p-src">hand-run</span>'
    if d["build"]:
        tags += '<span class="pill p-new">build</span>'
    head = (f'<div class="sb1"><button type="button" class="sbtog" aria-expanded="true" aria-label="Fold '
            f'{esc(d["dataset"])}">▾</button><span class="sdot {esc(d["filter"])}" aria-hidden="true"></span>'
            f'{badges}<span class="dsname" title="{esc(d["dataset"])}">{esc(d["dataset"])}</span>{tags}'
            f'<span class="sbbtn">{_one_button(d, view)}</span></div>')
    nxt = d.get("next_words")
    since = d.get("since_words")
    state = (f'<div class="sb2"><span class="pill {esc(d["pill"])}">{esc(word)}</span>'
             f'<span class="pill cad">⏱ {esc(d["cadence"] or "not known")}</span>'
             f'<span class="snext">{esc(_next_words(nxt))}</span>'
             f'<span class="ssince">{esc(since) if since else "since: not known"}</span></div>')
    meters = ""
    if d["coverage"]:
        c = d["coverage"]
        meters += _meter("cov", c["cls"], "coverage", c["share"], c["text"], c["target"])
    a = d["age"]
    meters += _meter("age", a["cls"], "since the last check", a["share"], a["text"])
    counts = f'<span class="cnt"><span class="ck">rows</span><b>{control.count(d["rows"])}</b></span>'
    if d["added"] is not None or d["removed"] is not None:
        counts += (f'<span class="cnt"><span class="ck">last run</span><b>+{control.count(d["added"])} / '
                   f'−{control.count(d["removed"])}</b></span>')
    if d["dataset"] == "spine/security":
        counts += (f'<span class="cnt uni-n"><span class="ck">in the universe</span>'
                   f'<b>{control.count(view["universe"]["size"])}</b></span>')
    rest = _sched_fields(d) + (_fill_row(d, view) if d["fill"]["cells"] else "") + _problems(d) + _acts(d, pause=False)
    return (f'<div class="dset sbox" data-st="{esc(d["filter"])}" data-prob="{d["issues_open"]}" '
            f'data-tags="{esc(" ".join(d["tags"]))}" data-dataset="{esc(d["dataset"])}" '
            f'style="border-left-color:{edge}">{head}{state}<div class="sbody">'
            f'<div class="sb3">{meters}</div><div class="sb4">{counts}</div><div class="sb5">{rest}</div></div></div>')


def _sources(f: dict, view: dict) -> str:
    """Stage 1 of a file's panel: its sources, one box a dataset, with the one button, then the providers."""
    rows = f["datasets"]
    by = {k: sum(1 for d in rows if d["filter"] == k) for k in ("fresh", "late", "stale", "never")}
    head = (f'<div class="srch"><h3><span class="stn">1</span> Sources · {esc(f["name"])}</h3>'
            f'<span class="dim">{esc(str(f["steps"]).split(" · ", 1)[-1])}</span>'
            '<span class="sract"><button type="button" class="btn" data-sb="shut">Collapse all</button>'
            '<button type="button" class="btn" data-sb="open">Open all</button></span></div>')
    state = f'<p class="fstate">🦆 {esc(f["name"])} · {esc(_fstate_words(f.get("state")))}</p>'
    runs = (f'<p class="runs">runs on <b class="runs-on">{esc(view["universe_summary"])}</b> · set by the Universe '
            'at the top of this tab</p>' if f["runs_on_universe"] else f'<p class="runs">{esc(f["words"])}</p>')
    boxes = "".join(_sbox(d, view) for d in rows) or '<p class="dim">no dataset of this file is on record yet</p>'
    return (f'<div class="sources fgroup" data-file="{esc(f["name"])}">{head}{state}{runs}'
            f'{_button_legend()}<div class="sgrid">{boxes}</div></div>')


def _funnel(view: dict) -> str:
    """How the list comes down to the stocks and ETFs we keep; nothing when the model has no funnel."""
    fun = (view.get("universe") or {}).get("funnel")
    if not fun:
        return ""

    def last(steps):
        return steps[-1].get("n") if steps else None

    def col(title, steps):
        lines = "".join(
            f'<li><span class="fw">{esc(s.get("step"))}</span><b class="fn">'
            f'{"not counted" if s.get("n") is None else control.count(s.get("n"))}</b>'
            f'<span class="fd">{esc(s.get("drops") or "")}</span></li>' for s in steps or [])
        return f'<div class="fcol"><h4>{title}</h4><ol>{lines}</ol></div>'

    stocks, etfs = fun.get("stocks") or [], fun.get("etfs") or []
    n_s = last(stocks)
    n_e = last(etfs)
    added = fun.get("added")
    return (f'<div class="funnel"><h4 class="funh">How the list comes down to '
            f'{"not counted" if n_s is None else control.count(n_s)} stocks and '
            f'{"not counted" if n_e is None else control.count(n_e)} ETFs</h4>'
            f'<div class="fcols">{col("Stocks", stocks)}{col("ETFs", etfs)}</div>'
            + (f'<p class="dim">added by hand: {esc(added if isinstance(added, str) else control.count(added))}</p>'
               if added is not None else "") + '</div>')


#: ``next_run_words`` answers that are whole sentences, printed without the "next " prefix.
_WHOLE_SENTENCES = ("skips until", "by hand", "next ", "due", "not before")


def _next_words(nxt) -> str:
    """``next <when>`` for a time ("every trading day, …", "Fri 13 Nov, …"); a whole sentence ("skips until Resume",
    "by hand only", "next pass not known", "due · …") stands alone, after any "timer off · " it carries."""
    if not nxt:
        return "next: not known"
    words = str(nxt)
    off = "timer off · " if words.startswith("timer off · ") else ""
    core = words[len(off):]
    return words if core.startswith(_WHOLE_SENTENCES) else f"{off}next {core}"


def _uni_cards(view: dict) -> str:
    """Identity and Our universe, today's two cards, side by side, when the model carries them."""
    ident, mine = view.get("identity"), view.get("my_universe")
    if ident is None and mine is None:
        return ""
    from RevenueChaser.module_settings import report
    return (f'<div class="ucards">{report._nd_identity(ident) if ident is not None else ""}'
            f'{report._nd_my_universe(mine) if mine is not None else ""}</div>')


def _picture_control(view: dict) -> str:
    """The **Recount folders** press (#434; named "Take the disk picture" until #527). It sits in Fetch because this is where a press belongs
    (``docs/data_health.md``: the Data section shows bars and no button), while the picture's age sits beside the
    counts it explains, on the Data section's roots line."""
    walked = next((f.get("walked_at") for f in view["files"]), None)
    return ('<div class="pictrow"><span class="hm">The HDD folder counts on the Database tab are not read live &middot; last count: '
            f'{esc(_walked(walked))}</span>'
            '<button type="button" class="btn" data-job="picture" title="Walks the HDD raw and Parquet folders and counts '
            'their files and bytes again (about half a minute, no provider call)">Recount folders</button>'
            '<span class="hm picout" role="status"></span></div>')


# ── The flow diagram ──────────────────────────────────────────────────────────────────────────────────────────

def _flow(view: dict) -> str:
    fl = view["flow"]
    legend = "".join(f'<span><i style="background:{esc(f["line"])}"></i>{esc(f["name"])}</span>' for f in fl["files"])
    phone = []
    own = dict(fl["edges"].get("boardOwn") or [])
    for f in view["files"]:
        providers = sorted({p for d in f["datasets"] for p in d["providers"]})
        names = " · ".join(screen.PROVIDERS[p].name for p in providers if p in screen.PROVIDERS) or "built here"
        readers = sorted({r["label"] for t in f["tables"] for r in t["readers"]})
        boards = ", ".join(esc(r) + (f' <code class="p">→ {esc(own[r])}</code>' if r in own else "") for r in readers)
        phone.append(f'<div class="wrow"><span class="wv">{esc(names)} → <code class="p">{esc(f["name"])}</code> → '
                     + (boards or "<b>no board yet</b>") + '</span></div>')
    return (
        '<details class="sec" id="sec-flow" open><summary><h2>Data flow</h2><span class="secs">source → SSD file → '
        'board → board file · a press lights its lines · a file opens its section below · a board says what it reads '
        'and writes · a faint dashed line is a board reading another board\'s file</span></summary>'
        '<div class="group" style="border-left-color:var(--acc)"><div class="scroll flowwrap"><svg class="flowsvg" '
        'id="flow2" viewBox="0 0 1116 900" role="group" aria-labelledby="flow2-title"></svg></div>'
        '<p class="bdmatch" id="bdmatch" aria-live="polite" hidden></p>'
        '<div class="bdinfo empty" id="bdinfo" aria-live="polite" hidden></div>'
        f'<div class="flowlegend"><b>Colour:</b>{legend}<span><i style="background:#6f7b32"></i>source</span>'
        f'<span><i style="background:{control.BOARD_COLOR}"></i>board</span>'
        '<span class="dim">Source icons: Tabler Icons (MIT licence).</span></div>'
        '<div class="phoneonly"><p class="fnote">The diagram above scrolls sideways on a phone; the same data as a list follows.</p>'
        f'{"".join(phone)}</div>'
        '</div></details>')


def _file_words(f: dict) -> str:
    """What a served file's own section says beside its name: what is on disk and how many lines it carries."""
    st = f.get("state") if isinstance(f.get("state"), dict) else {}
    if st.get("exists") is False:
        disk = "no file yet"
    elif st.get("exists"):
        disk = f'🦆 {_size(st.get("bytes"))}' if st.get("bytes") is not None else "🦆 size not known"
    elif f.get("bytes") is not None:
        disk = f'🦆 {_size(f["bytes"])}'
    else:
        disk = "size not known"
    n = len(f["datasets"])
    return f'{disk} · {n} {"line" if n == 1 else "lines"}'


# ── Providers ─────────────────────────────────────────────────────────────────────────────────────────────────

def _providers(view: dict, sid: str | None = "sec-providers") -> str:
    rows = []
    for i, p in enumerate(view["providers"]):
        color = control.SOURCE_COLORS[i % len(control.SOURCE_COLORS)]
        if p["cap"] is None:
            bar = '<span class="lbar none"></span><span class="ln">no cap · counted, never stopped</span>'
            cap = '<span class="pk none"><span class="jk">our day cap</span>no cap</span>'
        else:
            share = p["share"] or 0.0
            word = "near the limit" if share > control.CAP_BAD else ("half" if share >= control.CAP_WARN
                                                                      else "comfortable")
            bcls = {"warn": "near", "bad": "full"}.get(p["cls"], "")
            bar = (f'<span class="lbar"><i class="{esc(p["cls"])}" style="width:{min(share, 1.0) * 100:.1f}%"></i>'
                   f'</span><span class="ln">{control.count(p["used"])} / {control.count(p["cap"])} today'
                   f'<b class="{bcls}">{share:.0%} · {word}</b></span>')
            cap = f'<span class="pk"><span class="jk">our day cap</span>{control.count(p["cap"])} a day</span>'
        pace_cls = " none" if p["pace"] == "none" else ""
        rule_cls = " none" if p["rule"].startswith("none") else ""
        rows.append(
            f'<div class="plim"><span class="pp"><span class="srcb" style="background:{color}"><svg viewBox="0 0 24 24" '
            f'aria-hidden="true"><path d="{esc(p["icon"])}"/></svg>{esc(p["name"])}</span>'
            f'<span class="pw">{esc(p["what"])}</span></span><span class="pu">{bar}</span>{cap}'
            f'<span class="pk{pace_cls}"><span class="jk">our pace</span>{esc(p["pace"])}</span>'
            f'<span class="pk{rule_cls}"><span class="jk">provider\'s rule</span>{esc(p["rule"])}</span></div>')
    near = [p for p in view["providers"] if p["share"] is not None and p["share"] >= control.CAP_WARN]
    secs = f'{len(view["providers"])} providers · day cap, pace and the provider\'s rule' + (
        " · " + ", ".join(f'{p["name"]} at {p["share"]:.0%}' for p in near) if near else "")
    ident = f' id="{sid}"' if sid else ""
    return (f'<details class="sec provs"{ident}><summary><h2>Providers and daily limits</h2>'
            f'<span class="secs">{esc(secs)}</span></summary><div class="group" style="border-left-color:var(--l-src)">'
            f'<p class="psrc">{PROVIDERS_SOURCE}</p>'
            '<div class="plims"><div class="plim ph" aria-hidden="true"><span>Provider</span><span>Used today against '
            'our day cap</span><span>Our day cap</span><span>Our pace</span><span>Provider\'s own rule</span></div>'
            f'{"".join(rows)}</div><p class="hint">Three kinds of limit, side by side: <b>our day cap</b> '
            '(<code>rcdata/caps.py</code>; the builder does not start a dataset once its provider reached the cap for '
            'the New York day, counted in <code>provider_calls_daily</code>), <b>our pace</b> (each source\'s own '
            'pacer) and <b>the provider\'s own rule</b>, where the code quotes one. "No cap" means none is set, never '
            'zero. The bar turns amber at half and red past 70%.</p></div></details>')


# ── Data, file by file ────────────────────────────────────────────────────────────────────────────────────────

def _walked(at, now=None) -> str:
    """When the storage-picture job last walked the trees (#434). The serving-file sizes beside it are read live, so
    the page says which of the two numbers is a picture -- and how old the picture is once it is a day past, because
    nothing refreshes it as a side effect of opening the board any more."""
    if not at:
        return "not walked yet"
    try:
        when = datetime.fromisoformat(str(at))
        hours = ((now or datetime.now(timezone.utc)) - when).total_seconds() / 3600
    except (TypeError, ValueError):
        # Including a tz-naive stamp, which subtracts against an aware "now" with a TypeError. ``walk_storage``
        # cannot write one, but ``_picture`` checks every other field of a file it does not trust, and an
        # unguarded raise here would take the whole board down -- ``report`` renders this line unwrapped.
        return "not walked yet"
    return f"walked {control._clock(at)}" + (f", {hours:.0f} h ago" if hours >= PICTURE_STALE_HOURS else "")


def _jrows(f: dict) -> tuple[str, list[str]]:
    rows, srcs = [], []
    store = f["storage"]
    # #434: the folder counts come from the storage-picture job, not from this render. Until it has run here there
    # is no picture at all, which is not the same thing as a dataset having no folder.
    walked = f.get("walked_at")
    for i, d in enumerate(f["datasets"]):
        meta_pill = ('<span class="pill p-src">hand-run</span>' if d["attended"] else
                     '<span class="pill p-ok">built</span>' if d["checked_on"] else
                     '<span class="pill p-warn">tested</span>')
        role = d["role"]
        icons = "".join(_icon(control.ICONS.get(p, ""), control.SOURCE_COLORS[0],
                              screen.PROVIDERS[p].name if p in screen.PROVIDERS else p)
                        for p in d["providers"][:3])
        names = " ".join(screen.PROVIDERS[p].name for p in d["providers"] if p in screen.PROVIDERS)
        srcs.append(names)
        where = []
        for label, icon in (("raw", "📄"), ("parquet", "📦")):
            fo = (store.get(d["dataset"]) or {}).get(label) or {}
            if walked is None:
                meta = "not walked yet"
            elif fo.get("files") is None:
                meta = "no folder yet"
            else:
                meta = (f'{fo["files"]:,}{"+" if fo.get("capped") else ""} files · {_size(fo.get("bytes"))}')
            where.append(f'<span class="hl"><span class="hi">{icon}</span><span class="hk">{label}</span>'
                         f'{_pfold(d["dataset"] + "/")}<span class="hm">{esc(meta)}</span></span>')
        tabs = " ".join(t["name"] for t in f["tables"] if d["dataset"] in t["writers"]) or \
            " ".join(t["name"] for t in f["tables"])
        chips = ""
        if role == "build":
            # One "from" chip per input step of the file, with its full name (#492).
            chips = "".join(f'<span class="chip" data-card="{esc(o["dataset"])}"><span class="cn">{o["step"]}</span>'
                            f'<span class="t">{esc(o["dataset"])}</span></span>'
                            for o in f["datasets"] if o is not d and o["role"] in INPUT_ROLES)
            chips = f'<span class="jfrom"><span class="jfl">from</span>{chips}</span>' if chips else ""
        rows.append(
            f'<tr class="jr" tabindex="0" role="button" aria-expanded="false" aria-controls="jd-{esc(f["kind"])}-{i}" '
            f'data-ds="{esc(d["dataset"])}" data-src="{esc(names)}" data-tabs="{esc(tabs)}"><td class="js">'
            f'<span class="jcar" aria-hidden="true"></span><span class="stn">{d["step"]}</span>'
            f'<span class="jttl"><b>{esc(d["label"])}</b><span class="jsrc">{icons}</span>{chips}</span>'
            f'<span class="jmeta">{meta_pill}'
            f'<span class="tag role r-{esc(role.replace(" ", "-"))}" title="{esc(control.ROLE_WORDS.get(role, ""))}">'
            f'{esc(role)}</span><span class="jcad">⏱ {esc(d["cadence"] or "")}</span></span></td>'
            f'<td class="jh">{"".join(where)}</td></tr>'
            f'<tr class="jd" id="jd-{esc(f["kind"])}-{i}" hidden><td colspan="2"><div class="jdg">'
            f'<div class="jdl"><span class="jk">code name</span><code>{esc(d["dataset"])}</code></div>'
            f'<div class="jdl"><span class="jk">how often</span><span>{esc(d["cadence"] or "")}</span></div>'
            f'<div class="jdl"><span class="jk">role</span><span>{esc(control.ROLE_WORDS.get(role, role))}</span></div>'
            f'<div class="jdl"><span class="jk">last check</span><span>{esc(control.day_words(d["checked_on"]))} · '
            f'source as of {esc(control.day_words(d["source_as_of"]))}</span></div></div></td></tr>')
    return "".join(rows), srcs


def _pfold(path: str) -> str:
    """A folder path folded to a 📁 that opens sideways on a press (avo, 10 Nov 2026: the paths took too much room)."""
    return (f'<details class="pf"><summary title="{esc(path)}" aria-label="Show the folder {esc(path)}">📁</summary>'
            f'<code class="p">{esc(path)}</code></details>')


def _tchs(f: dict) -> str:
    out = []
    for t in f["tables"]:
        read_by = ", ".join(r["label"] for r in t["readers"])
        cols = "".join(f'<span class="cn">{esc(c)}</span><span class="ct">{esc(ty.lower())}</span>'
                       for c, ty in t["columns"])
        out.append(
            f'<div class="tch" data-t="{esc(t["name"])}"><div class="tchh" style="background:{esc(t["color"])}">'
            f'<button type="button" class="tpick" data-t="{esc(t["name"])}" aria-pressed="false" '
            f'title="{esc("read by " + read_by if read_by else "no board reads it today")}" '
            f'aria-label="{esc(t["name"])}: press to light the steps that write it and the boards that read it">⚡ '
            f'{esc(t["name"])}</button><button type="button" class="topen" aria-expanded="false" '
            f'aria-label="Show the {len(t["columns"])} columns of {esc(t["name"])}"><span class="tn">'
            f'{len(t["columns"])}</span><span class="tcar" aria-hidden="true"></span></button></div>'
            f'<div class="tcols" hidden>{cols}</div></div>')
    return f'<div class="tchs">{"".join(out)}</div>'


def _btree(f: dict) -> str:
    by_board: dict[str, list] = {}
    for t in f["tables"]:
        for r in t["readers"]:
            by_board.setdefault(r["key"], []).append((t["name"], r))
    groups, reading = [], 0
    bfiles, own, bf_reads = control.board_files()
    mine, also, hue = dict(own), {}, {b["name"]: b["color"] for b in bfiles}
    for b, bf in bf_reads:
        also.setdefault(b, []).append(bf)
    for group, tabs in control.BOARD_MENU:
        items = []
        for key, label in tabs:
            label = control.PLAIN_LABELS.get(key, label)
            hits = by_board.get(key)
            if not hits:
                continue
            reading += 1
            tnames = " ".join(dict.fromkeys(t for t, _ in hits))
            needs = ", ".join(dict.fromkeys(n for _, r in hits for n in r["needs"]))
            planned = not hits[0][1]["declared"]
            title = f"reads {tnames.replace(' ', ', ')} · through {needs}" + (" · planned, not migrated yet"
                                                                               if planned else "")
            items.append(f'<li class="on{" planned" if planned else ""}" data-b="{esc(label)}" data-tabs="{esc(tnames)}"'
                         f' tabindex="0" role="button" aria-pressed="false" title="{esc(title)}">'
                         f'<i style="background:{control.BOARD_COLOR}"></i>{esc(label)}'
                         + (f'<code class="bfo" style="background:{esc(hue.get(mine[label], ""))}" '
                            f'title="the file this board writes">→ {esc(mine[label])}</code>'
                            if label in mine else "")
                         + "".join(f'<code class="bfa" style="border-color:{esc(hue.get(x, ""))}" '
                                   f'title="another board\'s file it reads">⇢ {esc(x)}</code>'
                                   for x in also.get(label, [])) + '</li>')
        if items:
            groups.append(f'<li class="tg"><span>{esc(group)}</span><ul>{"".join(items)}</ul></li>')
    total = sum(len(t) for _, t in control.BOARD_MENU)
    body = "".join(groups) or '<li class="dim">no board reads this file yet</li>'
    return (f'<div class="btree"><span class="tf">{reading} of {total} boards read it</span><ul>{body}</ul></div>')


#: Characters of room beside the widest value of a journey meta column (#527: "a little bigger, easy on the eye").
COL_PAD = 3


def _meta_widths(f: dict) -> str:
    """``--jw1..3`` for the journey's status / role / cadence columns: the widest value of this file plus a little,
    so the three sit in fixed columns and never drift with the title's length (#527)."""
    ds = f["datasets"] or [{}]
    status = max(len("hand-run" if d.get("attended") else "built" if d.get("checked_on") else "tested") for d in ds)
    role = max(len(str(d.get("role") or "")) for d in ds)
    cad = max(len("⏱ " + str(d.get("cadence") or "")) for d in ds)
    return f"--jw1:{status + COL_PAD}ch;--jw2:{role + COL_PAD}ch;--jw3:{cad + COL_PAD}ch"


def _journey(f: dict) -> str:
    rows, _ = _jrows(f)
    n = max(1, 2 * len(f["datasets"]))
    roots = f["roots"]
    ssd = (f'<code class="p jfile" title="{esc(f["path"] or f["name"])}">🦆 {esc(f["name"])}</code>{_tchs(f)}'
           f'<span class="ssz">{_size(f["bytes"])}</span>')
    if not rows:
        # A file whose datasets have no card here still has its tables.
        rows = ('<tr class="jr"><td class="js dim">no dataset of this file is on record yet</td><td class="jh"></td>'
                '</tr>')
        n = 1
    first, rest = rows.split("</tr>", 1)
    rows = (first + f'<td class="ssdc" rowspan="{n}">{ssd}</td><td class="bdc" rowspan="{n}">{_btree(f)}</td></tr>'
            + rest)
    hdd = (f'<span class="jhl">📄 HDD raw {_pfold(roots.get("raw", ""))} · 📦 Parquet {_pfold(roots.get("parquet", ""))}'
           f'</span><span class="jhm">files and size · {esc(_walked(f.get("walked_at")))}</span>')
    ssdh = f'⚡ SSD live · served {_pfold(roots.get("ssd", ""))}'
    return (
        '<div class="journey"><p class="colh">The journey of this data · one line a build step · press a line or a '
        'table: everything tied to it lights, press again to drop it</p>'
        f'<div class="jleft"><table class="jt jt4 jt3 jtb" style="{_meta_widths(f)}"><colgroup><col class="c1">'
        '<col class="c2"><col class="c3"><col class="c4"></colgroup><thead><tr><th>Build step · status · role · how often</th>'
        f'<th class="hddh">{hdd}</th><th class="ssdh">{ssdh}</th><th class="bdh">📊 Boards · their own file</th></tr>'
        f'</thead><tbody>{rows}</tbody></table></div>'
        f'<aside class="jpanel jnarrow"><div class="jssd"><span class="jph">⚡ SSD live · served</span>{ssd}</div>'
        '</aside></div>')


def _samples(f: dict) -> str:
    seen, blocks = set(), []
    for d in f["datasets"]:
        for s in d["samples"]:
            t = s.get("table")
            if not t or t in seen:
                continue
            seen.add(t)
            cols = s.get("columns") or []
            # One source key per column (#492); without it every column is the builder's and no source row is drawn.
            pv = list(s.get("pv") or [])
            keys = [str(pv[i]) if i < len(pv) and pv[i] else "build" for i in range(len(cols))]
            head = "".join(f'<th class="pv-{esc(k)}">{esc(c.get("name"))}<span class="t">'
                           f'{esc(str(c.get("type")).lower())}</span></th>' for k, c in zip(keys, cols))
            src = ""
            if pv and cols:
                labels = getattr(control, "PV_LABELS", None) or {}
                runs: list[list] = []
                for k in keys:
                    if runs and runs[-1][0] == k:
                        runs[-1][1] += 1
                    else:
                        runs.append([k, 1])
                src = '<tr class="srcrow">' + "".join(
                    f'<th class="pv-{esc(k)}" colspan="{n}">{esc(labels.get(k, k))}</th>' for k, n in runs) + "</tr>"
            body = "".join("<tr>" + "".join(
                f'<td class="pv-{esc(keys[j] if j < len(keys) else "build")}">{esc("" if v is None else v)}</td>'
                for j, v in enumerate(r)) + "</tr>" for r in s.get("rows") or [])
            if not s.get("built"):
                body = (f'<tr><td colspan="{max(1, len(cols))}" class="dim">the file is not built yet: these are the '
                        'columns the code declares</td></tr>')
            notes = "".join(f'<p class="hint">{esc(n)}</p>' for n in s.get("notes") or ())
            blocks.append(
                f'<section class="dsec" data-sample="{esc(t)}"><h3 class="dsh"><code>{esc(t)}</code> as served '
                f'<span class="dim">{len(s.get("rows") or [])} rows</span></h3><div class="dsact"><button type="button" '
                f'class="btn sm" '
                f'data-sample-refresh="{esc(t)}">Refresh the example rows</button><span class="dim">reads 5 rows of '
                f'<code>{esc(f["name"])}</code> · no provider call</span></div><div class="scroll"><table class="ex">'
                f'<thead>{src}<tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{notes}</section>')
    if not blocks:
        return ""
    return (f'<details class="blk b-ex b-ds"><summary>🗂 Data structure <span class="dim">{len(blocks)} tables · a few '
            f'rows each, with their columns and types</span></summary>{"".join(blocks)}</details>')


def _health_table(f: dict) -> str:
    rows = []
    for d in f["datasets"]:
        for r in d["recent"]:
            rows.append(f'<tr><td data-label="Dataset"><code>{esc(d["dataset"])}</code></td>'
                        f'<td data-label="When">{esc(r.get("checked_on") or "—")}</td>'
                        f'<td data-label="Result">{esc(r.get("result") or "—")}</td>'
                        f'<td data-label="As-of">{esc(r.get("source_as_of") or "—")}</td>'
                        f'<td data-label="Rows">+{control.count(r.get("added"))} / −{control.count(r.get("removed"))}'
                        f'</td><td data-label="Calls">{control.count(r.get("calls"))}</td>'
                        f'<td data-label="Note">{esc(r.get("note") or "—")}</td></tr>')
    probs = sum(d["issues_open"] for d in f["datasets"])
    body = ("".join(rows) if rows else
            '<tr><td colspan="7" class="dim">no run on record for any dataset of this file</td></tr>')
    return ('<details class="blk b-hlt"><summary>🩺 Health <span class="dim">the last three runs of each dataset, open '
            'problems</span></summary><div class="scroll"><table class="stk"><thead><tr><th>Dataset</th><th>When</th>'
            '<th>Result</th><th>As-of</th><th>Rows</th><th>Calls</th><th>Note</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div><p class="hint">Open problems in this file: {probs:,} · each is listed '
            'under its source box above.</p></details>')


# ── Cross-check, queue ────────────────────────────────────────────────────────────────────────────────────────

def _xsource(c: dict, view: dict) -> str:
    """The second source as the journey shows a source: its provider badge, or a grey pill when there is none."""
    name = c.get("second_source")
    if not name:
        return '<span class="pill p-mut">no second source</span>'
    pid = c.get("provider")
    icon = (f'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="{esc(control.ICONS.get(pid, ""))}"/></svg>'
            if pid in control.ICONS else "")
    return f'<span class="srcb" style="background:{_pcolor(view, pid)}">{icon}{esc(name)}</span>'


def _differs(n) -> str:
    if n is None:
        return '<span class="dim">—</span>'
    return f'<span class="pill p-ok">0 differ</span>' if not n else f'<span class="pill p-bad">{int(n):,} differ</span>'


def _cross(view: dict) -> str:
    """#527 step 8b: the same badges, pills and fonts as the journey table; the short columns never wrap."""
    rows = []
    for c in view["cross_check"]:
        rows.append(f'<tr><td data-label="Field"><b>{esc(c.get("field"))}</b> <code class="p">{esc(c.get("need") or "")}'
                    f'</code></td><td data-label="Second source">{_xsource(c, view)}</td>'
                    f'<td data-label="Compared">{esc(c.get("checked") or "—")}</td>'
                    f'<td data-label="Differs">{_differs(c.get("differs"))}</td>'
                    f'<td data-label="Largest deviation">{esc(c.get("worst") or "—")}</td>'
                    f'<td data-label="When">{esc(c.get("as_of") or "none yet")}</td></tr>')
        if c.get("command"):
            rows.append(f'<tr><td colspan="6" class="dim">hand-run: <code>{esc(c["command"])}</code></td></tr>')
    body = "".join(rows) or '<tr><td colspan="6" class="dim">no cross-check recorded</td></tr>'
    return ('<details class="sec" id="sec-crosscheck"><summary><h2>Cross-check · first ↔ second source</h2>'
            f'<span class="secs">{len(view["cross_check"])} reports</span></summary>'
            '<div class="group" style="border-left-color:var(--new)"><div class="scroll"><table class="stk xck">'
            '<thead><tr><th>Field</th><th>Second source</th><th>Compared</th><th>Differs</th><th>Largest deviation</th>'
            f'<th>When</th></tr></thead><tbody>{body}</tbody></table></div><p class="hint">Where a field has no second '
            'source the row says so; no source is invented. The report shown is the one that exists: this page makes '
            'no new call.</p></div></details>')


#: A queue note longer than this is cut to one line; the whole note is one tap away.
NOTE_CUT = 120


def _note(text) -> str:
    t = str(text or "—")
    if len(t) <= NOTE_CUT:
        return esc(t)
    return f'<details class="note"><summary>{esc(t[:NOTE_CUT])}…</summary>{esc(t)}</details>'


def _queue(view: dict) -> str:
    q = view["queue"]
    lines = {r.get("id"): r for r in view.get("running") or []}
    rows = []
    for r in list(q.get("active") or []) + list(q.get("finished") or [])[:10]:
        stop = (f'<button type="button" class="btn" data-job="stop" data-dataset="{esc(r.get("dataset"))}" '
                f'data-status="{esc(r.get("status"))}">Stop</button><span class="dim" role="status"></span>'
                if r.get("status") in ("queued", "running") else "")
        run = lines.get(r.get("id"))
        run_line = (f'<span class="runl {esc(run.get("cls") or "")}">{esc(run["line"].split(" · ", 1)[-1])}</span>'
                    if run else "")
        when = control._clock(r.get("requested_at")) if r.get("requested_at") else "—"
        rows.append(f'<tr><td data-label="Dataset"><code class="dsn">{esc(r.get("dataset"))}</code>{run_line}</td>'
                    f'<td data-label="Status"><span class="stbtn">{esc(r.get("status"))}{stop}</span></td>'
                    f'<td data-label="Asked">{esc(r.get("requested_by") or "—")} · {esc(when)}</td>'
                    f'<td data-label="Note">{_note(r.get("note"))}</td></tr>')
    body = "".join(rows) or '<tr><td colspan="4" class="dim">nothing queued, nothing run yet</td></tr>'
    waiting = len(q.get("active") or [])
    # #527: open while something waits or runs, so a running job's line is in sight; closed when the queue is empty
    return (f'<details class="sec" id="sec-queue"{" open" if waiting else ""}><summary><h2>Queue and last runs</h2>'
            f'<span class="secs">{waiting} waiting or running · the last finished requests</span></summary>'
            '<div class="group" style="border-left-color:var(--mut)"><div class="scroll"><table class="stk qtbl" '
            'id="ctl-queue"><thead><tr><th>Dataset</th><th>Status</th><th>Asked</th><th>Note</th></tr>'
            f'</thead><tbody>{body}</tbody></table></div><p class="hint">Stop pauses the dataset: a queued request '
            'is cancelled, a running fetch stops before its next provider call (the files it fetched stay), and '
            'the hourly pass skips the dataset until Resume. A running line reads time so far / its limit · who '
            'started it; a Claude session\'s command is cut at 2 h.</p></div></details>')


def _bcls(row: dict) -> str:
    """A backup row's colour: red past the alert age or with nothing kept, amber past the warn age."""
    return "s-bad" if row.get("alert") else ("s-warn" if row.get("warn") else "s-ok")


def _backups(view: dict) -> str:
    """The weekly backup, on the page itself (#715 step 2; avo, 11 Nov 2026). One row a step, one row a served
    file: how old the newest copy is and how many are kept. It reads what the backup job recorded and starts
    nothing."""
    b = view.get("backups") or {}
    rows, bad = [], 0
    if not b:
        return ""
    if not b.get("configured"):
        rows.append('<tr><td data-label="What">Backup folder</td>'
                    '<td data-label="State" class="s-bad">not set: RAW_STORE_BACKUP_DIR is empty</td></tr>')
        bad += 1
    elif b.get("root"):
        rows.append('<tr><td data-label="What">Backup folder</td>'
                    f'<td data-label="State"><code>{esc(b.get("root"))}</code></td></tr>')
    for st in b.get("steps") or []:
        age = st.get("age_days")
        text = "never ran" if age is None else f"last good run {age} d ago · {esc(st.get('last_ok') or '—')}"
        if st.get("error"):
            text += f" · last run failed: {esc(st['error'])}"
        bad += 1 if st.get("alert") else 0
        rows.append(f'<tr><td data-label="What">Step {esc(st.get("step"))}</td>'
                    f'<td data-label="State" class="{_bcls(st)}">{text}</td></tr>')
    for k in b.get("kinds") or []:
        age = k.get("age_days")
        text = ("no copy" if age is None else f"{age} d old · {esc(k.get('newest') or '—')}") \
            + f" · {esc(k.get('copies') or 0)} kept"
        bad += 1 if k.get("alert") else 0
        rows.append(f'<tr><td data-label="What">Newest {esc(k.get("kind"))} copy</td>'
                    f'<td data-label="State" class="{_bcls(k)}">{text}</td></tr>')
    body = "".join(rows) or '<tr><td colspan="2" class="dim">no backup recorded</td></tr>'
    words = (f'{bad} past its limit' if bad else "every copy inside its limit")
    return (f'<details class="sec" id="sec-backups"{" open" if bad else ""}><summary><h2>Weekly backups</h2>'
            f'<span class="secs">{words}</span></summary>'
            '<div class="group" style="border-left-color:var(--mut)"><div class="scroll">'
            '<table class="stk bktbl"><thead><tr><th>What</th><th>State</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div><p class="hint">One job every Sunday 03:30 keeps the newest '
            f'{backup.KEEP} copies of every file on the HDD. Amber past '
            f'{esc(b.get("warn_days") or "?")} days, red past {esc(b.get("alert_days") or "?")}. This page only reads what that '
            'job recorded; it copies nothing.</p></div></details>')


# ── The page's data and the whole section ─────────────────────────────────────────────────────────────────────

def client(view: dict) -> dict:
    """What the script reads: routes, the universe (sets, groups, tickers of B's parts), the calendars, the fill
    switch and the minute the page was read."""
    u = view["universe"]
    cals = {}
    for name, c in view["calendars"].items():
        cals[_cal_key(name)] = {**c, "sets": [{"k": s["key"], "label": s["label"], "n": s["n"] or 0}
                                              for s in u["sets"] if s["offered"]]}
    return {"routes": ROUTES, "read_at": view["read_at_iso"], "stale_ms": STALE_MINUTES * 60 * 1000,
            "fill_enabled": view["fill_enabled"], "fill_off_why": view["fill_off_why"],
            "universe": {"sets": [{"k": s["key"], "tier": s["tier"], "label": s["label"], "n": s["n"] or 0,
                                   "on": s["on"]} for s in u["sets"] if s["offered"]],
                         "include": [g["key"] for g in u["include"]], "tickers": u["tickers"],
                         "bits": board_settings.UNIVERSE_SET_BITS,
                         "inc_bits": board_settings.UNIVERSE_INCLUDE_BITS},
            "calendars": cals, "today": view["read_at_iso"][:10]}


def _err(view: dict, title: str = "Database") -> str:
    """The one line a panel is when the model could not be built."""
    return (f'<section class="ctl"><div class="wrap"><h1>Data/Health · {esc(title)}</h1><div class="errbar"><b>Could not read</b> '
            f'· {esc(view["error"])}</div></div></section>')


def _ready(view: dict) -> dict:
    """``view`` with the universe summary its sections print."""
    return {**view, "universe_summary": " + ".join(s["label"] for s in view["universe"]["sets"] if s["on"])
            + f' · {control.count(view["universe"]["size"])} names'}


#: The roles whose steps feed a build row, hand-run ones included: never a cross-check ("compared, never served")
#: or a build.
INPUT_ROLES = ("first", "only source", "fills")


def _panel(f: dict, view: dict, first: bool) -> str:
    """One served file's own folded section on the Database tab: its journey, the folder recount press and its
    example rows. Its sources and their runs are on the Fetch/Health tab (``_fetch_group``).

    #715 step 3 (avo, 11 Nov 2026: "Hicbir sekmenin altinda baska sekme olmayacak"): the six files used to be a
    ``role="tablist"`` picker with one panel showing at a time. They are stacked folded sections now, like the
    Fetch tab's groups; the first one is open.
    """
    return (f'<details class="sec fpanel" data-file="{esc(f["name"])}" id="fp-{esc(f["kind"])}" '
            f'style="--fh:{esc(f["color"])}"{" open" if first else ""}>'
            f'<summary><h2>🦆 {esc(f["name"])}</h2><span class="secs">{esc(_file_words(f))}</span></summary>'
            f'<div class="fpbody">{_journey(f)}{_samples(f)}</div></details>')


def _fetch_group(f: dict, view: dict) -> str:
    """One served file on the Fetch/Health tab, folded like a Jobs group: its sources with their presses and
    schedules, then the last runs of each dataset. Open when one of its datasets is late or stale."""
    rows = f["datasets"]
    by = {k: sum(1 for d in rows if d["filter"] == k) for k in ("fresh", "late", "stale", "never")}
    bad = by["late"] + by["stale"]
    words = (f'{len(rows)} sources · <b class="s-ok">{by["fresh"]} fresh</b> · <b class="s-warn">{by["late"]} late</b>'
             f' · <b class="s-bad">{by["stale"]} stale</b> · {by["never"]} never ran')
    return (f'<details class="sec fgrp" data-file="{esc(f["name"])}" style="--fh:{esc(f["color"])}"'
            f'{" open" if bad else ""}><summary><h2>🦆 {esc(f["name"])}</h2><span class="secs">{words}</span>'
            f'</summary>{_sources(f, view)}{_health_table(f)}</details>')


def render_data(view: dict, lookup_html: str = "") -> str:
    """The Data tab's panel (#331, #492): the header, the Ticker lookup (#715), Health right now, the flow diagram,
    then one folded section a served file in plan order (#715 step 3: no picker, no nested tabs).

    ``lookup_html`` rides inside the board rather than in a sibling block of its own, so the page's own background
    never shows between the help band and the board (e2e ``test_the_what_am_i_looking_at_band_follows_the_theme``).
    """
    if view.get("error"):
        return _err(view) + lookup_html
    view = _ready(view)
    panels = "".join(_panel(f, view, i == 0) for i, f in enumerate(view["files"]))
    return ('<section id="ctl" class="ctl" aria-label="Data/Health · Data control board"><div class="wrap">'
            f'{_header(view)}{lookup_html}{_flow(view)}'
            f'<div class="fpanels">{panels}</div></div></section>')


def render_fetch(view: dict) -> str:
    """The Fetch/Health tab's top (avo, 10 Nov 2026): the switches, Health right now, then one folded group a served
    file with its sources and runs, then the queue, Cross-check and the weekly backups (#715 step 2). The Health
    tab's parts follow it in the same panel."""
    if view.get("error"):
        return _err(view, "Fetch/Health")
    view = _ready(view)
    groups = "".join(_fetch_group(f, view) for f in view["files"])
    return ('<section class="ctl" aria-label="Data/Health · Fetch/Health"><div class="wrap">'
            f'{_header(view, "Fetch/Health")}{_topbar(view)}{_health(view)}{_providers(view)}'
            f'<details class="sec" id="sec-universe" open><summary><h2>Universe</h2><span class="secs">'
            f'{esc(view["universe_summary"])} · the tier sets the next fetches and fills use</span></summary>'
            f'{_universe(view)}{_legend(view)}</details>'
            '<div class="ffilter" id="ffilter" hidden></div><div class="emptyst" id="ffempty" hidden></div>'
            f'<div class="fgrps">{groups}</div>{_picture_control(view)}{_queue(view)}{_cross(view)}'
            f'{_backups(view)}</div></section>')


def scripts(view: dict) -> str:
    """The page's data and behaviour, one inline block (after ``ESC_JS``, which defines ``rcEsc``)."""
    if view.get("error"):
        return ""
    return (f"window.RC_FLOW = {script_json(view['flow'])};\nwindow.CTL = {script_json(client(view))};\n{CTL_JS}\n{TREE_JS}")
