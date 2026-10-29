"""Forecast outcomes (#62 62b-1, #380 S0): what happened after each frozen pick, derived into ``forecast_outcomes``.

``forecast_ledger`` is append-only and holds no outcome (decision 5-B). This module reads its picks and the daily bars
of the prices store and writes, for every pick and horizon, how far the price went for and against it. Everything here
is derived: it can be recomputed at any time from the ledger and the bars, so the table is a keyed upsert.

Since PR 10 (G4) both tables live in the ledger serving kind (``ledger.duckdb``) and the builder's ``ledger/outcomes``
dataset runs this module on its build file (``con``): the rows land in the new file, the swap publishes them. The bars
come from ``core.market_bars`` (the prices store), where they came from D01 (``core.prices``) before.

Rules (S0 card, O-1 default; #62 62b):
- A pick (``target_type`` 'pick') gets one row per horizon in ``HORIZONS`` (calendar days).
- The window is the sessions after ``forecast_date`` up to the last session on or before ``forecast_date`` + N
  (``window_end``). The forecast day itself is never in it: the board froze after that session's close.
- ``ref_price`` is the open of the first session after ``forecast_date``: the first price the pick could be bought
  at. It comes from the same nominal series as the bars it is compared with, so a split inside the window changes
  nothing. Only that session's bar counts: when it is missing or has no usable open (None, NaN, <= 0), a later open
  never stands in for it; the row has no reference and stays ``open`` (review S0-1).
- ``mfe`` = max(High) / ref - 1 and ``mae`` = min(Low) / ref - 1 over the window's sessions seen so far, on
  ``market_bars.daily_bars(basis="nominal")`` (splits applied, dividends not; the prices store's ``split`` basis).
- ``status``: ``closed`` once the row has a reference and the ticker has a bar on or after the window's last
  session; ``no_bars`` when a session has completed since ``forecast_date`` and the ticker has no bar in the window;
  else ``open``.
- ``split_check`` instead of ``closed``/``open`` when the nominal series jumps by ``SPLIT_JUMP`` or more from
  ``forecast_date``'s close to the window's end (the last usable close -> a bar's open, else its close; review
  S0-7): a split the store has no action for, so the bars mix two bases (the POAS case: shown 0.14, next open
  2.12). The values stay for a look; the row is not final, so once the prices store records the split the next run
  recomputes it on one basis and it closes.
- A ``closed`` row of the current ``MODEL_VERSION`` is final: the nightly run skips it, and selects the picks still
  to do in SQL, so it never loads the whole ledger (review S0-2). ``full`` recomputes all.
- With no daily bar stored at all (a missing or empty price store) while a pick has a completed session, the run
  refuses before any write: it would otherwise turn every final row into ``no_bars`` (review S0-3).

Reads ``forecast_ledger`` and the bars; writes ``forecast_outcomes`` only, in ``con`` (delete the key, insert the row).
It never writes the ledger and makes no network call (``market_bars`` reads the prices store only).

shortcut: a pick's bars are found by its ``subject`` as the ticker a security trades under now, so a pick whose
security has since changed its symbol reads no bars (``no_bars``); upgrade when the ledger row carries the
``security_id`` the board scored.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import date, datetime, timedelta

from RevenueChaser.core import calendar, market_bars

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
CHUNK = 200            # tickers per bar read

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
    return outcome_by_horizon(forecast_date, (horizon_days,), bars, latest_session)[horizon_days]


def outcome_by_horizon(forecast_date: date, horizons: tuple[int, ...], bars: list[dict],
                       latest_session: date) -> dict[int, dict]:
    """``{horizon: outcome columns}`` for every horizon, bucketed per horizon in one pass over ``bars``.

    Each bar is tested against every horizon's window in that single pass, so a pick with both horizons walks its
    series once instead of once per horizon; ``outcome`` is this function with one horizon, and its rules are the ones
    applied here.
    """
    ends = {h: window_end(forecast_date, h) for h in horizons}
    windows: dict[int, list[dict]] = {h: [] for h in horizons}
    anchor: list[dict] = []
    for b in bars:
        day = b["session_date"]
        if day == forecast_date:
            anchor.append(b)
        for h, end in ends.items():
            if forecast_date < day <= end:
                windows[h].append(b)
    last = bars[-1]["session_date"] if bars else None
    return {h: _outcome(forecast_date, end, windows[h], anchor, last, latest_session)
            for h, end in ends.items()}


def _outcome(forecast_date: date, end: date, window: list[dict], anchor: list[dict],
             last: date | None, latest_session: date) -> dict:
    """The outcome columns of one horizon over its ``window``: ``outcome``'s rule, in one place.

    ``anchor``: the bars on ``forecast_date`` itself (the split check starts from that close); ``last``: the ticker's
    newest session, the proof its window has ended.
    """
    if not window:
        status = NO_BARS if latest_session > forecast_date else OPEN
        return {"ref_price": None, "mfe": None, "mae": None, "sessions_seen": 0, "status": status}
    first_session = calendar.next_trading_day(forecast_date)
    ref = _num(window[0]["open"]) if window[0]["session_date"] == first_session else None
    if ref is None or ref <= 0:          # no reference: not final, the nightly retries it
        return {"ref_price": None, "mfe": None, "mae": None, "sessions_seen": len(window), "status": OPEN}
    if _split_jump(anchor + window):
        status = SPLIT_CHECK
    else:
        status = CLOSED if last is not None and last >= end else OPEN
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


def _query(con, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _pick_count(con) -> int:
    rows = _query(con, f"SELECT count(*) AS n FROM {LEDGER} WHERE target_type = ?", [PICK])
    return int(rows[0]["n"]) if rows else 0


def _todo_picks(con, full: bool) -> list[dict]:
    """The picks with a horizon still to compute, each with ``closed``: its horizons already final. ``full``: every
    pick. Otherwise an anti-join in SQL keeps only the picks with fewer than ``len(HORIZONS)`` closed rows of the
    current ``MODEL_VERSION``, so a run never loads the whole ledger or every closed key."""
    cols = ", ".join(f"l.{k}" for k in _KEY)
    if full:
        rows = _query(con, f"SELECT {cols} FROM {LEDGER} l WHERE l.target_type = ? "
                           "ORDER BY l.subject, l.source, l.forecast_date", [PICK])
        return [{**r, "closed": ()} for r in rows]
    on = " AND ".join(f"o.{k} = l.{k}" for k in _KEY)
    hz = ", ".join("?" for _ in HORIZONS)
    rows = _query(
        con,
        f"SELECT {cols}, list(o.horizon_days) FILTER (WHERE o.horizon_days IS NOT NULL) AS closed "
        f"FROM {LEDGER} l LEFT JOIN {TABLE} o ON {on} AND o.status = ? AND o.model_version = ? "
        f"AND o.horizon_days IN ({hz}) WHERE l.target_type = ? GROUP BY {cols} "
        "HAVING count(o.horizon_days) < ? ORDER BY l.subject, l.source, l.forecast_date",
        [CLOSED, MODEL_VERSION, *HORIZONS, PICK, len(HORIZONS)])
    return [{**r, "closed": tuple(r["closed"] or ())} for r in rows]


def newest_bar() -> date | None:
    """The newest session the prices store holds a bar for (None: no bars at all)."""
    return market_bars.newest_session()


def _bars(tickers: list[str], start: date) -> dict[str, list[dict]]:
    frame = market_bars.daily_bars(tickers, basis="nominal", start=start)
    out: dict[str, list[dict]] = {}
    if frame.empty:
        return out
    for t, d, o, h, lo, c in zip(frame["ticker"], frame["session_date"], frame["open"], frame["high"], frame["low"],
                                 frame["close"]):
        out.setdefault(t, []).append({"session_date": d.date(), "open": o, "high": h, "low": lo, "close": c})
    return out


COLUMNS = [*_KEY, "horizon_days", "ref_price", "mfe", "mae", "sessions_seen", "status", "computed_at",
           "model_version", "run_id"]


def upsert(con, table: str, columns: list[str], key: list[str], rows: list[dict]) -> int:
    """Replace the rows of ``table`` in ``con`` that share a key with ``rows``, then insert ``rows``; the count."""
    if not rows:
        return 0
    import pandas as pd
    frame = pd.DataFrame([{c: r.get(c) for c in columns} for r in rows], columns=columns)
    con.register("_upsert_rows", frame)
    try:
        on = " AND ".join(f't."{k}" IS NOT DISTINCT FROM n."{k}"' for k in key)
        con.execute(f'DELETE FROM "{table}" t WHERE EXISTS (SELECT 1 FROM _upsert_rows n WHERE {on})')
        cols = ", ".join(f'"{c}"' for c in columns)
        con.execute(f'INSERT INTO "{table}" ({cols}) SELECT {cols} FROM _upsert_rows')
    finally:
        con.unregister("_upsert_rows")
    return len(rows)


def run(con, *, now: datetime | None = None, run_id: str | None = None, full: bool = False,
        write: bool = True) -> dict:
    """Compute the outcome rows of every ledger pick in ``con`` and upsert them there; returns a summary.

    ``con``: a DuckDB connection holding ``forecast_ledger`` and ``forecast_outcomes`` (the builder's build file).
    ``full=False`` (each daily run) skips the closed rows of the current ``MODEL_VERSION``; ``write=False`` computes and
    counts only. ``now`` is the clock (default: ``calendar``'s seam); ``computed_at`` is its epoch.
    """
    t0 = time.monotonic()
    latest = calendar.latest_complete_session(now)
    stamp = (now or calendar.now_ny()).timestamp()
    picks = _pick_count(con)
    todo: dict[str, list[tuple[dict, int]]] = {}
    for p in _todo_picks(con, full):
        for h in HORIZONS:
            if h not in p["closed"]:
                todo.setdefault(str(p["subject"]), []).append((p, h))
    skipped = max(picks * len(HORIZONS) - sum(len(v) for v in todo.values()), 0)
    if any(p["forecast_date"] < latest for v in todo.values() for p, _ in v) and newest_bar() is None:
        raise RuntimeError("no daily bars in the prices store: refusing to compute outcomes "
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
            # One grouped read per pick rather than one per (pick, horizon): the horizons still open for that date
            # are bucketed together in a single pass over the series, and each horizon gets the columns ``outcome``
            # would have returned for it. Rows stay in ``todo`` order, the order the per-horizon loop wrote them in.
            wanted: dict[date, list[int]] = {}
            for p, h in todo[t]:
                wanted.setdefault(p["forecast_date"], []).append(h)
            got = {d: outcome_by_horizon(d, tuple(hs), series, latest) for d, hs in wanted.items()}
            for p, h in todo[t]:
                row = {**{k: p[k] for k in _KEY}, "horizon_days": h, **got[p["forecast_date"]][h],
                       "computed_at": stamp, "model_version": MODEL_VERSION, "run_id": run_id}
                status[row["status"]] += 1
                rows.append(row)
        rows_total += len(rows)
        if write and rows:
            written += upsert(con, TABLE, COLUMNS, [*_KEY, "horizon_days"], rows)
    res = {"run_id": run_id, "latest_session": latest.isoformat(), "picks": picks, "rows": rows_total,
           "skipped_closed": skipped, "written": written, "status": dict(status),
           "tickers": len(tickers), "full": full, "dry_run": not write,
           "seconds": round(time.monotonic() - t0, 2)}
    logger.info("forecast outcomes: %s", res)
    return res
