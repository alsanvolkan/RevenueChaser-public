"""The Settings / Data health page's second tab, **Needs** (#331): the need catalog and the request log.

``build()`` reads the model: one row per need from ``rcdata.needs.catalog`` with who reads it (the board's own
``READS``, or its planned reads, drawn apart) and its state now (``needs.summary``), plus the read log's
``need:<name>`` rows from ``ops.need_reads``, summed over today, 7 and 30 days, by process. ``render(model)`` draws
it: every string through ``html_safe.esc``; the styles are ``static/needs.css``, scoped to ``#needs``.

Two failure rules. A summary that raises for one need is that row's ``unreadable`` with its error, never a failed
tab; a log that cannot be read is one line in the log section. The tab's own summaries are not counted in the log
(``summary(count=False)``), or every render would add one read per need under ``dashboard`` and bury the requests
the tab exists to show.

Read-only: no provider, no write, no queue. ``ops.need_reads`` never creates ``ops.sqlite``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from RevenueChaser.core.html_safe import esc

_STATIC = Path(__file__).resolve().parent / "static"
NEEDS_CSS = (_STATIC / "needs.css").read_text(encoding="utf-8")

#: The log's windows: (key, label, days counted back from today, today included).
WINDOWS = (("today", "Today", 1), ("d7", "7 days", 7), ("d30", "30 days", 30))
LOG_DAYS = max(days for _, _, days in WINDOWS)
UNREADABLE = "unreadable"
STATES = ("fresh", "stale", "missing", UNREADABLE)
BOARD_PREFIX = "module_"
#: The served duckdb files, in the order the catalog groups its needs by (#492). A need's file is its table's
#: serving kind (``catalog.Need.kind``, ``rcdata.read.KIND_OF_TABLE``); a need of no served file goes last.
SERVED_FILES = ("spine", "prices", "fundamentals", "ownership", "options", "market")
OTHER_FILE = "other"

def _summary(name: str) -> dict:
    from RevenueChaser.rcdata import needs
    return needs.summary(name, count=False)


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value)


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


def _readers() -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]], str | None]:
    """``(declared, planned, error)``: each board's own ``READS`` and, for a board without them, its planned reads."""
    from RevenueChaser.rcdata.needs import boards
    try:
        declared = boards.all_reads()
    except Exception as exc:  # noqa: BLE001 - an unreadable board package is a line, never a failed tab
        return {}, {}, _error(exc)
    planned = {b: tuple(r) for b, r in boards.PLANNED_READS.items() if b not in declared}
    return declared, planned, None


def _board_name(package: str) -> str:
    return package[len(BOARD_PREFIX):] if package.startswith(BOARD_PREFIX) else package


def _catalog_rows(summarize: Callable[[str], dict]) -> tuple[list[dict], str | None]:
    from RevenueChaser.rcdata.needs import catalog
    declared, planned, readers_error = _readers()
    rows = []
    for name, need in sorted(catalog.NEEDS.items()):
        row = {"need": name, "kind": need.kind, "table": need.table, "dataset": need.dataset,
               "day_column": need.day_column, "max_lag": need.max_lag, "unit": need.max_lag_unit,
               "ask_param": need.ask_param, "ask_required": need.ask_required, "providers": list(need.providers),
               "legacy": need.legacy,
               "declared": [_board_name(b) for b, r in sorted(declared.items()) if name in r],
               "planned": [_board_name(b) for b, r in sorted(planned.items()) if name in r],
               "state": UNREADABLE, "newest_day": None, "lag": None, "rows": None, "keys": None, "status": None,
               "error": None}
        try:
            s = summarize(name)
            fresh = s.get("freshness") or {}
            row.update(state=s["state"], newest_day=_iso(fresh.get("as_of_session")), lag=fresh.get("lag_sessions"),
                       rows=s.get("row_count"), keys=s.get("key_count"), status=s.get("status"))
        except Exception as exc:  # noqa: BLE001 - one need's failed summary is that row's word, never a failed tab
            row["error"] = _error(exc)
        rows.append(row)
    return rows, readers_error


