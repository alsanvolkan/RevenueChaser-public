"""The Settings / Data control board's HTML (design v28), from ``control.build``'s model.

Every string goes through ``html_safe.esc``; the page's own data (the flow diagram, the calendars, the universe)
rides in one ``script_json`` block the script reads. The styles are ``static/control.css``, scoped to ``#ctl``, and
the behaviour ``static/control.js``. Nothing a press does here fetches by itself: Fetch and Pause put one request in
the builder's queue, exactly as before; Apply saves the universe; Fill and "Fill this range" stay off unless the
server's ``RC_FILL_ENABLED`` is set (avo's word); "Refresh the example rows" reads five rows of the live file.
"""
from __future__ import annotations

from pathlib import Path

from RevenueChaser.core.html_safe import esc, script_json
from RevenueChaser.module_settings import control
from RevenueChaser.rcdata.api import screen

_STATIC = Path(__file__).resolve().parent / "static"
CTL_CSS = (_STATIC / "control.css").read_text(encoding="utf-8")
CTL_JS = (_STATIC / "control.js").read_text(encoding="utf-8")

#: The routes a press uses; the script reads them from the page's data, so a test sees every route in one place.
ROUTES = {"request": "/api/build/request/", "schedule": "/api/build/schedule/", "queue": "/api/build/queue",
          "settings": "/api/settings/board/module_settings", "sample": "/api/settings/sample/",
          "job": "/api/job/"}
#: After this long the page says it is stale and offers a refresh (the design: "refreshes after 60 minutes").
STALE_MINUTES = 60
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
        "<p><b>What a press does.</b> Fetch and Pause put one request in the builder's queue (it runs at its next "
        "pass); a queued request cannot be taken back, there is no cancel route. Apply saves the universe. Fill and "
        "&ldquo;Fill this range&rdquo; stay off until your word. Refresh the example rows reads five rows of the live "
        "file and calls no provider.</p>")


def _header(view: dict) -> str:
    return (f'<h1>Settings · Data</h1>'
            f'<p class="sub"><b>Read {esc(view["read_at"])}</b> (Amsterdam) · source: <code>ops.sqlite</code> + the '
            f'serving files · after {STALE_MINUTES} minutes a strip on top says the numbers are old '
            '<button class="wiali" type="button" aria-expanded="false" aria-controls="wial">What am I looking at?'
            '</button></p>'
            f'<div class="wialbox" id="wial" hidden>{WIAL}</div>'
            f'<div class="stalebar" id="ctl-stale" hidden><b>Stale</b> · this page was read at {esc(view["read_at"])} · '
            '<button type="button" class="lnk" data-reload>Refresh</button></div>')


def _health(view: dict) -> str:
    h = view["health"]
    tiles = []
    for t in h["tiles"]:
        inner = f'<span class="n">{esc(t["n"])}</span><span class="l">{esc(t["label"])}</span>'
        if t["filter"]:
            tiles.append(f'<button type="button" class="tile {esc(t["cls"])}" data-f="{esc(t["filter"])}">{inner}'
                         '</button>')
        else:
            tiles.append(f'<div class="tile {esc(t["cls"])} static">{inner}</div>')
    c = h["counts"]
    summ = (f'<b class="s-ok">{c["fresh"]} fresh</b> · <b class="s-warn">{c["late"]} late</b> · '
            f'<b class="s-bad">{c["stale"]} stale</b> · {c["never"]} never ran · {h["problems"]} open problems')
    return ('<details class="sec" id="sec-health" open><summary><h2>Health right now</h2>'
            f'<span class="secs">{summ}</span></summary><div class="strip">{"".join(tiles)}</div></details>')


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
                f'<span class="un">{control.count(s["n"])}</span></button>')
        else:
            segs.append(
                f'<span class="useg uoff" role="img" data-tier="{esc(s["key"])}" data-n="{int(n)}" tabindex="0" '
                f'aria-label="{esc(label)}" data-tip="{esc(s["label"] + " · " + s["words"])}" '
                f'style="flex-grow:{max(int(n), 1)}"><b>{esc(s["label"])}</b> <span class="un">'
                f'{control.count(s["n"])}</span> <span class="uoffl">not offered</span></span>')
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
        '<div class="ubar" role="group" aria-label="Tier sets in the universe · hover a set, or press and hold on a '
        f'phone, for what it holds">{"".join(segs)}</div>'
        f'<p class="ulive">{live}C and D are shown for the whole picture, not offered: hover them, or press and hold, '
        f'for why. All tiers: {control.count(u.get("total"))} tickers on the spine'
        + (f' of {esc(control.day_words(u["as_of"]))}' if u.get("as_of") else "") + '.</p>'
        f'<div class="uinch"><span class="dim">A narrowed press also asks for</span>{inc}</div>'
        '<div class="udraft" id="uni-draft" hidden></div>'
        '<div class="uhand"><label for="uni-names">Names by hand</label>'
        '<input id="uni-names" type="text" placeholder="e.g. TSM, ASML · comma separated" autocomplete="off">'
        '<button type="button" class="btn" id="uni-add">Add</button><span class="uhchips" id="uni-hchips"></span>'
        '<span class="dim" id="uni-handn">no names added</span></div>'
        '<p class="dim uhnote">Names typed by hand stay in this browser until Apply sends them with the next press; '
        'they are not saved on the server.</p></div>')


