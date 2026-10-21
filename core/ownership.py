"""Ownership per session (#300, D20): the one writer of ``ownership_snapshot``.

Two jobs store what they read, each under its own ``source``:

- ``universe`` / Asymmetric Growth (``yfinance``): shares outstanding, insider % and institutional % from each fresh
  yfinance ``.info`` the Fetch already gets for the D12 valuation (``vendor_data.Capture(ownership=True)``). No
  extra request.
- the 13F Fetch (``uw_info``): UW ``/stock/{t}/info`` ``outstanding`` of every 13F ticker, one call each per
  session, at most ``UW_MAX_CALLS_PER_FETCH`` per Fetch (:func:`store_uw_shares`, ``app/runners.build_sec13f``). It
  replaces the retired 13F enrichment writer.

Session snapshot, rule B of ``phase2/06_interface.md`` §2: one row per (ticker, session_date, source); only a
complete-session run writes (a weekend or pre-open run maps to the last complete session, an intraday run writes
nothing) and the first write of a session wins. ``insider_pct`` and ``institutional_pct`` are fractions as served
(``06_interface/ddl_292.md`` §10); a field the source did not give is NULL, and a source that gave none of the three
writes no row.

Which source a reader takes first is the readers' rule (06 §3 D20: UW, then SEC dei via D11, then yfinance); this
module stores every source as served and changes no board.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date

from RevenueChaser.core import calendar, job_control, rawstore

logger = logging.getLogger(__name__)

TABLE = "ownership_snapshot"
YFINANCE = "yfinance"
UW_INFO = "uw_info"
FIELDS = ("shares_outstanding", "insider_pct", "institutional_pct")

# ownership_snapshot column -> yfinance ``.info`` key (``module_sec_13f/enrich.py`` read the insider one,
# ``module_asymmetric_growth/engine.py`` the institutional one; ``core/providers/yfinance.py`` the shares).
_YF_KEYS = {"shares_outstanding": "sharesOutstanding", "insider_pct": "heldPercentInsiders",
            "institutional_pct": "heldPercentInstitutions"}

UW_BATCH = 100          # tickers asked per stored batch: a crash or a killed dashboard keeps the batches stored
# UW calls one 13F Fetch may make (#300 review B1). The 13F job cannot be cancelled (``app/dashboard.py``), so this
# is the only bound on a run. 2,222 13F tickers on the PC (2026-10-09) plus room; the order of ``pending_uw`` makes
# a lower cap spread over Fetches.
UW_MAX_CALLS_PER_FETCH = 2_500


def _clock() -> float:
    """Epoch seconds; the one clock seam of this module (tests pin it)."""
    return time.time()


def _num(v) -> float | None:
    """A finite float, else None (never 0)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str) and not v.strip():
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _row(ticker: str, source: str, values: dict) -> dict | None:
    if all(values.get(f) is None for f in FIELDS):
        return None
    return {"ticker": ticker.upper(), "source": source, **{f: values.get(f) for f in FIELDS}}


# ── the mappers (pure) ───────────────────────────────────────────────────────

def row_yfinance(ticker: str, info: dict | None) -> dict | None:
    """The row of a yfinance ``.info`` dict, None when it has none of the three fields."""
    if not isinstance(info, dict):
        return None
    return _row(ticker, YFINANCE, {col: _num(info.get(key)) for col, key in _YF_KEYS.items()})


def row_uw(ticker: str, info: dict | None) -> dict | None:
    """The row of a UW ``/stock/{t}/info`` ``data`` object: shares outstanding only; None without it."""
    if not isinstance(info, dict):
        return None
    return _row(ticker, UW_INFO, {"shares_outstanding": _num(info.get("outstanding"))})


# ── the writer ───────────────────────────────────────────────────────────────

def session_of(run_at: float) -> tuple[date | None, str | None]:
    """(session_date, None) when a run at ``run_at`` may write, else (None, why not)."""
    try:
        stamp = calendar.session_for(run_at)
    except ValueError:
        return None, "no session"
    if stamp.quality != calendar.QUALITY_COMPLETE:
        return None, "intraday"
    return stamp.session_date, None


def _placeholders(n: int) -> str:
    return ", ".join(["?"] * n)


