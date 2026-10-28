"""Swing board -> result history (#278 G1, second writer after AG #332).

``record`` is a step of a Swing Fetch (``app/runners.run_swing``), after the latest tables (``swing_scores``,
``swing_gates``) are written and before the freeze. It hands ``core/history.record_run`` the same rows the latest
tables got (``swing_scores_history`` and ``swing_gates_history`` are their mirrors; ``scores.persist`` passes them
through its ``sink``), so the two cannot drift. The mapping lives here, in the module, not in ``core/``. A Refresh
never calls it (06 rule 9). The header also carries the board payload (compressed): a Refresh shows it instead of
re-scoring (K4, #383 R1, ``core/screen_common.stored_payload``).

Swing is a session board: the first complete run of a session keeps its rows; a later, intraday or partial run keeps
its header only (``core/history.record_run``). A run is partial when it scored nothing, the universe pull was partial,
SPY's bars were stale, or, for a run that belongs to a complete session, the freeze's own bar anchor refuses it:
neither D01 nor the cache has SPY's complete bar of that session, or a shown candidate's bars were cached before the
close + 30 min (``core.ledger.spy_anchor_problem`` and ``ohlc_gaps``). Such a run must not become the session's permanent
snapshot.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from RevenueChaser.core import calendar, history, ledger
from RevenueChaser.module_swing_potential import freeze

BOARD = "module_swing_potential"
SCORES, GATES = "swing_scores_history", "swing_gates_history"
_LIST_CAP = 10


def _run_at(payload: dict) -> float:
    try:
        when = datetime.fromisoformat(str(payload.get("generated_at")))
    except ValueError:
        return time.time()
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def _bar_problem(payload: dict, session) -> str | None:
    why = ledger.spy_anchor_problem(session)
    if why:
        return why
    shown = [c.get("ticker") for c in payload.get("candidates") or []]
    gaps = ledger.ohlc_gaps(session, shown)
    if not gaps:
        return None
    names = sorted(gaps)
    listed = ", ".join(names[:_LIST_CAP]) + (f" +{len(names) - _LIST_CAP} more" if len(names) > _LIST_CAP else "")
    return f"{len(names):,} of {len(shown):,} shown picks lack a complete {session} daily bar: {listed}"


def _partial(payload: dict, rows: dict, run_at: float) -> str | None:
    """Why this run is not a whole, correct session snapshot (None when it is)."""
    if payload.get("no_data"):
        return payload.get("no_data_message") or "the run reported no data"
    if not rows.get("scores"):
        return "no ticker was scored"
    if payload.get("universe_partial"):
        return "the universe pull was partial"
    if (payload.get("data_quality") or {}).get("spy_stale"):
        return "SPY's bars were stale, so the board's baseline is stale"
    try:
        stamp = calendar.session_for(run_at)
    except ValueError:
        return None                                        # record_run says "no session" itself
    if stamp.quality != calendar.QUALITY_COMPLETE:
        return None                                        # an intraday run stays 'intraday'
    return _bar_problem(payload, stamp.session_date)


def record(payload: dict, *, rows: dict, run_id: str):
    """Record a finished Swing scan: a header for every Fetch, the rows when it scored tickers and is a whole run.

    ``rows`` is the sink ``scores.persist`` filled (``scores`` and ``gates``); empty when nothing was persisted.
    Returns rows written per table."""
    run_at = _run_at(payload)
    summary = payload.get("summary") or {}
    return history.record_run(
        BOARD, run_id=run_id, run_at=run_at, model_version=freeze.model_version(payload),
        rows={SCORES: rows.get("scores") or [], GATES: rows.get("gates") or []},
        params=freeze.effective_config(payload),
        context={"generated_at": payload.get("generated_at"), "data_as_of": payload.get("data_as_of"),
                 "regime": payload.get("regime"), "universe_total": payload.get("universe_total"),
                 "universe_generated_at": payload.get("universe_generated_at"),
                 "universe_refreshed_by": payload.get("universe_refreshed_by"), "passing": payload.get("passing"),
                 "avg_quality": payload.get("avg_quality"), "data_quality": payload.get("data_quality"),
                 "summary": summary},
        payload=payload, partial=_partial(payload, rows, run_at),
        provider_health=payload.get("provider_health"))