def _legend(view: dict) -> str:
    fill = ("the Fill buttons are on (RC_FILL_ENABLED is set on this server)" if view["fill_enabled"]
            else view["fill_off_why"])
    return ('<div class="flegend"><span class="lg2 s-notpicked">a tier set: in the universe or not</span>'
            '<span class="lg2 s-nodata">no tier choice · no button</span>'
            f'<span class="dim">A Fill cell shows its set and whether the universe holds it. Where a dataset has days to pick (alpaca/bars), a press opens '
            f'its Backfill calendar with that set ticked, and the calendar says names, calls and nights before you '
            f'start. Fill: {esc(fill)}.</span>'
            '<div class="lgrow"><b>After a press</b> the button shows its request:<span class="lg3 st-q">queued</span>'
            '<span class="lg3 st-r">running</span><span class="lg3 st-d">done</span>'
            '<span class="lg3 st-f">failed</span><span class="lg3 st-p">paused</span></div></div>')


# ── Fetch: one row a dataset ──────────────────────────────────────────────────────────────────────────────────

def _fetch_buttons(d: dict, view: dict) -> tuple[str, str]:
    name = d["dataset"]
    if d["attended"]:
        return ((f'<button type="button" class="btn" data-job="copy" data-cmd="{esc(d["command"] or "")}">'
                 'Copy the command</button><button type="button" class="btn" disabled>Fetch</button>'),
                '<b>Fetch is off:</b> a hand-run dataset · <b>Copy the command</b> runs it in the foreground on the '
                'server; it asks for Duo')
    if not d["fetch"]["self"]:
        return ('<span class="dbnote">fetched by <button type="button" class="lnk" data-goto="universe">Fetch the '
                'universe</button> above</span>', "")
    f = d["fetch"]
    runs = ""
    if f["honours_scope"] and view["scope"] is not None:
        runs = " · runs on the universe above"
    left = ""
    if f["provider"] and f["cap"] is not None and f["remaining"] is not None:
        left = f' · {control.count(f["remaining"])} of {control.count(f["cap"])} {esc(f["provider"])} calls left today'
    return (f'<button type="button" class="btn go" data-job="fetch" data-dataset="{esc(name)}" '
            f'data-scope="{1 if f["honours_scope"] else 0}">Fetch</button>',
            f'<b>Fetch</b> puts one request in the queue{runs}{left} · <b>cannot be taken back:</b> a queued request '
            'cannot be cancelled')


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


def _acts(d: dict) -> str:
    parts, cost = [], []
    if d["calendar"]:
        key = _cal_key(d["dataset"])
        parts.append(f'<button type="button" class="btn bfbtn" data-bf="{esc(key)}" aria-expanded="false" '
                     f'aria-controls="bf-{esc(key)}">Backfill… <span class="bfc">calendar</span></button>')
        cost.append('<b>Backfill</b> the calendar picks the days and the sets; "Fill this range" stays off until '
                    'your word')
    if not d["attended"] and d["fetch"]["self"]:
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


