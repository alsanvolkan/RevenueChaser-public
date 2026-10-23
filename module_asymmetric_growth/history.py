"""Asymmetric Growth board -> result history (#332, #278 G1 first writer).

``record`` is a step of an AG Fetch (``app/runners.run_asymmetric_growth``), after the latest table
(``ticker_scores``) is written and before the freeze. It hands ``core/history.record_run`` the same rows the
latest table got (``ag_scores_history`` is its mirror), so the two cannot drift. The mapping lives here, in the
module, not in ``core/``. A Refresh never calls it (06 rule 9).

AG is a session board: the first complete run of a session keeps its rows; a later, intraday or partial run keeps
its header only (``core/history.record_run``). A run is partial when it has no cards, the universe pull was partial,
more than ``FREEZE_MAX_SCORE_ERROR_RATE`` of the tickers failed to score, or Daily Bars has not written the session's
SPY bar yet (the freeze's own anchor, #380 S4). The header's context also carries ``data_as_of``: the newest D01
session among the cards, so a Refresh says which price session it shows.

``recorded_sessions`` reads it back for the board (#380, core review slice A2: the first history reader), through
the shared reader ``core/history.sessions`` (K2, #383 R1) and the snapshot runs' ``ag_scores_history`` rows. It is
a read on Fetch and Refresh alike: no write, and no history file when none exists.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from RevenueChaser.core import calendar, history, rawstore, ticker_store
from RevenueChaser.module_asymmetric_growth import freeze

BOARD = "module_asymmetric_growth"
SESSIONS_SHOWN = 10      # sessions the board lists, newest first
TOP_N = 5                # tickers named per session, by points


def _run_at(result: dict) -> float:
    try:
        when = datetime.fromisoformat(str(result.get("asof")))
    except ValueError:
        return time.time()
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def _partial(result: dict, run_at: float) -> str | None:
    """Why this run is not a whole, correct session snapshot (None when it is).

    The card momentum is D01's since #380 S4, so the run is judged on the session's price bars (the freeze's anchor:
    SPY's D01 bar for the session), not on the universe snapshot behind the quote prices."""
    if not result.get("cards"):
        return result.get("no_data_message") or "no cards"
    if result.get("universe_partial"):
        return "the universe pull was partial"
    why = freeze._score_error_problem(result)
    if why:
        return why
    try:
        stamp = calendar.session_for(run_at)
    except ValueError:
        return None                                        # record_run says "no session" itself
    if stamp.quality != calendar.QUALITY_COMPLETE:
        return None                                        # an intraday run stays 'intraday'
    return freeze._make_anchor(result)(stamp.session_date)


def record(result: dict, *, run_id: str):
    """Record a finished AG scan: a header for every Fetch, the rows when it has cards and is a whole run.

    Returns rows written per table."""
    rows = ticker_store.rows_from_cards(result.get("cards") or [])
    summary = result.get("summary") or {}
    errors = summary.get("score_error_types")
    run_at = _run_at(result)
    return history.record_run(
        BOARD, run_id=run_id, run_at=run_at, model_version=freeze.model_version(),
        rows={"ag_scores_history": rows}, params=freeze.effective_config(),
        context={"asof": result.get("asof"), "data_as_of": result.get("data_as_of"),
                 "universe_total": result.get("universe_total"),
                 "universe_generated_at": result.get("universe_generated_at"),
                 "universe_refreshed_by": result.get("universe_refreshed_by"),
                 "universe_partial": bool(result.get("universe_partial")), "summary": summary},
        funnel=result.get("funnel_steps"), warnings=[{"score_error_types": errors}] if errors else None,
        partial=_partial(result, run_at), provider_health=result.get("provider_health"))


def recorded_sessions(limit: int = SESSIONS_SHOWN) -> list[dict]:
    """The board's last ``limit`` recorded sessions, newest first, from history.duckdb.

    Per session: ``session_date``, the snapshot run (``run_id``, ``run_at``) and ``runs`` (every Fetch the session
    recorded, header-only runs included), from ``core/history.sessions`` (K2); and from the snapshot's rows
    (``ag_scores_by_session``) ``names`` scored, ``avg_points`` and ``top``: the ``TOP_N`` tickers by points, ties
    by ticker. A session with no snapshot (intraday, partial or failed runs only) has no rows and is not listed.
    ``[]`` when nothing is recorded; a read never creates the history file."""
    snaps = history.sessions(BOARD, limit)
    if not snaps:
        return []
    marks = ", ".join("?" for _ in snaps)
    scored = {r["run_id"]: r for r in rawstore.query(
        f"""
        SELECT run_id, count(*) AS names, avg(total_points) AS avg_points,
               list(ticker ORDER BY total_points DESC NULLS LAST, ticker)[1:{int(TOP_N)}] AS top
        FROM ag_scores_history WHERE run_id IN ({marks}) GROUP BY run_id""",
        [s["run_id"] for s in snaps], db="history")}
    out = []
    for s in snaps:
        rows = scored.get(s["run_id"]) or {}
        out.append({"session_date": s["session_date"], "run_id": s["run_id"], "run_at": s["run_at"],
                    "runs": s["runs"], "names": int(rows.get("names") or 0), "avg_points": rows.get("avg_points"),
                    "top": list(rows.get("top") or [])})
    return out