def record(rows, *, run_id: str, run_at: float | None = None, fetched_at: float | None = None) -> dict:
    """Store ``rows`` (from the mappers) for the session of ``run_at``, first write per key, in one transaction.

    A row's own ``fetched_at`` wins over the argument. Returns ``written``, ``kept`` (the key was already stored),
    ``skipped`` and ``skip_reason`` (an intraday run, or no session). Raises on a store error."""
    run_at = _clock() if run_at is None else float(run_at)
    fetched_at = run_at if fetched_at is None else float(fetched_at)
    rows = [r for r in rows if r]
    out = {"written": 0, "kept": 0, "skipped": 0, "skip_reason": None}
    if not rows:
        return out
    session, why = session_of(run_at)
    if session is None:
        out["skipped"], out["skip_reason"] = len(rows), why
        return out
    by_key: dict[tuple, dict] = {}
    for r in rows:
        by_key.setdefault((r["ticker"], r["source"]), r)       # the key twice in one batch: the first answer wins
    tickers = sorted({t for t, _ in by_key})

    def _write(tx):
        tx.ensure_table(TABLE)
        taken = {(t, s) for t, s in tx.execute(
            f"SELECT ticker, source FROM {TABLE} WHERE session_date = ? "
            f"AND ticker IN ({_placeholders(len(tickers))})", [session, *tickers]).fetchall()}
        fresh = [{**r, "session_date": session, "fetched_at": r.get("fetched_at") or fetched_at,
                  "run_id": run_id, "run_at": run_at}
                 for k, r in by_key.items() if k not in taken]
        tx.write_rows(TABLE, fresh)
        return len(fresh)

    out["written"] = rawstore.atomic(_write)
    out["kept"] = len(by_key) - out["written"]
    return out


# ── the 13F UW shares step ───────────────────────────────────────────────────

def _newest(source: str) -> dict[str, date]:
    """``{ticker: newest stored session}`` of ``source``."""
    return {r["ticker"]: r["s"] for r in rawstore.query(
        f"SELECT ticker, MAX(session_date) AS s FROM {TABLE} WHERE source = ? GROUP BY ticker", [source])}


def pending_uw(tickers, run_at: float) -> tuple[date | None, list[str]]:
    """(session, the tickers with no ``uw_info`` row for it yet); (None, []) when the run may not write.

    Order: never stored first, then the oldest stored session first, then the ticker, so a capped Fetch serves a
    different part of the list each time instead of the same head."""
    session, _ = session_of(run_at)
    if session is None:
        return None, []
    newest = _newest(UW_INFO)
    wanted = {str(t).upper() for t in tickers if t}
    todo = [t for t in wanted if newest.get(t) is None or newest[t] < session]
    return session, sorted(todo, key=lambda t: (newest.get(t) is not None, newest.get(t) or date.min, t))


def store_uw_shares(uw, tickers, *, run_id: str, run_at: float | None = None) -> dict:
    """Ask UW ``info`` for each ticker not stored yet this session and store its ``outstanding``, per batch.

    ``ticker_info`` never raises on a failed request (it answers ``{}``), so a failed or empty answer stores no row
    and the next Fetch of the session asks again. At most ``UW_MAX_CALLS_PER_FETCH`` tickers are asked; ``left``
    counts the rest, asked by the next Fetch. Each batch is stored before the next one is asked. Returns
    ``session``, ``skip_reason``, ``asked``, ``written``, ``no_value`` and ``left``."""
    run_at = _clock() if run_at is None else float(run_at)
    session, todo = pending_uw(tickers, run_at)
    cap = max(0, int(UW_MAX_CALLS_PER_FETCH))
    out = {"session": session, "skip_reason": session_of(run_at)[1], "asked": 0, "written": 0, "no_value": 0,
           "left": max(0, len(todo) - cap)}
    todo = todo[:cap]
    for start in range(0, len(todo), UW_BATCH):
        job_control.checkpoint()
        rows = []
        for t in todo[start:start + UW_BATCH]:
            out["asked"] += 1
            try:
                info = uw.ticker_info(t)
            except Exception as exc:  # noqa: BLE001 - one ticker's answer, never the whole step
                logger.debug("ownership %s UW info: %s", t, exc)
                info = None
            r = row_uw(t, info)
            if r is None:
                out["no_value"] += 1
                continue
            rows.append({**r, "fetched_at": _clock()})
        out["written"] += record(rows, run_id=run_id, run_at=run_at)["written"]
    return out