def _dset(d: dict, view: dict) -> str:
    pills = ""
    if d["attended"]:
        pills += '<span class="pill p-src">hand-run</span>'
    if d["build"]:
        pills += '<span class="pill p-new">build</span>'
    pills += f'<span class="pill {esc(d["pill"])}">{esc(d["status_words"])}</span>'
    counts = f'<span class="cnt"><span class="ck">rows</span><b>{control.count(d["rows"])}</b></span>'
    if d["added"] is not None or d["removed"] is not None:
        counts += (f'<span class="cnt"><span class="ck">last run</span><b>+{control.count(d["added"])} / '
                   f'−{control.count(d["removed"])}</b></span>')
    if d["dataset"] == "spine/security":
        counts += (f'<span class="cnt uni-n"><span class="ck">in the universe</span>'
                   f'<b>{control.count(view["universe"]["size"])}</b></span>')
    meters = ""
    if d["coverage"]:
        c = d["coverage"]
        meters += _meter("cov", c["cls"], "coverage", c["share"], c["text"], c["target"])
    a = d["age"]
    meters += _meter("age", a["cls"], "since the last check", a["share"], a["text"])
    buttons, cost = _fetch_buttons(d, view)
    return (
        f'<div class="dset" data-st="{esc(d["filter"])}" data-prob="{d["issues_open"]}" '
        f'data-tags="{esc(" ".join(d["tags"]))}" data-dataset="{esc(d["dataset"])}">'
        f'<div class="dshead"><span class="dsname">{esc(d["dataset"])}</span><span class="dslabel">{esc(d["label"])}'
        f'</span>{pills}</div><div class="dbar"><div class="dbc">{counts}</div>{meters}'
        f'<div class="dbb">{buttons}</div>' + (f'<p class="cost">{cost}</p>' if cost else "") + '</div>'
        f'{_fill_row(d, view)}{_problems(d)}{_acts(d)}</div>')


def _fetch(view: dict) -> str:
    groups = []
    for f in view["files"]:
        pills = "".join(f'<span class="pill {esc(c)}">{esc(t)}</span>' for c, t in f["health"]["pills"])
        runs = (f'runs on <b class="runs-on">{esc(view["universe_summary"])}</b> · set by the universe at the top '
                'of this section' if f["runs_on_universe"] else esc(f["words"]))
        rows = "".join(_dset(d, view) for d in f["datasets"])
        groups.append(f'<div class="fgroup" style="--fh:{esc(f["color"])}" data-file="{esc(f["name"])}">'
                      f'<div class="fgh"><span class="fgn">{esc(f["name"])}</span><span class="gstep">'
                      f'{esc(f["steps"])}</span>{pills}</div><p class="runs">{runs}</p>{rows}</div>')
    return ('<details class="sec" id="sec-fetch" open><summary><h2>Fetch · every dataset, its bar, its Fill by tier '
            'set</h2><span class="secs">the universe · one bar a dataset · Fill and Backfill</span></summary>'
            '<div class="fetchwrap"><div class="ffilter" id="ffilter" hidden></div><div class="emptyst" id="ffempty" '
            f'hidden></div>{_universe(view)}{_legend(view)}{"".join(groups)}</div></details>')


# ── The flow diagram ──────────────────────────────────────────────────────────────────────────────────────────

