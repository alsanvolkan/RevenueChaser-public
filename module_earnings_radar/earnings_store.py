"""Earnings prints with history (#298, D13): the one writer of ``earnings_history``.

An Earnings Fetch already reads two UW answers per scored ticker (``pipeline.EarningsPipeline._score``):
``/earnings/{ticker}`` (the calendar and straddle tape R4 and the drawer read, the upcoming event first) and
``/stock/{ticker}/earnings`` (the EPS print series R2 reads). This module keeps them; it makes no request.

A scoring worker hands each answer to the run's :class:`Capture`; the runner flushes it once, after the run, with the
job's ``run_id`` (``app/runners.run_earnings_range``), so the four workers never write. A Refresh fetches nothing and
writes nothing.

**Change history** (rule A of ``phase2/06_interface.md`` §2): one row per (ticker, endpoint, report_date) and run.
A row is appended only when its typed fields or its payload differ from the newest stored row of that key; a
daytime run appends a change too (#298). ``valid_from`` is the run's session (rule B), ``status`` is 'present'. UW's
``inserted_at`` / ``updated_at`` are not a change. A print the answer no longer serves, or a failed read (an empty
answer), is not closed with an 'absent' row: the vendor not serving a print says nothing about its value.

A row without a ``report_date`` has no key and is skipped. ``/stock/{ticker}/earnings`` can serve an annual and a
quarterly row on one report date; the key keeps one, the quarterly row (the one R2 reads). The typed columns hold
what the endpoint serves, never a derived value: ``surprise_pct`` is set from ``/stock/{ticker}/earnings`` only, the
expected and post-earnings moves from ``/earnings/{ticker}`` only (both as UW serves them, ratios).
"""
from __future__ import annotations

import json
import logging
import threading
import time

from RevenueChaser.core import calendar, ledger, rawstore
from RevenueChaser.core.vendor_data import _clean, _date, _num

logger = logging.getLogger(__name__)

TABLE = "earnings_history"
SOURCE = "uw"
PRESENT = "present"
EARNINGS = "/earnings/{ticker}"
STOCK_EARNINGS = "/stock/{ticker}/earnings"

NUMBERS = ("actual_eps", "eps_estimate", "surprise_pct", "expected_move_perc", "post_earnings_move_1d")
TEXTS = ("report_time", "report_type")
# endpoint -> column -> the served key it is read from
_FIELDS = {
    EARNINGS: {"actual_eps": "actual_eps", "eps_estimate": "street_mean_est",
               "expected_move_perc": "expected_move_perc", "post_earnings_move_1d": "post_earnings_move_1d",
               "fiscal_period_end": "ending_fiscal_quarter"},
    STOCK_EARNINGS: {"actual_eps": "reported_eps", "eps_estimate": "estimated_eps",
                     "surprise_pct": "surprise_percentage", "fiscal_period_end": "fiscal_date_ending",
                     "report_type": "report_type"},
}
# Served bookkeeping, not values: a change here alone appends nothing.
_VOLATILE = frozenset({"inserted_at", "updated_at"})

EMPTY_RESULT = {"appended": 0, "unchanged": 0}


def _clock() -> float:
    """Epoch seconds; the one clock seam of this module (tests pin it)."""
    return time.time()


def _text(v) -> str | None:
    return (str(v).strip() or None) if v is not None else None


def _row(ticker: str, endpoint: str, served: dict) -> dict | None:
    report_date = _date(served.get("report_date"))
    if report_date is None:
        return None
    src = _FIELDS[endpoint]
    out = {"ticker": ticker, "endpoint": endpoint, "report_date": report_date, "source": SOURCE,
           "report_time": _text(served.get("report_time")), "payload": _clean(served)}
    out.update({c: _num(served.get(src[c])) if c in src else None for c in NUMBERS})
    out["fiscal_period_end"] = _date(served.get(src["fiscal_period_end"]))
    report_type = _text(served.get(src["report_type"])) if "report_type" in src else None
    out["report_type"] = report_type.lower() if report_type else None
    return out


def rows_for(ticker: str, endpoint: str, served) -> list[dict]:
    """The ``earnings_history`` rows of one UW answer (pure): one per report date, the quarterly row first."""
    if endpoint not in _FIELDS:
        raise ValueError(f"unknown earnings endpoint: {endpoint}")
    ticker = (ticker or "").upper()
    by_date: dict = {}
    for rec in served if isinstance(served, list) else []:
        row = _row(ticker, endpoint, rec) if isinstance(rec, dict) else None
        if row is None:
            continue
        kept = by_date.get(row["report_date"])
        if kept is None or (kept["report_type"] != "quarterly" and row["report_type"] == "quarterly"):
            by_date[row["report_date"]] = row
    return list(by_date.values())