def _log(today: date, catalog_names: list[str], read: Callable[[str], list[dict]]) -> dict:
    first = {key: (today - timedelta(days=days - 1)).isoformat() for key, _, days in WINDOWS}
    out = {"first_day": first["d30"], "today": today.isoformat(), "rows": [], "processes": [],
           "never_asked": [], "error": None}
    try:
        raw = read(first["d30"])
    except Exception as exc:  # noqa: BLE001 - an unreadable ops file is one line in the section
        out["error"] = _error(exc)
        out["never_asked"] = list(catalog_names)
        return out
    known = set(catalog_names)
    per: dict[str, dict] = {}
    processes: set[str] = set()
    for r in raw:
        name, day, n, proc = str(r["need"]), str(r["day"]), int(r["reads"] or 0), str(r["process"])
        if day > out["today"]:
            continue            # a clock that ran ahead on another machine: not a read of "today"
        row = per.setdefault(name, {"need": name, "in_catalog": name in known, "today": 0, "d7": 0, "d30": 0,
                                    "by_process": {}})
        for key, _, _ in WINDOWS:
            if day >= first[key]:
                row[key] += n
        row["by_process"][proc] = row["by_process"].get(proc, 0) + n
        processes.add(proc)
    for row in per.values():
        # A list, not a map: a process name is a string the page shows, and a map's keys are not its values.
        row["by_process"] = [{"process": p, "reads": n}
                             for p, n in sorted(row["by_process"].items(), key=lambda kv: (-kv[1], kv[0]))]
    out["rows"] = sorted((r for r in per.values() if r["d30"] > 0), key=lambda r: (-r["d30"], -r["d7"], r["need"]))
    out["processes"] = sorted(processes)
    asked = {r["need"] for r in out["rows"]}
    out["never_asked"] = [n for n in catalog_names if n not in asked]
    return out


def build(*, today: date | None = None, summarize: Callable[[str], dict] | None = None,
          read_log: Callable[[str], list[dict]] | None = None) -> dict:
    """The tab's model. ``today`` is the New York day the log's windows end on; the two readers are seams for tests."""
    from RevenueChaser.core import calendar
    from RevenueChaser.rcdata import ops
    today = today or calendar.today_ny()
    catalog_rows, readers_error = _catalog_rows(summarize or _summary)
    counts = {s: sum(1 for r in catalog_rows if r["state"] == s) for s in STATES}
    return {"read_at": datetime.now().strftime("%H:%M"), "catalog": catalog_rows, "counts": counts,
            "readers_error": readers_error,
            "log": _log(today, [r["need"] for r in catalog_rows], read_log or ops.need_reads)}


# ── Render ──────────────────────────────────────────────────────────────────────────────────────────────────────

WIAL = ("<p>Every board asks the store for a <b>need</b> by name, never for a table: <code>rcdata.needs</code> "
        "answers from the serving file the catalog names and says how fresh the answer is. This tab shows the "
        "catalog and how often each need was asked.</p>"
        "<p><b>State</b> is read now, once per render: <b>fresh</b> when the newest day is inside the need's window, "
        "<b>stale</b> when it is older, <b>missing</b> when the store holds no row, <b>unreadable</b> when the read "
        "failed (that row's error is shown; the other rows are unaffected). <b>Read by</b>: a solid chip is the "
        "board's own <code>READS</code>, a dashed chip a planned read the board has not declared yet.</p>"
        "<p>The <b>request log</b> is <code>ops.sqlite</code>'s <code>read_log</code>, which counts "
        "<code>need:&lt;name&gt;</code> per process and per day; a process writes its counts at most once a minute "
        "and at exit, so the newest minute may not be in yet. This tab's own reads are not counted. Nothing here "
        "fetches, fills or writes.</p>")


def _num(value) -> str:
    return "—" if value is None else f"{int(value):,}"


def _fresh_rule(row: dict) -> str:
    if row["day_column"] is None:
        return "no date: present or absent"
    unit = "session" if row["unit"] == "sessions" else "day"
    return f"≤ {row['max_lag']} {unit}{'' if row['max_lag'] == 1 else 's'}"


def _chips(row: dict) -> str:
    own = "".join(f'<span class="chip own" title="declared READS">{esc(b)}</span>' for b in row["declared"])
    plan = "".join(f'<span class="chip plan" title="planned, not declared yet">{esc(b)}</span>' for b in row["planned"])
    return (own + plan) or '<span class="dim">no board yet</span>'


def _need_name(name) -> str:
    """The need's name, allowed to break after a dot and nowhere else (escaped first, then the break inserted)."""
    return esc(name).replace(".", ".<wbr>")


def _lag(row: dict) -> str:
    unit = "session" if row["unit"] == "sessions" else "day"
    return f"{row['lag']} {unit}{'' if row['lag'] == 1 else 's'} behind"


def _state_cell(row: dict) -> str:
    state = row["state"] if row["state"] in STATES else UNREADABLE
    lag = (f'<span class="dim"> · {esc(_lag(row))}</span>'
           if row["lag"] not in (None, 0) and state in ("fresh", "stale") else "")
    return f'<span class="st st-{esc(state)}">{esc(state)}</span>{lag}'


