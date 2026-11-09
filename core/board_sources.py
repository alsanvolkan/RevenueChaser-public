"""Which build of which source DuckDB a board run read, and whether the board is behind its sources now (#566).

avo, 9 Nov 2026: "Ben dashboardlarin en guncel duckdb yi okudunu ve ona gore kendi datalarini olusturup kendi
duckdb lerine yazdigini nasil bilebilirim". Every builder file (``prices.duckdb``, ``fundamentals.duckdb``, ...)
stamps its build in ``serving_schema_version`` (``rcdata/serving.py:_stamp``); a board run is recorded in
``history.duckdb`` ``result_runs`` (``core/history.record_run``). This module joins the two:

- :func:`snapshot` is what ``record_run`` keeps in the run's ``context["source_builds"]``: for every builder file the
  board declares it reads (its ``READS``, else its ``PLANNED_READS``, ``rcdata/needs/boards.py``), the file, its
  live ``build_id`` and ``built_at``, the newest day of the board's first need in that file, and whether the boards
  read that file at all or fell back to the old store (the ``source_state`` of ``core/prices``,
  ``core/fundamentals_layer`` and ``core/universe``).
- :func:`freshness` compares each board's newest recorded run with the builds that are live now and names one state
  per source and per board: ``current``, ``behind`` (a newer build is live than the one the run read, or a build
  landed while the run was reading), ``old_store``, ``missing`` (no such file) or ``not_recorded`` (a run recorded
  before this module existed).

The record is taken when the run is recorded, not at each read. A file swapped after ``run_at`` (the moment the
board's result is dated) is flagged as ``rebuilt_during_run`` rather than claimed as read, so the record never says
more than it knows. Nothing here may fail a run: :func:`safe_snapshot` turns any error into one entry that says so.
"""
from __future__ import annotations

import importlib
import logging

import duckdb

logger = logging.getLogger(__name__)

#: The key of the run's ``context`` this record lives under.
CONTEXT_KEY = "source_builds"

#: A board recorded under a name that is not its package (``module_sector_rotation`` records two boards).
ALIASES = {"module_money_flow": "module_sector_rotation"}

#: The builder files whose readers can fall back to the old store, and the module that says which one they read.
_SOURCE_STATE = {"prices": "RevenueChaser.core.prices", "fundamentals": "RevenueChaser.core.fundamentals_layer",
                 "spine": "RevenueChaser.core.universe"}

CURRENT, BEHIND, OLD_STORE, MISSING, NOT_RECORDED = "current", "behind", "old_store", "missing", "not_recorded"
#: Worst first: a board's state is the worst of its sources'.
_RANK = (BEHIND, MISSING, OLD_STORE, NOT_RECORDED, CURRENT)


def board_needs(board: str) -> tuple[str, ...]:
    """The needs the board reads: its own ``READS``, else its ``PLANNED_READS``, else none."""
    from ..rcdata.needs import boards
    pkg = ALIASES.get(board, board)
    try:
        declared = boards.declared_reads(pkg)
    except OSError:                                   # not a board package (a test board, a retired name)
        declared = None
    return tuple(declared) if declared is not None else tuple(boards.PLANNED_READS.get(pkg, ()))


def board_kinds(board: str) -> list[tuple[str, object]]:
    """``(kind, need)`` per builder file the board reads, in the order its needs name them. ``need`` is the board's
    first need in that file that has a day column (the one whose newest day is shown), or None."""
    from ..rcdata.needs import catalog
    out: dict[str, object] = {}
    for name in board_needs(board):
        need = catalog.NEEDS.get(name)
        if need is None:
            continue
        try:
            kind = need.kind
        except KeyError:                              # a table no builder file serves yet
            continue
        if kind not in out or (out[kind] is None and need.day_column):
            out[kind] = need if need.day_column else out.get(kind)
    return list(out.items())


def _state(kind: str) -> dict | None:
    """The ``source_state`` of ``kind``'s readers, or None for a file that has no old-store fallback."""
    mod = _SOURCE_STATE.get(kind)
    if mod is None:
        return None
    return importlib.import_module(mod).source_state()