def _flow(view: dict) -> str:
    fl = view["flow"]
    legend = "".join(f'<span><i style="background:{esc(f["line"])}"></i>{esc(f["name"])}</span>' for f in fl["files"])
    phone = []
    for f in view["files"]:
        providers = sorted({p for d in f["datasets"] for p in d["providers"]})
        names = " · ".join(screen.PROVIDERS[p].name for p in providers if p in screen.PROVIDERS) or "built here"
        readers = sorted({r["label"] for t in f["tables"] for r in t["readers"]})
        phone.append(f'<div class="wrow"><span class="wk s1">{esc(f["steps"])}</span><span class="wv">{esc(names)} → '
                     f'<code class="p">{esc(f["name"])}</code> → '
                     + (esc(", ".join(readers)) if readers else "<b>no board yet</b>") + '</span></div>')
    return (
        '<details class="sec" id="sec-flow"><summary><h2>Where it comes from, who fetches it, where it ends · flow '
        'diagram</h2><span class="secs">source → plan step → SSD file → board · a press filters the Data section '
        'below and opens it · a board says what it reads</span></summary>'
        '<div class="group" style="border-left-color:var(--acc)"><div class="scroll flowwrap"><svg class="flowsvg" '
        'id="flow2" viewBox="0 0 1116 900" role="group" aria-labelledby="flow2-title"></svg></div>'
        '<p class="bdmatch" id="bdmatch" aria-live="polite" hidden></p>'
        '<div class="bdinfo empty" id="bdinfo" aria-live="polite"></div>'
        '<p class="fnote">Press any box — a source, a step, a file, a table, a board — to keep only its own lines: what '
        'feeds it and what it feeds; the rest fades and the Data section below follows. Press it again to drop it. '
        'Several picks in one column = any of them; picks in different columns = all must hold. ▸ on a file opens its '
        'tables. A dashed line is a board not migrated yet: what it is planned to read, not a measurement. Source '
        'icons: Tabler Icons (MIT licence).</p>'
        f'<div class="flowlegend"><b>Colour:</b>{legend}<span><i style="background:#6f7b32"></i>source</span>'
        f'<span><i style="background:{control.BOARD_COLOR}"></i>board</span></div>'
        '<div class="phoneonly"><p class="fnote">On a phone, the same data as a list instead of the diagram.</p>'
        f'{"".join(phone)}</div>'
        '<p class="hint">The diagram is drawn from the code: each dataset\'s providers and plan step, the serving '
        'files\' schemas, and each board\'s declared reads (solid) or planned reads (dashed) through the need '
        'catalog.</p></div></details>')


# ── Providers ─────────────────────────────────────────────────────────────────────────────────────────────────

def _providers(view: dict) -> str:
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
    return ('<details class="sec" id="sec-providers"><summary><h2>Providers and daily limits</h2>'
            f'<span class="secs">{esc(secs)}</span></summary><div class="group" style="border-left-color:var(--l-src)">'
            '<div class="plims"><div class="plim ph" aria-hidden="true"><span>Provider</span><span>Used today against '
            'our day cap</span><span>Our day cap</span><span>Our pace</span><span>Provider\'s own rule</span></div>'
            f'{"".join(rows)}</div><p class="hint">Three kinds of limit, side by side: <b>our day cap</b> '
            '(<code>rcdata/caps.py</code>; the builder does not start a dataset once its provider reached the cap for '
            'the New York day, counted in <code>provider_calls_daily</code>), <b>our pace</b> (each source\'s own '
            'pacer) and <b>the provider\'s own rule</b>, where the code quotes one. "No cap" means none is set, never '
            'zero. The bar turns amber at half and red past 70%.</p></div></details>')


# ── Data, file by file ────────────────────────────────────────────────────────────────────────────────────────