def _catalog_row(row: dict) -> str:
    asked = (f'<div class="dim">asked by <code>{esc(row["ask_param"])}</code>'
             f'{" (required)" if row["ask_required"] else ""}</div>' if row["ask_param"] else
             '<div class="dim">asked with no argument</div>')
    name = f'<td class="nm" data-label="Need"><code class="need">{_need_name(row["need"])}</code>{asked}</td>'
    state = f'<td class="nw" data-label="State">{_state_cell(row)}</td>'
    if row["error"]:
        return (f'<tr class="unreadable" data-need="{esc(row["need"])}">{name}{state}<td class="err" colspan="5" data-label="Error">'
                f'{esc(row["error"])}</td></tr>')
    keys = f'<div class="dim">{esc(_num(row["keys"]))} keys</div>' if row["keys"] is not None else ""
    served = (f'<code>{esc(row["kind"])}</code> · <code>{esc(row["table"])}</code>'
              f'<div class="dim">{esc(row["dataset"] or "several datasets")}</div>')
    return (f'<tr data-need="{esc(row["need"])}" data-state="{esc(row["state"])}">{name}{state}'
            f'<td class="d nw" data-label="Newest day">{esc(row["newest_day"] or "—")}</td>'
            f'<td class="n nw" data-label="Rows">{esc(_num(row["rows"]))}{keys}</td>'
            f'<td class="nw" data-label="Fresh if">{esc(_fresh_rule(row))}</td>'
            f'<td data-label="Served from">{served}</td>'
            f'<td data-label="Read by"><div class="chips">{_chips(row)}</div></td></tr>')


def _file_of(row: dict) -> str:
    return row["kind"] if row.get("kind") in SERVED_FILES else OTHER_FILE


