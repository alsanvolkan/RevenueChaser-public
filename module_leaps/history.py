"""LEAPS board -> result history (#377, A3; avo's card 6: LEAPS footprints, no 60 cap, one per session).

``record`` is a step of a LEAPS Fetch (``app/runners.run_leaps_latest``), after the board file is written. It hands
``core/history.record_run`` one ``leaps_footprint_history`` row per watchlist ticker the scan fetched, whatever its
label (the board lists only Accumulating and Distributing): the footprint's columns, the flow-print metrics merged
into it, ``fetch_status`` and ``inputs`` (compressed: the three UW answers, the two baselines it was classified
against and its flow candidate). The mapping lives here, in the module, not in ``core/``. A Refresh
(``run_leaps_cached``) never calls it (06 rule 9); it reads the resolved names back from the last recorded session
(``scan._history_names``, #377 L5).

``baselines`` is the other reader (K2, #377 L3): the classifier's two baselines come from this table, not from the
frozen ``leaps_cache.history`` list. Per ticker the ``1d`` baseline is the newest recorded session strictly before
today (a same-session row is not its own baseline), the ``q`` baseline the newest of those at or before the last
quarter end (O8), and both carry the OI-per-expiry rows they were built from, so growth is matched-expiry (O9,
``engine.matched_far_growth``). One batched read per scan: the session runs, their run counts and their rows.

``leaps_cache`` keeps the latest snapshot per ticker (Earnings reads it) and its old list, frozen (D1, #377 L3):
``engine.save_cache`` carries it unchanged and never appends, so no data is deleted.

LEAPS is a session board: the first complete run of a session keeps its rows; a later, intraday or partial run keeps
its header only (``core/history.record_run``). A run is partial when the watchlist is empty, a footprint could not be
fetched, or the UW key was rejected, the client's breaker opened or some UW calls failed. The print scan hitting its
page cap is a header warning, not a reason: the footprints are whole and the board shows the same warning.
"""
from __future__ import annotations

import json
from datetime import date

from RevenueChaser.core import history, ledger, rawstore
from RevenueChaser.module_leaps import engine

BOARD, TABLE = "module_leaps", "leaps_footprint_history"
# Bump when the footprint or flow scoring changes in a way the config hash does not see (``engine._score``'s
# weights, ``bucket_score``, a formula), not a threshold.
# 2 (#377 L3): the baselines are the recorded sessions and growth is matched-expiry.
SELECTION_VERSION = "2"
_SESSIONS_READ = 130            # about two quarters of sessions: enough for the 1d and the quarter baseline
_LIST_CAP = 10
_COLUMNS = ("label", "score", "flow_score", "far_call_oi", "far_put_oi", "far_call_share", "far_oi_growth_1d_pct",
            "far_oi_growth_q_pct", "far_call_sweep_pct", "iv_rank", "iv_rank_slope", "near_term_event", "as_of",
            "frequency", "notional", "net_notional", "concentration", "concentration_mult", "ask_share", "itm_conc",
            "otm_conc")


def effective_config() -> dict:
    """The thresholds the footprint and the flow scan are built and labelled with."""
    return {
        "FAR_DTE": engine.FAR_DTE, "NEAR_EVENT_DAYS": engine.NEAR_EVENT_DAYS, "CALL_SHARE_MIN": engine.CALL_SHARE_MIN,
        "ASK_SHARE_MIN": engine.ASK_SHARE_MIN,
        "MIN_DTE": engine.MIN_DTE, "MIN_PREMIUM": engine.MIN_PREMIUM, "ITM_DELTA": list(engine.ITM_DELTA),
        "OTM_DELTA": list(engine.OTM_DELTA), "ITM_CONC_MIN": engine.ITM_CONC_MIN,
        "OTM_CONC_MIN": engine.OTM_CONC_MIN, "INDEX_TICKERS": sorted(engine.INDEX_TICKERS),
    }


def model_version() -> str:
    return ledger.model_version(SELECTION_VERSION, effective_config())


# ── baselines (K2 readers; O8/O9, #377 L3) ────────────────────────────────────────────────────────────────────────

def _iso(value) -> str:
    """A session date as an ISO string: the ``as_of`` column is a DATE, so it reads back as a ``date``."""
    if value is None or value == "":
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)[:10]


def _sorted_rows(rows) -> list[dict]:
    return sorted((r for r in rows or [] if r.get("as_of")), key=lambda r: r["as_of"], reverse=True)


