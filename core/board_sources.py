"""Which build of which source DuckDB a board run read, and whether the board is behind its sources now (#566).

avo, 9 Nov 2026: "Ben dashboardlarin en guncel duckdb yi okudunu ve ona gore kendi datalarini olusturup kendi
duckdb lerine yazdigini nasil bilebilirim". Every builder file (``prices.duckdb``, ``fundamentals.duckdb``, ...)
stamps its build in ``serving_schema_version`` (``rcdata/serving.py:_stamp``); a board run is recorded in
``history.duckdb`` ``result_runs`` (``core/history.record_run``). This module joins the two:

- :func:`snapshot` is what ``record_run`` keeps in the run's ``context["source_builds"]``: every builder file the
  run's job **actually opened** (``rcdata/opens.py``, noted by ``serving.open_read``), with the build it got, and
  every builder file the board declares (``READS``, else ``PLANNED_READS``) but did not open, marked ``not_opened``
  (with the old-store reason where ``source_state`` says the boards fell back). A file opened at two builds in one
  run (a swap landed mid-run) is ``rebuilt_during_run``. A declaration alone never claims a read (review of #568,
  blocker 1).
- :func:`freshness` compares each board's newest recorded run with the builds that are live now: ``current``,
  ``behind`` (a newer build is live, or the run read two builds), ``rolled_back`` (the live build is older than the
  one read), ``missing`` (the file is gone), ``old_store``, ``error``, ``not_opened`` (the run opened no builder
  file) or ``not_recorded`` (a run recorded before this module existed). The newest day of each live file is read
  here, once per build, not on every run (review of #568, finding 5).

Nothing here may fail a run: an error is one entry for its own file (review finding 4).
"""
from __future__ import annotations

import importlib
import logging
import threading

import duckdb

logger = logging.getLogger(__name__)

#: The key of the run's ``context`` this record lives under.
CONTEXT_KEY = "source_builds"

#: A board recorded under a name that is not its package (``module_sector_rotation`` records two boards).
ALIASES = {"module_money_flow": "module_sector_rotation"}

#: The builder files whose readers can fall back to the old store, and the module that says which one they read.
_SOURCE_STATE = {"prices": "RevenueChaser.core.prices", "fundamentals": "RevenueChaser.core.fundamentals_layer",
                 "spine": "RevenueChaser.core.universe"}

CURRENT, BEHIND, ROLLED_BACK, MISSING, ERROR = "current", "behind", "rolled_back", "missing", "error"
OLD_STORE, NOT_OPENED, NOT_RECORDED = "old_store", "not_opened", "not_recorded"
#: Worst first: a board's state is the worst of its sources'. ``not_opened`` counts only when nothing was opened.
_RANK = (BEHIND, ERROR, MISSING, ROLLED_BACK, OLD_STORE, NOT_RECORDED, CURRENT, NOT_OPENED)

#: The need whose newest day stands for each file on the card ("last bar" for prices).
DAY_NEED = {"prices": "prices.bars", "fundamentals": "fundamentals.filings", "ownership": "ownership.holders_13f",
            "options": "options.daily", "spine": "identity.universe", "market": "market.series"}


def board_needs(board: str) -> tuple[str, ...]:
    """The needs the board declares: its own ``READS``, else its ``PLANNED_READS``, else none."""
    from ..rcdata.needs import boards
    pkg = ALIASES.get(board, board)
    try:
        declared = boards.declared_reads(pkg)
    except OSError:                                   # not a board package (a test board, a retired name)
        declared = None
    return tuple(declared) if declared is not None else tuple(boards.PLANNED_READS.get(pkg, ()))


def board_kinds(board: str) -> list[str]:
    """The builder files the board declares it reads, in the order its needs name them."""
    from ..rcdata.needs import catalog
    out: list[str] = []
    for name in board_needs(board):
        need = catalog.NEEDS.get(name)
        if need is None:
            continue
        try:
            kind = need.kind
        except KeyError:                              # a table no builder file serves yet
            continue
        if kind not in out:
            out.append(kind)
    return out


def _state(kind: str) -> dict | None:
    """The ``source_state`` of ``kind``'s readers, or None for a file that has no old-store fallback."""
    mod = _SOURCE_STATE.get(kind)
    if mod is None:
        return None
    return importlib.import_module(mod).source_state()


def _order(kind: str) -> int:
    from ..rcdata import serving
    return serving.KINDS.index(kind) if kind in serving.KINDS else len(serving.KINDS)


def snapshot(board: str, run_at: float | None = None) -> list[dict]:
    """One entry per builder file the run opened or the board declares (see the module docstring). Reading it
    forgets this thread's opens, so the next run on the thread starts empty."""
    from ..rcdata import opens
    seen = opens.take()
    try:
        declared = board_kinds(board)
    except Exception as exc:  # noqa: BLE001 - the opened files are still recorded
        declared = []
        logger.warning("declared reads of %s not read: %s", board, exc)
    out = []
    for kind in sorted(set(seen) | set(declared), key=_order):
        entry: dict = {"kind": kind, "file": f"{kind}.duckdb"}
        builds = seen.get(kind) or {}
        if builds:
            newest = max(builds.items(), key=lambda kv: kv[1] or 0)
            entry.update(reads="file", build_id=newest[0], built_at=newest[1])
            if len(builds) > 1:
                entry.update(rebuilt_during_run=True, build_ids=sorted(str(b) for b in builds))
        else:
            entry["reads"] = NOT_OPENED
            try:
                state = _state(kind)
            except Exception as exc:  # noqa: BLE001 - one file's error is that file's entry
                entry["error"] = f"{type(exc).__name__}: {exc}"[:200]
                state = None
            if state and state.get("effective") == "store":
                entry["old_store"] = (state.get("reason")
                                      or (f"set to {state.get('variable')}" if state.get("variable") else "set"))
        out.append(entry)
    return out