def _catalog_groups(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """``(file, rows)`` per served file in ``SERVED_FILES`` order, then ``other``; a file with no need is left out."""
    order = SERVED_FILES + (OTHER_FILE,)
    return [(f, [r for r in rows if _file_of(r) == f]) for f in order if any(_file_of(r) == f for r in rows)]


def _catalog_group(file: str, rows: list[dict]) -> str:
    """One file's group: a full-width head row with its toggle, then its rows in their own ``tbody``. Problems
    first (Q47): a group with a need that is not fresh opens; an all-fresh group stays closed."""
    fresh = sum(1 for r in rows if r["state"] == "fresh")
    stale = sum(1 for r in rows if r["state"] == "stale")
    body_id = f"cg-{esc(file)}"
    open_ = fresh < len(rows)
    return (f'<tbody class="cghead" data-file="{esc(file)}"><tr><td colspan="7">'
            f'<button type="button" class="cgtog" aria-expanded="{"true" if open_ else "false"}" '
            f'aria-controls="{body_id}"><span class="cgarr" aria-hidden="true">{"▾" if open_ else "▸"}</span> '
            f'<span class="cgfile">{esc(file)}</span> '
            f'<span class="cgcount">{len(rows)} need{"" if len(rows) == 1 else "s"} · fresh {fresh} · '
            f'stale {stale}</span></button></td></tr></tbody>'
            f'<tbody class="cbody"{"" if open_ else " hidden"} id="{body_id}">{"".join(_catalog_row(r) for r in rows)}</tbody>')


#: The group toggle: a press opens or closes the group's rows and flips ``aria-expanded`` and the arrow.
NEEDS_JS = """
(function(){
  [].forEach.call(document.querySelectorAll('#needs-catalog .cgtog'), function (b) {
    b.addEventListener('click', function () {
      var body = document.getElementById(b.getAttribute('aria-controls'));
      if (!body) return;
      var open = b.getAttribute('aria-expanded') !== 'true';
      body.hidden = !open;
      b.setAttribute('aria-expanded', open ? 'true' : 'false');
      var arr = b.querySelector('.cgarr');
      if (arr) arr.textContent = open ? '▾' : '▸';
    });
  });
})();
"""


def _catalog_section(model: dict) -> str:
    c = model["counts"]
    head = " · ".join(f'<span class="st st-{esc(s)}">{c[s]} {esc(s)}</span>' if s != "fresh" else f'{c[s]} {s}'
                      for s in STATES if c.get(s))
    note = (f'<p class="warn">Who reads which need could not be read: {esc(model["readers_error"])}</p>'
            if model.get("readers_error") else "")
    body = "".join(_catalog_group(f, rows) for f, rows in _catalog_groups(model["catalog"]))
    return (f'<section class="part" id="needs-catalog" aria-labelledby="needs-catalog-h">'
            f'<h2 id="needs-catalog-h">The catalog <span class="secs">{len(model["catalog"])} needs · '
            f'{head}</span></h2>{note}'
            '<table class="stk"><thead><tr><th>Need</th><th class="nw">State</th><th class="nw">Newest day</th>'
            '<th class="nw">Rows</th><th class="nw">Fresh if</th><th>Served from</th><th>Read by</th></tr></thead>'
            f'{body}</table>'
            '<p class="legend"><span class="chip own">board</span> the board\'s own <code>READS</code> · '
            '<span class="chip plan">board</span> planned, not declared yet</p></section>')


def _by_process(by: list[dict]) -> str:
    return " · ".join(f'<span class="proc">{esc(p["process"])} <b>{esc(_num(p["reads"]))}</b></span>' for p in by)


def _log_row(row: dict) -> str:
    off = '' if row["in_catalog"] else ' <span class="dim">not in the catalog</span>'
    cells = "".join(f'<td class="n" data-label="{esc(label)}">{esc(_num(row[key]) if row[key] else "—")}</td>'
                    for key, label, _ in WINDOWS)
    return (f'<tr><td class="nm" data-label="Need"><code class="need">{_need_name(row["need"])}</code>{off}</td>{cells}'
            f'<td data-label="By process (30 days)">{_by_process(row["by_process"])}</td></tr>')


def _log_section(model: dict) -> str:
    log = model["log"]
    total = len(model["catalog"])
    asked = sum(1 for r in log["rows"] if r["in_catalog"])
    if log["error"]:
        body = f'<p class="warn">The read log could not be read: {esc(log["error"])}</p>'
    elif not log["rows"]:
        body = (f'<p class="empty">No need was asked between {esc(log["first_day"])} and {esc(log["today"])}: '
                'the log holds no <code>need:</code> row in that window.</p>')
    else:
        heads = "".join(f'<th class="n">{esc(label)}</th>' for _, label, _ in WINDOWS)
        body = ('<table class="stk"><thead><tr><th>Need</th>' + heads + '<th>By process (30 days)</th></tr></thead>'
                f'<tbody>{"".join(_log_row(r) for r in log["rows"])}</tbody></table>')
    never = log["never_asked"]
    never_html = (f'<details class="never"><summary>Never asked in 30 days: {len(never)}</summary><p>'
                  + ", ".join(f'<code>{esc(n)}</code>' for n in never) + '</p></details>') if never else ""
    return (f'<section class="part" id="needs-log" aria-labelledby="needs-log-h">'
            f'<h2 id="needs-log-h">The request log <span class="secs"><code>read_log</code> · '
            f'<code>need:&lt;name&gt;</code> by day · {asked} of {total} needs asked in 30 days</span></h2>'
            f'{body}{never_html}'
            '<p class="legend">The process is the name the reading process wrote: <code>RC_PROCESS_NAME</code>, '
            'else its script\'s name: the dashboard writes <code>dashboard</code>, the builder <code>builder</code>, '
            'and another <code>python -m</code> run its package\'s name (<code>rcdata</code> for the backup CLI). '
            'Rows from before 1 Nov 2026 may say <code>__main__</code>. Counted per New York day.</p></section>')


def head(model: dict, title: str = "Settings · Needs") -> str:
    """The tab's head line: the title, the read minute and "What am I looking at?" with its box."""
    return (f'<h1>{esc(title)}</h1>'
            f'<p class="sub"><b>Read {esc(model.get("read_at") or "—")}</b> · what a board can ask the store for, how '
            'fresh it is, and how often it is asked · <button type="button" class="wiali" aria-expanded="false" '
            'aria-controls="needs-wial">What am I looking at?</button></p>'
            f'<div class="wialbox" id="needs-wial" hidden>{WIAL}</div>')


def sections(model: dict) -> tuple[str, str]:
    """``(catalog, log)``: the tab's body in two parts, so a page can put its own part between them. The routes are
    documented in ``docs/data_health.md``, not drawn on the page (Q50).
    A model with ``error`` is one line, in the first part."""
    if model.get("error"):
        return f'<p class="warn">The Needs tab could not be read: {esc(model["error"])}</p>', ""
    return _catalog_section(model), _log_section(model)


def render(model: dict) -> str:
    """The tab's panel body (the ``#needs`` element's inside). A model with ``error`` is one line, never a failure."""
    catalog, rest = sections(model)
    return f'<div class="wrap">{head(model)}{catalog}{rest}</div>'


def view(payload: dict | None = None) -> dict:
    """The model the page draws: the payload's own ``needs`` when the caller read it (tests, fixtures), else read now.

    A failure of the whole read is a model with ``error``, drawn as one line.
    """
    payload = payload or {}
    if "needs" in payload:
        return payload["needs"]
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - the tab must never take the Settings page down
        return {"error": _error(exc), "read_at": datetime.now().strftime("%H:%M")}