def _jrows(f: dict) -> tuple[str, list[str]]:
    rows, srcs = [], []
    store = f["storage"]
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
            if fo.get("files") is None:
                meta = "no folder yet"
            else:
                meta = (f'{fo["files"]:,}{"+" if fo.get("capped") else ""} files · {_size(fo.get("bytes"))}')
            where.append(f'<span class="hl"><span class="hi">{icon}</span><code class="p">{esc(d["dataset"])}/</code>'
                         f'<span class="hm">{esc(meta)}</span></span>')
        tabs = " ".join(t["name"] for t in f["tables"] if d["dataset"] in t["writers"]) or \
            " ".join(t["name"] for t in f["tables"])
        rows.append(
            f'<tr class="jr" tabindex="0" role="button" aria-expanded="false" aria-controls="jd-{esc(f["kind"])}-{i}" '
            f'data-src="{esc(names)}" data-tabs="{esc(tabs)}"><td class="js"><span class="jcar" aria-hidden="true">'
            f'</span><span class="stn">{d["step"]}</span>{icons}<b>{esc(d["label"])}</b>{meta_pill}'
            f'<span class="tag role r-{esc(role.replace(" ", "-"))}" title="{esc(control.ROLE_WORDS.get(role, ""))}">'
            f'{esc(role)}</span><span class="jcad">⏱ {esc(d["cadence"] or "")}</span></td>'
            f'<td class="jh">{"".join(where)}</td></tr>'
            f'<tr class="jd" id="jd-{esc(f["kind"])}-{i}" hidden><td colspan="2"><div class="jdg">'
            f'<div class="jdl"><span class="jk">code name</span><code>{esc(d["dataset"])}</code></div>'
            f'<div class="jdl"><span class="jk">how often</span><span>{esc(d["cadence"] or "")}</span></div>'
            f'<div class="jdl"><span class="jk">role</span><span>{esc(control.ROLE_WORDS.get(role, role))}</span></div>'
            f'<div class="jdl"><span class="jk">last check</span><span>{esc(control.day_words(d["checked_on"]))} · '
            f'source as of {esc(control.day_words(d["source_as_of"]))}</span></div></div></td></tr>')
    return "".join(rows), srcs


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
    for group, tabs in control.BOARD_MENU:
        items = []
        for key, label in tabs:
            label = control.PLAIN_LABELS.get(key, label)
            hits = by_board.get(key)
            if not hits:
                items.append(f'<li class="off" title="reads no table of this file"><i></i>{esc(label)}</li>')
                continue
            reading += 1
            tnames = " ".join(dict.fromkeys(t for t, _ in hits))
            needs = ", ".join(dict.fromkeys(n for _, r in hits for n in r["needs"]))
            planned = not hits[0][1]["declared"]
            title = f"reads {tnames.replace(' ', ', ')} · through {needs}" + (" · planned, not migrated yet"
                                                                               if planned else "")
            items.append(f'<li class="on{" planned" if planned else ""}" data-b="{esc(label)}" data-tabs="{esc(tnames)}"'
                         f' tabindex="0" role="button" aria-pressed="false" title="{esc(title)}">'
                         f'<i style="background:{control.BOARD_COLOR}"></i>{esc(label)}</li>')
        groups.append(f'<li class="tg"><span>{esc(group)}</span><ul>{"".join(items)}</ul></li>')
    total = sum(len(t) for _, t in control.BOARD_MENU)
    return (f'<div class="btree"><span class="tf">{esc(f["name"])} <span class="dim">{reading} / {total}</span></span>'
            f'<ul>{"".join(groups)}</ul></div>')


def _journey(f: dict) -> str:
    rows, _ = _jrows(f)
    n = max(1, 2 * len(f["datasets"]))
    roots = f["roots"]
    ssd = (f'<code class="p">{esc(f["path"] or f["name"])}</code>{_tchs(f)}<span class="ssz">{_size(f["bytes"])}'
           '</span><span class="dim"> · the file is one piece: the builder builds the new one beside it and moves it '
           'into place</span><span class="tnote">press a name to pick the table · ▸ shows its columns, from the '
           "code's schema</span>")
    tree = _btree(f)
    if not rows:
        # A file whose datasets have no card here still has its tables and its readers.
        rows = ('<tr class="jr"><td class="js dim">no dataset of this file is on record yet</td><td class="jh"></td>'
                '</tr>')
        n = 1
    first, rest = rows.split("</tr>", 1)
    rows = (first + f'<td class="ssdc" rowspan="{n}">{ssd}</td><td class="bdc" rowspan="{n}">{tree}</td></tr>'
            + rest)
    return (
        '<div class="journey"><p class="colh">The journey of this data · one line a build step · press a line, a table '
        'or a board: everything tied to it lights in all four columns, press again to drop it</p>'
        f'<p class="rootline">Roots · <code class="p">📄 {esc(roots.get("raw", ""))}</code> <code class="p">📦 '
        f'{esc(roots.get("parquet", ""))}</code> <code class="p">⚡ {esc(roots.get("ssd", ""))}</code></p>'
        '<div class="jleft"><table class="jt jt4"><colgroup><col class="c1"><col class="c2"><col class="c3">'
        '<col class="c4"></colgroup><thead><tr><th>Build step · status · role · how often</th><th>📄 HDD raw over 📦 '
        'Parquet · files and size</th><th class="ssdh">⚡ SSD live · served</th><th class="bdh">📊 Who reads it '
        f'today</th></tr></thead><tbody>{rows}</tbody></table></div>'
        f'<aside class="jpanel jnarrow"><div class="jssd"><span class="jph">⚡ SSD live · served</span>{ssd}</div>'
        f'<div class="jbd"><span class="jph">📊 Who reads it today</span><span class="jps">file → menu group → board · '
        f'faded = reads no table of this file · press a board to pick it</span>{tree}</div></aside></div>')