def _newest_day(need) -> str | None:
    """The newest day of ``need``'s table in the live file, ``YYYY-MM-DD``; None when it cannot be read."""
    from ..rcdata import serving
    where, params = "", []
    if need.filter_values:
        where = f" WHERE {need.summary_key} IN ({', '.join('?' for _ in need.filter_values)})"
        params = list(need.filter_values)
    elif need.filter_prefix:
        where, params = f" WHERE {need.summary_key} LIKE ?", [need.filter_prefix + "%"]
    try:
        with serving.open_read(need.kind) as con:
            row = con.execute(f"SELECT max({need.day_column}) FROM {need.table}{where}", params).fetchone()
    except (serving.ServingFileMissing, duckdb.Error):
        return None
    return str(row[0])[:10] if row and row[0] is not None else None


def snapshot(board: str, run_at: float | None) -> list[dict]:
    """One entry per builder file the board reads, as it stands now (see the module docstring)."""
    from ..rcdata import serving
    out = []
    for kind, need in board_kinds(board):
        entry: dict = {"kind": kind, "file": f"{kind}.duckdb"}
        state = _state(kind)
        if state and state.get("effective") == "store":
            entry.update(reads=OLD_STORE, reason=state.get("reason")
                         or (f"set to {state.get('variable')}" if state.get("variable") else None))
            out.append(entry)
            continue
        stamp = serving.live_stamp(kind)
        if stamp is None:
            entry["reads"] = MISSING
            out.append(entry)
            continue
        entry.update(reads="file", build_id=stamp["build_id"], built_at=stamp["built_at"],
                     build_mode=stamp.get("build_mode"))
        if need is not None:
            entry.update(newest_day=_newest_day(need), day_of=need.name)
        if run_at is not None and stamp["built_at"] is not None and stamp["built_at"] > float(run_at):
            entry["rebuilt_during_run"] = True
        out.append(entry)
    return out


def safe_snapshot(board: str, run_at: float | None) -> list[dict]:
    """:func:`snapshot`, or one entry naming the error: recording the sources never fails a run."""
    try:
        return snapshot(board, run_at)
    except Exception as exc:  # noqa: BLE001 - a run is kept whatever this record could not read
        logger.warning("source builds of %s not recorded: %s", board, exc)
        return [{"error": f"{type(exc).__name__}: {exc}"[:200]}]


def with_sources(board: str, context: dict | None, run_at: float | None) -> dict:
    """``context`` with ``source_builds`` added; a board that set the key itself keeps its own."""
    ctx = dict(context or {})
    ctx.setdefault(CONTEXT_KEY, safe_snapshot(board, run_at))
    return ctx


# ── the comparison the Data/Health card shows ─────────────────────────────────────────────────────────────────────

def _source_state(recorded: dict, live: dict | None) -> str:
    reads = recorded.get("reads")
    if reads == OLD_STORE:
        return OLD_STORE
    if reads == MISSING or recorded.get("error"):
        return MISSING
    if recorded.get("rebuilt_during_run"):
        return BEHIND
    if live is None:
        return MISSING
    if live.get("build_id") != recorded.get("build_id") and (live.get("built_at") or 0) > (recorded.get("built_at") or 0):
        return BEHIND
    return CURRENT


def worst(states) -> str:
    states = list(states)
    return next((s for s in _RANK if s in states), NOT_RECORDED)


def freshness(runs: list[dict], live_stamp=None) -> list[dict]:
    """Per board run header (``history.last_runs``): ``{board, run_id, run_at, quality, state, sources}``, where each
    source carries what the run recorded, the build live now (``live_built_at``, ``live_build_id``) and its
    ``state``. ``live_stamp`` is ``serving.live_stamp`` unless a caller passes its own (tests)."""
    if live_stamp is None:
        from ..rcdata import serving
        live_stamp = serving.live_stamp
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
            sources.append({**rec, "live_build_id": (now or {}).get("build_id"),
                            "live_built_at": (now or {}).get("built_at"), "state": _source_state(rec, now)})
        out.append({**row, "state": worst(s["state"] for s in sources) if sources else CURRENT,
                    "sources": sources})
    return out