# ── the writer ───────────────────────────────────────────────────────────────

def _key(r: dict) -> tuple:
    return (r["ticker"], r["endpoint"], r["report_date"])


def _comparable(r: dict) -> tuple:
    payload = r.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
    if isinstance(payload, dict):
        payload = {k: v for k, v in payload.items() if k not in _VOLATILE}
    # One JSON form for both sides: what the store read back and what a fresh fetch built.
    text = json.dumps(_clean(payload), sort_keys=True, default=str)
    return (tuple(_num(r.get(c)) for c in NUMBERS) + tuple(r.get(c) for c in TEXTS)
            + (_date(r.get("fiscal_period_end")), text))


def _latest(tx, tickers: list[str]) -> dict[tuple, tuple]:
    """``{key: comparable}`` of the newest stored row of every key of ``tickers``."""
    if not tickers:
        return {}
    cols = ("ticker", "endpoint", "report_date", *NUMBERS, *TEXTS, "fiscal_period_end", "payload")
    cur = tx.execute(
        f"SELECT {', '.join(cols)} FROM {TABLE} WHERE ticker IN ({', '.join(['?'] * len(tickers))}) "
        f"QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker, endpoint, report_date ORDER BY run_at DESC) = 1", tickers)
    out = {}
    for values in cur.fetchall():
        r = dict(zip(cols, values))
        out[_key(r)] = _comparable(r)
    return out


def record(rows, *, run_id: str, run_at: float | None = None) -> dict:
    """Append the rows whose print changed since its newest stored row, in one transaction. Rows come from
    :func:`rows_for`; a row's own ``fetched_at`` wins over ``run_at``. Returns the counts. Raises on a store error."""
    run_at = _clock() if run_at is None else float(run_at)
    rows = [r for r in rows if r]
    if not rows:
        return dict(EMPTY_RESULT)
    try:
        valid_from = calendar.session_for(run_at).session_date
    except ValueError:
        valid_from = None                      # outside the holiday table: the row keeps run_at only

    def _write(tx):
        tx.ensure_table(TABLE)
        latest = _latest(tx, sorted({r["ticker"] for r in rows}))
        new_rows, seen, unchanged = [], set(), 0
        for r in rows:
            k = _key(r)
            if k in seen:
                continue                       # the key twice in one run: the first answer wins
            seen.add(k)
            if latest.get(k) == _comparable(r):
                unchanged += 1
                continue
            new_rows.append({**r, "valid_from": valid_from, "status": PRESENT,
                             "fetched_at": r.get("fetched_at") or run_at, "run_id": run_id, "run_at": run_at})
        tx.write_rows(TABLE, new_rows)
        return {"appended": len(new_rows), "unchanged": unchanged}

    return rawstore.atomic(_write)


class Capture:
    """The earnings answers a run's scoring workers read, collected thread-safely and stored by :meth:`flush` on
    the job thread, once per run. ``run_at`` is the run's time (default: when the capture is made)."""

    def __init__(self, run_at: float | None = None):
        self.run_at = _clock() if run_at is None else float(run_at)
        self._noted = False                    # a store that keeps failing gives one job note per run
        self._lock = threading.Lock()
        self._rows: list[dict] = []

    def add(self, ticker: str, endpoint: str, served) -> None:
        """One UW answer of ``endpoint`` (:data:`EARNINGS` or :data:`STOCK_EARNINGS`) for ``ticker``."""
        rows = rows_for(ticker, endpoint, served)
        now = _clock()
        with self._lock:
            self._rows.extend({**r, "fetched_at": now} for r in rows)

    def flush(self, *, run_id: str, label: str = "Earnings") -> dict:
        """Store and clear what was collected. A store failure is a job note and an ``error`` key, never an
        exception: the board the job builds does not depend on these rows."""
        with self._lock:
            rows, self._rows = self._rows, []
        if not rows:
            return dict(EMPTY_RESULT)
        try:
            return record(rows, run_id=run_id, run_at=self.run_at)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.exception("%s prints were not stored", label)
            text = f"{type(exc).__name__}: {exc}"
            if not self._noted:
                self._noted = True
                ledger.add_note(f"{label} prints were not stored: {text}")
            return {**EMPTY_RESULT, "error": text}