def _samples(f: dict) -> str:
    seen, blocks = set(), []
    for d in f["datasets"]:
        for s in d["samples"]:
            t = s.get("table")
            if not t or t in seen:
                continue
            seen.add(t)
            cols = s.get("columns") or []
            head = "".join(f'<th class="pv-build">{esc(c.get("name"))}<span class="t">{esc(str(c.get("type")).lower())}'
                           '</span></th>' for c in cols)
            body = "".join("<tr>" + "".join(f'<td class="pv-build">{esc("" if v is None else v)}</td>' for v in r)
                           + "</tr>" for r in s.get("rows") or [])
            if not s.get("built"):
                body = (f'<tr><td colspan="{max(1, len(cols))}" class="dim">the file is not built yet: these are the '
                        'columns the code declares</td></tr>')
            notes = "".join(f'<p class="hint">{esc(n)}</p>' for n in s.get("notes") or ())
            blocks.append(
                f'<section class="dsec" data-sample="{esc(t)}"><h4><code>{esc(t)}, as served</code> <span class="dim">'
                f'{len(s.get("rows") or [])} rows</span></h4><div class="dsact"><button type="button" class="btn" '
                f'data-sample-refresh="{esc(t)}">Refresh the example rows</button><span class="dim">reads 5 rows of '
                f'<code>{esc(f["name"])}</code> · no provider call</span></div><div class="scroll"><table class="ex">'
                f'<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{notes}</section>')
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
            'under its dataset in the Fetch section.</p></details>')


def _data(view: dict) -> str:
    cards = []
    for f in view["files"]:
        pills = "".join(f'<span class="pill {esc(c)}">{esc(t)}</span>' for c, t in f["health"]["pills"])
        targets = "".join(
            f'<div class="tgt"><div class="tgth"><span class="dsname">{esc(d["dataset"])}</span><span class="dslabel">'
            f'{esc(d["label"])}</span><span class="pill {esc(d["pill"])}">{esc(d["status_words"])}</span></div>'
            + _meter("cov", d["coverage"]["cls"], "coverage", d["coverage"]["share"], d["coverage"]["text"],
                     d["coverage"]["target"]) + '</div>'
            for d in f["datasets"] if d["coverage"])
        tgt = (f'<div class="cbars"><p class="tgtnote">How full each dataset\'s target is · Fetch, Fill and Backfill '
               f'are in the Fetch section at the top</p>{targets}</div>' if targets else "")
        cards.append(f'<details class="group" style="--fh:{esc(f["color"])}" data-file="{esc(f["name"])}"><summary>'
                     f'<div class="ghead"><h3>{esc(f["name"])}</h3><span class="gstep">{esc(f["steps"])}</span>{pills}'
                     f'</div></summary>{tgt}{_journey(f)}{_samples(f)}{_health_table(f)}</details>')
    return ('<details class="sec" id="sec-data"><summary><h2>Data, file by file · in build order</h2>'
            f'<span class="secs" id="datasum">{len(view["files"])} files · how full each dataset is, the journey of '
            'each step, who reads which table · the flow diagram filters this list</span></summary>'
            '<div class="allctl"><button type="button" class="btn" id="openall">Open every file</button>'
            '<button type="button" class="btn" id="shutall">Close every file</button><span class="dim" id="cardsum">'
            'every file is closed when the page loads · the flow diagram above filters this list</span></div>'
            '<div class="emptyst" id="dataempty" hidden><b>No file matches the diagram\'s picks.</b> <button '
            'type="button" class="lnk" data-clearpicks>Clear the picks</button></div>'
            '<div class="group" style="border-left-color:var(--mut)"><p class="hint">Each card\'s table is the build '
            'order: one line per step, with its status · <span class="pill p-ok">built</span> ran against real data · '
            '<span class="pill p-warn">tested</span> the code and tests exist, no run on record yet · <span class="pill '
            'p-src">hand-run</span> needs a login on the server.</p></div>'
            f'{"".join(cards)}</details>')


# ── Cross-check, queue ────────────────────────────────────────────────────────────────────────────────────────