def safe_snapshot(board: str, run_at: float | None = None) -> list[dict]:
    """:func:`snapshot`, or one entry naming the error: recording the sources never fails a run."""
    try:
        return snapshot(board, run_at)
    except Exception as exc:  # noqa: BLE001 - a run is kept whatever this record could not read
        logger.warning("source builds of %s not recorded: %s", board, exc)
        return [{"error": f"{type(exc).__name__}: {exc}"[:200]}]


def with_sources(board: str, context: dict | None, run_at: float | None) -> dict:
    """``context`` with ``source_builds`` added; a board that set the key itself keeps its own."""
    ctx = dict(context or {})
    if CONTEXT_KEY not in ctx:
        ctx[CONTEXT_KEY] = safe_snapshot(board, run_at)
    return ctx


# ── the comparison the Data/Health card shows ─────────────────────────────────────────────────────────────────────

_DAYS: dict[tuple, str | None] = {}
_DAYS_LOCK = threading.Lock()


def newest_day(kind: str, build_id) -> str | None:
    """The newest day of ``kind``'s :data:`DAY_NEED` in the live file, ``YYYY-MM-DD``; read once per build. Not an
    ``open_read``: the card looking at a file is not a board reading it."""
    from ..rcdata import serving
    from ..rcdata.needs import catalog
    key = (kind, build_id)
    with _DAYS_LOCK:
        if key in _DAYS:
            return _DAYS[key]
    need = catalog.NEEDS.get(DAY_NEED.get(kind, ""))
    day = None
    if need is not None and need.day_column and serving.live_path(kind).exists():
        try:
            with duckdb.connect(str(serving.live_path(kind)), read_only=True) as con:
                row = con.execute(f"SELECT max({need.day_column}) FROM {need.table}").fetchone()
            day = str(row[0])[:10] if row and row[0] is not None else None
        except duckdb.Error:
            day = None
    with _DAYS_LOCK:
        if len(_DAYS) > 64:
            _DAYS.clear()
        _DAYS[key] = day
    return day


def _source_state(recorded: dict, live: dict | None) -> str:
    if recorded.get("error"):
        return ERROR
    if recorded.get("reads") == NOT_OPENED:
        return OLD_STORE if recorded.get("old_store") else NOT_OPENED
    if recorded.get("rebuilt_during_run"):
        return BEHIND
    if live is None:
        return MISSING
    if live.get("build_id") == recorded.get("build_id"):
        return CURRENT
    return BEHIND if (live.get("built_at") or 0) >= (recorded.get("built_at") or 0) else ROLLED_BACK


def worst(states) -> str:
    states = list(states)
    return next((s for s in _RANK if s in states), NOT_OPENED)


def freshness(runs: list[dict], live_stamp=None, day=None) -> list[dict]:
    """Per board run header (``history.last_runs``): ``{board, run_id, run_at, quality, state, sources}``, where each
    source carries what the run recorded, the build live now (``live_built_at``, ``live_build_id``), the live file's
    ``newest_day`` and its ``state``. ``live_stamp`` / ``day`` are ``serving.live_stamp`` / :func:`newest_day`
    unless a caller passes its own (tests)."""
    if live_stamp is None:
        from ..rcdata import serving
        live_stamp = serving.live_stamp
    day = day or newest_day
    cache: dict[str, dict | None] = {}

    def live(kind):
        if kind not in cache:
            try:
                cache[kind] = live_stamp(kind)
            except Exception:  # noqa: BLE001 - one unreadable file is one source shown missing
                cache[kind] = None
        return cache[kind]

    out = []
    for run in runs:
        recorded = (run.get("context") or {}).get(CONTEXT_KEY)
        row = {"board": run.get("board"), "run_id": run.get("run_id"), "run_at": run.get("run_at"),
               "quality": run.get("quality")}
        if not isinstance(recorded, list):
            out.append({**row, "state": NOT_RECORDED, "sources": []})
            continue
        sources = []
        for rec in recorded:
            if not isinstance(rec, dict):
                continue
            now = live(rec["kind"]) if rec.get("kind") else None
            src = {**rec, "live_build_id": (now or {}).get("build_id"),
                   "live_built_at": (now or {}).get("built_at"), "state": _source_state(rec, now)}
            if now is not None and rec.get("kind"):
                try:
                    src["newest_day"] = day(rec["kind"], now.get("build_id"))
                except Exception:  # noqa: BLE001
                    src["newest_day"] = None
            sources.append(src)
        out.append({**row, "state": worst(s["state"] for s in sources), "sources": sources})
    return out