def pick(rows, today) -> dict:
    """The baselines for ``today`` out of one ticker's recorded rows (newest first). O8, #377 L3.

    ``1d`` is the newest row from a session strictly before ``today`` (a same-session row is not its own
    baseline); ``q`` is the newest of those at or before the last quarter end, so a row after the quarter end
    never becomes the quarter baseline. ``newest`` is the newest row whatever its date (D4 reads it).
    """
    day = today if isinstance(today, date) else date.fromisoformat(_iso(today))
    ordered = _sorted_rows(rows)
    prior = [r for r in ordered if r["as_of"] < day.isoformat()]
    quarter_end = engine._prev_quarter_end(day)
    return {"newest": ordered[0] if ordered else None,
            "1d": prior[0] if prior else None,
            "q": next((r for r in prior if r["as_of"] <= quarter_end), None)}


def _baseline_row(row: dict) -> dict:
    """One recorded row as a baseline: its date and the OI-per-expiry it was built from (O9)."""
    inputs = row.get("inputs") or {}
    return {"as_of": _iso(row.get("as_of")), "label": row.get("label"),
            "oi_per_expiry": inputs.get("oi_per_expiry") or [],
            "far_call_oi": row.get("far_call_oi"), "far_put_oi": row.get("far_put_oi"),
            "far_call_share": row.get("far_call_share"), "ask_share": row.get("ask_share"),
            "near_term_event": row.get("near_term_event")}


def baselines(tickers, today) -> dict:
    """Recorded baselines per ticker from ``leaps_footprint_history`` (O8/O9, #377 L3).

    One read of the snapshot sessions (their run ids), one of their rows, and the ``core.history`` count query:
    three history queries for a whole watchlist, not one per ticker. A ticker with no recorded row has ``1d`` and
    ``q`` None, so growth is None, never 0. ``rows`` (newest first) lets a caller pick again for another date (a
    Refresh re-derives with the row's own ``as_of``, D4). A locked file raises; the caller turns it into a warning.
    """
    wanted = sorted({str(t).strip().upper() for t in (tickers or []) if t})
    if not wanted:
        return {}
    with rawstore.brief_wait():
        snaps = history.sessions(BOARD, _SESSIONS_READ)
        if not snaps:
            return {}
        ids = [s["run_id"] for s in snaps]
        rows = rawstore.query(f"SELECT * FROM {TABLE} WHERE run_id IN ({', '.join('?' for _ in ids)})",
                              ids, db="history")
    by: dict[str, list[dict]] = {}
    for row in rows:
        ticker = (row.get("ticker") or "").upper()
        if ticker in wanted:
            by.setdefault(ticker, []).append(_baseline_row(rawstore._decode_row(TABLE, row)))
    out = {}
    for ticker in wanted:
        ordered = _sorted_rows(by.get(ticker, []))
        out[ticker] = {**pick(ordered, today), "rows": ordered}
    return out


def _plain(value):
    """JSON-safe copy: dates and other non-JSON values become strings."""
    return json.loads(json.dumps(value, default=str))


def _row(footprint: dict) -> dict:
    snap = footprint.get("snapshot") or {}
    return {"ticker": footprint["ticker"], "name": footprint.get("name") or snap.get("name"),
            **{c: snap.get(c) for c in _COLUMNS}, "fetch_status": footprint.get("fetch_status"),
            "inputs": _plain(footprint.get("inputs") or None)}


def rows_from_footprints(footprints) -> list[dict]:
    """One row per ticker, the first of a repeated ticker (the table's key is run_id + ticker)."""
    out: dict[str, dict] = {}
    for fp in footprints or []:
        if fp.get("ticker") and fp["ticker"] not in out:
            out[fp["ticker"]] = _row(fp)
    return list(out.values())


def _problems(rows: list[dict], uw_health: dict | None) -> list[str]:
    """Why this run is not a whole session snapshot (empty when it is)."""
    out = [] if rows else ["the watchlist is empty, so no footprint was scanned"]
    bad = sorted(r["ticker"] for r in rows if r["fetch_status"] != "ok")
    if bad:
        listed = ", ".join(bad[:_LIST_CAP]) + (f" +{len(bad) - _LIST_CAP} more" if len(bad) > _LIST_CAP else "")
        out.append(f"{len(bad)} of {len(rows)} footprints were not fetched whole: {listed}")
    return out + history.uw_problems(uw_health)


def record(footprints, *, run_id: str, run_at: float, uw_health: dict | None = None,
           warnings: list | None = None, shown: dict | None = None):
    """Record a finished LEAPS Fetch: a header for every Fetch, the rows when it is a whole run.

    ``footprints`` is the list ``scan.scan_leaps`` filled; ``shown`` how many names the board lists. Returns rows
    written per table."""
    rows = rows_from_footprints(footprints)
    return history.record_run(
        BOARD, run_id=run_id, run_at=run_at, model_version=model_version(), rows={TABLE: rows},
        params=effective_config(), context={"watchlist": len(rows), "shown": shown},
        warnings=list(warnings) if warnings else None,
        partial="; ".join(_problems(rows, uw_health)) or None)