def _cross(view: dict) -> str:
    rows = []
    for c in view["cross_check"]:
        rows.append(f'<tr><td data-label="Field">{esc(c.get("field"))} <code>{esc(c.get("need") or "")}</code></td>'
                    f'<td data-label="Second source">{esc(c.get("second_source") or "no second source")}</td>'
                    f'<td data-label="Compared">{esc(c.get("checked") or "—")}</td>'
                    f'<td data-label="Differs">{esc(c.get("differs") if c.get("differs") is not None else "—")}</td>'
                    f'<td data-label="Largest deviation">{esc(c.get("worst") or "—")}</td>'
                    f'<td data-label="When">{esc(c.get("as_of") or c.get("why") or "none yet")}</td></tr>')
        if c.get("command"):
            rows.append(f'<tr><td colspan="6" class="dim">hand-run: <code>{esc(c["command"])}</code></td></tr>')
    body = "".join(rows) or '<tr><td colspan="6" class="dim">no cross-check recorded</td></tr>'
    return ('<details class="sec" id="sec-crosscheck"><summary><h2>Cross-check · first ↔ second source</h2>'
            f'<span class="secs">{len(view["cross_check"])} reports</span></summary>'
            '<div class="group" style="border-left-color:var(--new)"><div class="scroll"><table class="stk">'
            '<thead><tr><th>Field</th><th>Second source</th><th>Compared</th><th>Differs</th><th>Largest deviation</th>'
            f'<th>When</th></tr></thead><tbody>{body}</tbody></table></div><p class="hint">Where a field has no second '
            'source the row says so; no source is invented. The report shown is the one that exists: this page makes '
            'no new call.</p></div></details>')


def _queue(view: dict) -> str:
    q = view["queue"]
    rows = []
    for r in list(q.get("active") or []) + list(q.get("finished") or [])[:10]:
        rows.append(f'<tr><td data-label="Dataset"><code>{esc(r.get("dataset"))}</code></td>'
                    f'<td data-label="Status">{esc(r.get("status"))}</td>'
                    f'<td data-label="Asked by">{esc(r.get("requested_by") or "—")}</td>'
                    f'<td data-label="Asked">{esc(control._clock(r.get("requested_at")) if r.get("requested_at") else "—")}'
                    f'</td><td data-label="Note">{esc(r.get("note") or "—")}</td></tr>')
    body = "".join(rows) or '<tr><td colspan="5" class="dim">nothing queued, nothing run yet</td></tr>'
    waiting = len(q.get("active") or [])
    return ('<details class="sec" id="sec-queue"><summary><h2>Queue and last runs</h2>'
            f'<span class="secs">{waiting} waiting or running · the last finished requests</span></summary>'
            '<div class="group" style="border-left-color:var(--mut)"><div class="scroll"><table class="stk" '
            'id="ctl-queue"><thead><tr><th>Dataset</th><th>Status</th><th>Asked by</th><th>Asked</th><th>Note</th></tr>'
            f'</thead><tbody>{body}</tbody></table></div><p class="hint">A queued request cannot be cancelled: the '
            'builder runs it or skips it at its next pass.</p></div></details>')


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
                         "bits": {"A": 1, "E": 2, "R": 4}, "inc_bits": {"anchors": 1, "watchlist": 2,
                                                                        "cover_classes": 4}},
            "calendars": cals, "today": view["read_at_iso"][:10]}


def render(view: dict) -> str:
    """The ``<section id="ctl">`` of the page, or one line when the model could not be built."""
    if view.get("error"):
        return ('<section id="ctl"><div class="wrap"><h1>Settings · Data</h1><div class="errbar"><b>Could not read</b> '
                f'· {esc(view["error"])}</div></div></section>')
    view = {**view, "universe_summary": " + ".join(s["label"] for s in view["universe"]["sets"] if s["on"])
            + f' · {control.count(view["universe"]["size"])} names'}
    return ('<section id="ctl" aria-label="Settings · Data control board"><div class="wrap">'
            f'{_header(view)}{_health(view)}{_fetch(view)}{_flow(view)}{_providers(view)}{_data(view)}'
            f'{_cross(view)}{_queue(view)}</div></section>')


def scripts(view: dict) -> str:
    """The page's data and behaviour, one inline block (after ``ESC_JS``, which defines ``rcEsc``)."""
    if view.get("error"):
        return ""
    return (f"window.RC_FLOW = {script_json(view['flow'])};\nwindow.CTL = {script_json(client(view))};\n{CTL_JS}")
