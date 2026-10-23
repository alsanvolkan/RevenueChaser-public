"""Fundamentals board -> result history (#278 G1, third writer after AG #332 and Swing).

``record`` is a step of a Fundamentals Fetch (``app/runners.run_fundamentals``), after the latest tables
(``fundamentals_scores``, ``fundamentals_gates``) are written and before the freeze. It hands
``core/history.record_run`` the same rows the latest tables got (``fundamentals_scores_history`` and
``fundamentals_gates_history`` are their mirrors; ``scores.persist`` passes them through its ``sink``), so the two
cannot drift. The mapping lives here, in the module, not in ``core/``. A Refresh never calls it (06 rule 9). The
header also carries the board payload (compressed): a Refresh shows it instead of re-scoring (K4, #383 R1,
``core/screen_common.stored_payload``).

Fundamentals is a session board: the first complete run of a session keeps its rows; a later, intraday or partial run
keeps its header only (``core/history.record_run``). A run is partial when it scored nothing, the universe pull was
partial, SPY's bars were stale, any scored ticker rests on an ``edgar_financials`` row that is not at the current
``sec_edgar.EXTRACT_VERSION``, or, for a run that belongs to a complete session, the freeze's own bar anchor refuses
it (``core.ledger.spy_anchor_problem`` and ``ohlc_gaps``). Such a run must not become the session's permanent snapshot.

Why the EDGAR version counts: extract_version 2 (#158) derives discrete quarters from YTD facts; a row without it
(or with 1) feeds TTM growth, FCF margin and Rule of 40 from a different quarter basis. One snapshot must not mix two
bases, so a Fetch run while any scored ticker is on an older row keeps its header only, and the header's context says
how many tickers were on each version.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from RevenueChaser.core import calendar, history, ledger
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_fundamentals import freeze

BOARD = "module_fundamentals"
SCORES, GATES = "fundamentals_scores_history", "fundamentals_gates_history"
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


def _edgar_problem(versions: dict) -> str | None:
    """The scored tickers whose EDGAR row is not at the current extract_version (None when all are)."""
    total = sum(versions.values())
    older = total - versions.get(str(sec_edgar.EXTRACT_VERSION), 0)
    if not older:
        return None
    return (f"{older:,} of {total:,} scored tickers rest on an EDGAR row older than extract_version "
            f"{sec_edgar.EXTRACT_VERSION} (their quarters are not the discrete-quarter basis)")


def _partial(payload: dict, rows: dict, run_at: float) -> str | None:
    """Why this run is not a whole, correct session snapshot (None when it is)."""
    if payload.get("is_fixture"):
        return "the payload is fixture data"
    if payload.get("no_data"):
        return payload.get("no_data_message") or "the run reported no data"
    if not rows.get("scores"):
        return "no ticker was scored"
    if payload.get("universe_partial"):
        return "the universe pull was partial"
    if (payload.get("data_quality") or {}).get("spy_stale"):
        return "SPY's bars were stale, so the board's baseline is stale"
    why = _edgar_problem(rows.get("edgar_versions") or {})
    if why:
        return why
    try:
        stamp = calendar.session_for(run_at)
    except ValueError:
        return None                                        # record_run says "no session" itself
    if stamp.quality != calendar.QUALITY_COMPLETE:
        return None                                        # an intraday run stays 'intraday'
    return _bar_problem(payload, stamp.session_date)


def record(payload: dict, *, rows: dict, run_id: str):
    """Record a finished Fundamentals scan: a header for every Fetch, the rows when it scored tickers and is whole.

    ``rows`` is the sink ``scores.persist`` filled (``scores``, ``gates``, ``edgar_versions``); empty when nothing was
    persisted. Returns rows written per table."""
    run_at = _run_at(payload)
    summary = payload.get("summary") or {}
    return history.record_run(
        BOARD, run_id=run_id, run_at=run_at, model_version=freeze.model_version(),
        rows={SCORES: rows.get("scores") or [], GATES: rows.get("gates") or []}, params=freeze.effective_config(),
        context={"generated_at": payload.get("generated_at"), "data_as_of": payload.get("data_as_of"),
                 "regime": payload.get("regime"), "universe_total": payload.get("universe_total"),
                 "universe_generated_at": payload.get("universe_generated_at"),
                 "universe_refreshed_by": payload.get("universe_refreshed_by"), "passing": payload.get("passing"),
                 "tiers": payload.get("tiers"), "distorted_count": payload.get("distorted_count"),
                 "data_quality": payload.get("data_quality"), "edgar_extract_version": sec_edgar.EXTRACT_VERSION,
                 "edgar_versions": rows.get("edgar_versions"), "summary": summary},
        payload=payload, partial=_partial(payload, rows, run_at),
        provider_health=payload.get("provider_health"))
