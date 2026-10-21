"""Forecast outcomes (#62 62b-1, #380 S0): what happened after each frozen pick, derived into ``forecast_outcomes``.

``forecast_ledger`` is append-only and holds no outcome (decision 5-B). This module reads its picks and the stored
D01 daily bars and writes, for every pick and horizon, how far the price went for and against it. Everything here
is derived: it can be recomputed at any time from the ledger and the bars, so the table is a keyed upsert.

Rules (S0 card, O-1 default; #62 62b):
- A pick (``target_type`` 'pick') gets one row per horizon in ``HORIZONS`` (calendar days).
- The window is the sessions after ``forecast_date`` up to the last session on or before ``forecast_date`` + N
  (``window_end``). The forecast day itself is never in it: the board froze after that session's close.
- ``ref_price`` is the open of the first session after ``forecast_date``: the first price the pick could be bought
  at. It comes from the same nominal series as the bars it is compared with, so a split inside the window changes
  nothing. Only that session's bar counts: when it is missing or has no usable open (None, NaN, <= 0), a later open
  never stands in for it; the row has no reference and stays ``open`` (review S0-1).
- ``mfe`` = max(High) / ref - 1 and ``mae`` = min(Low) / ref - 1 over the window's sessions seen so far, on
  ``prices.daily_bars(basis="nominal")`` (splits applied, dividends not).
- ``status``: ``closed`` once the row has a reference and the ticker has a bar on or after the window's last
  session; ``no_bars`` when a session has completed since ``forecast_date`` and the ticker has no bar in the window;
  else ``open``.
- ``split_check`` instead of ``closed``/``open`` when the nominal series jumps by ``SPLIT_JUMP`` or more from
  ``forecast_date``'s close to the window's end (the last usable close -> a bar's open, else its close; review
  S0-7): a split the store has no action for, so the bars mix two bases (the POAS case: shown 0.14, next open
  2.12). The values stay for a look; the row is not final, so once D01 records the split the nightly recomputes it
  on one basis and it closes.
- A ``closed`` row of the current ``MODEL_VERSION`` is final: the nightly run skips it, and selects the picks still
  to do in SQL, so it never loads the whole ledger (review S0-2). ``full`` recomputes all.
- With no daily bar stored at all (a missing or empty price store) while a pick has a completed session, the run
  refuses before any write: it would otherwise turn every final row into ``no_bars`` (review S0-3).

Reads ``forecast_ledger`` and the bars; writes ``forecast_outcomes`` only, through ``rawstore.write_rows``. It never
writes the ledger and makes no network call (``prices.daily_bars`` reads the store only).
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import date, datetime, timedelta

from RevenueChaser.core import calendar, prices, rawstore

logger = logging.getLogger(__name__)

TABLE = "forecast_outcomes"
LEDGER = "forecast_ledger"
PICK = "pick"
HORIZONS = (30, 90)
# Bump when a rule above changes: a closed row of an older version is recomputed by the next nightly run.
MODEL_VERSION = "o1:next-open:calendar-days"
OPEN, CLOSED, NO_BARS, SPLIT_CHECK = "open", "closed", "no_bars", "split_check"
# A previous close -> open ratio at or beyond this (either way) is read as an unrecorded split: 3:1 and 1:3 or
# larger. A 2:1 split with no action is not caught; a real gap that large is rare enough to flag, not drop.
SPLIT_JUMP = 2.5
CHUNK = 200            # tickers per bar read and per write (a short transaction each)

_KEY = ("source", "forecast_date", "subject", "target_type", "target_date")


def window_end(forecast_date: date, horizon_days: int) -> date:
    """The last session on or before ``forecast_date`` + ``horizon_days`` calendar days."""
    return calendar.previous_trading_day(forecast_date + timedelta(days=horizon_days))


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None          # NaN (a pandas gap) is no value


def outcome(forecast_date: date, horizon_days: int, bars: list[dict], latest_session: date) -> dict:
    """The outcome columns of one pick and horizon. ``bars``: the ticker's nominal bars, sorted by session date
    (``session_date`` a ``date``, ``open``/``high``/``low``/``close``); ``latest_session``: the latest complete
    session."""
    end = window_end(forecast_date, horizon_days)
    window = [b for b in bars if forecast_date < b["session_date"] <= end]
    if not window:
        status = NO_BARS if latest_session > forecast_date else OPEN
        return {"ref_price": None, "mfe": None, "mae": None, "sessions_seen": 0, "status": status}
    first = calendar.next_trading_day(forecast_date)
    ref = _num(window[0]["open"]) if window[0]["session_date"] == first else None
    if ref is None or ref <= 0:          # no reference: not final, the nightly retries it
        return {"ref_price": None, "mfe": None, "mae": None, "sessions_seen": len(window), "status": OPEN}
    if _split_jump([b for b in bars if b["session_date"] == forecast_date] + window):
        status = SPLIT_CHECK
    else:
        status = CLOSED if bool(bars) and bars[-1]["session_date"] >= end else OPEN
    highs = [h for h in (_num(b["high"]) for b in window) if h is not None]
    lows = [lo for lo in (_num(b["low"]) for b in window) if lo is not None]
    return {"ref_price": ref,
            "mfe": max(highs) / ref - 1 if highs else None,
            "mae": min(lows) / ref - 1 if lows else None,
            "sessions_seen": len(window),
            "status": status}


def _split_jump(bars: list[dict]) -> bool:
    """True when a bar's first usable price (its open, else its close) is ``SPLIT_JUMP`` times (or 1/``SPLIT_JUMP``)
    the last usable close before it. A bar without a usable open or close does not hide the jump."""
    last = None
    for b in bars:
        o, c = _num(b.get("open")), _num(b.get("close"))
        price = o if o is not None and o > 0 else c if c is not None and c > 0 else None
        if last is not None and price is not None and not 1 / SPLIT_JUMP < price / last < SPLIT_JUMP:
            return True
        if c is not None and c > 0:
            last = c
    return False


def _pick_count() -> int:
    rows = rawstore.query(f"SELECT count(*) AS n FROM {LEDGER} WHERE target_type = ?", [PICK])
    return int(rows[0]["n"]) if rows else 0


def _todo_picks(full: bool) -> list[dict]:
    """The picks with a horizon still to compute, each with ``closed``: its horizons already final. ``full``: every
    pick. Otherwise an anti-join in SQL keeps only the picks with fewer than ``len(HORIZONS)`` closed rows of the
    current ``MODEL_VERSION``, so the nightly never loads the whole ledger or every closed key."""
    cols = ", ".join(f"l.{k}" for k in _KEY)
    if full or not rawstore.query("SELECT 1 AS x FROM information_schema.tables WHERE table_name = ?", [TABLE]):
        rows = rawstore.query(f"SELECT {cols} FROM {LEDGER} l WHERE l.target_type = ? "
                              "ORDER BY l.subject, l.source, l.forecast_date", [PICK])
        return [{**r, "closed": ()} for r in rows]
    on = " AND ".join(f"o.{k} = l.{k}" for k in _KEY)
    hz = ", ".join("?" for _ in HORIZONS)
    rows = rawstore.query(
        f"SELECT {cols}, list(o.horizon_days) FILTER (WHERE o.horizon_days IS NOT NULL) AS closed "
        f"FROM {LEDGER} l LEFT JOIN {TABLE} o ON {on} AND o.status = ? AND o.model_version = ? "
        f"AND o.horizon_days IN ({hz}) WHERE l.target_type = ? GROUP BY {cols} "
        "HAVING count(o.horizon_days) < ? ORDER BY l.subject, l.source, l.forecast_date",
        [CLOSED, MODEL_VERSION, *HORIZONS, PICK, len(HORIZONS)])
    return [{**r, "closed": tuple(r["closed"] or ())} for r in rows]


def _newest_bar() -> date | None:
    rows = rawstore.query("SELECT max(session_date) AS d FROM price_bars_daily", db=rawstore._db_of("price_bars_daily"))
    return rows[0]["d"] if rows else None


def _bars(tickers: list[str], start: date) -> dict[str, list[dict]]:
    frame = prices.daily_bars(tickers, basis="nominal", start=start)
    out: dict[str, list[dict]] = {}
    if frame.empty:
        return out
    for t, d, o, h, lo, c in zip(frame["ticker"], frame["session_date"], frame["open"], frame["high"], frame["low"],
                                 frame["close"]):
        out.setdefault(t, []).append({"session_date": d.date(), "open": o, "high": h, "low": lo, "close": c})
    return out


def run(*, now: datetime | None = None, run_id: str | None = None, full: bool = False, write: bool = True) -> dict:
    """Compute the outcome rows of every ledger pick and upsert them; returns a summary.

    ``full=False`` (the nightly) skips the closed rows of the current ``MODEL_VERSION``; ``write=False`` computes and
    counts only (a dry run). ``now`` is the clock (default: ``calendar``'s seam); ``computed_at`` is its epoch.
    """
    t0 = time.monotonic()
    latest = calendar.latest_complete_session(now)
    stamp = (now or calendar.now_ny()).timestamp()
    picks = _pick_count()                 # before the todo read: a pick frozen in between is computed, never skipped
    todo: dict[str, list[tuple[dict, int]]] = {}
    for p in _todo_picks(full):
        for h in HORIZONS:
            if h not in p["closed"]:
                todo.setdefault(str(p["subject"]), []).append((p, h))
    skipped = max(picks * len(HORIZONS) - sum(len(v) for v in todo.values()), 0)
    if any(p["forecast_date"] < latest for v in todo.values() for p, _ in v) and _newest_bar() is None:
        raise RuntimeError("no daily bars in the price store: refusing to compute outcomes "
                           "(every final row would turn into no_bars)")

    status: Counter = Counter()
    rows_total = written = 0
    tickers = sorted(todo)
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        start = min(p["forecast_date"] for t in chunk for p, _ in todo[t])     # its close anchors the split check
        bars = _bars(chunk, start)
        rows = []
        for t in chunk:
            series = bars.get(t, [])
            for p, h in todo[t]:
                row = {**{k: p[k] for k in _KEY}, "horizon_days": h,
                       **outcome(p["forecast_date"], h, series, latest),
                       "computed_at": stamp, "model_version": MODEL_VERSION, "run_id": run_id}
                status[row["status"]] += 1
                rows.append(row)
        rows_total += len(rows)
        if write and rows:
            written += rawstore.write_rows(TABLE, rows)
    res = {"run_id": run_id, "latest_session": latest.isoformat(), "picks": picks, "rows": rows_total,
           "skipped_closed": skipped, "written": written, "status": dict(status),
           "tickers": len(tickers), "full": full, "dry_run": not write,
           "seconds": round(time.monotonic() - t0, 2)}
    logger.info("forecast outcomes: %s", res)
    return res
