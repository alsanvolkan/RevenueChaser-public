"""Quotes and market cap per session (D04, #294): the writer of ``quote_snapshot`` and its read side.

One row per ``(ticker, New York session, source)``: the market cap, price, share count and 52-week range a vendor
served for that session, with the vendor's whole row in ``payload`` (compressed JSON). The table is ``append``, so the
first write of a key wins and a session's row is never rewritten; a later session adds its own row, which is the
history ``equity_universe`` (one overwritten value per ticker) cannot keep.

Two sources, Yahoo first (avo, phase 2 decision 2.6-2):

- ``yfinance``: written by the universe pull itself (``core/universe_fetch.py``). Phase A records the screener quotes
  it got (:func:`record_yahoo`), Phase B the caps it filled from ``fast_info`` (:func:`record_fast_info`).
- ``uw``: written by :func:`ensure_quote_snapshot`, UW's stock screener in batches of 100 over the universe, which the
  universe job runs after the pull (``app/runners.run_universe_refresh``).

A row needs a positive cap: a quote without one writes nothing, so a later run of the same session (or Phase B) can
still fill it, where a stored empty row could never be replaced. A run inside the session (09:30-16:30 New York)
writes nothing: its values are intraday, and under first write wins they would shadow the session's closing values.
The same holds for a run that crosses a session boundary: the session is taken when the run starts and checked again
at every write, and a run whose session changed in between writes nothing more (``"session_moved"``). So a UW run
started at 09:29 never writes a batch answered at 09:31, and a pull started at 16:20 never writes after 16:30.

The writers never raise: a failure is a count or an error name in the result, so the universe pull that calls them
never fails because of this table. :func:`latest_caps` is the quote half of the D6 current view that #279 builds.
Swing and Fundamentals prefilter on this table (:func:`prefilter_quotes`, #383 R3 C12); the other boards still read
``equity_universe``.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime

from RevenueChaser.core import calendar, job_control, rawstore
from RevenueChaser.core.identity.sources import norm_ticker

logger = logging.getLogger(__name__)

TABLE = "quote_snapshot"
SOURCE_YAHOO = "yfinance"
SOURCE_UW = "uw"
SOURCES = (SOURCE_YAHOO, SOURCE_UW)      # read order: Yahoo first, UW where Yahoo has no row (2.6-2)
UW_BATCH = 100                           # tickers per UW screener call (core/providers/unusual_whales.py)
CAP_GAP_PCT = 5                          # Data Health lists caps more than this many percent apart
GAP_EXAMPLES = 5
# Phase B's last stop on Yahoo's rate limit: one source_checks stamp (dataset, source, key).
RATE_LIMIT_CHECK = (TABLE, SOURCE_YAHOO, "__rate_limited__")


# ── small helpers ────────────────────────────────────────────────────────────

def _now(now: datetime | None) -> datetime:
    """The run clock: ``now`` when given, else the calendar seam (never ``datetime.now``)."""
    return now if now is not None else calendar._now()


def _f(v) -> float | None:
    """A finite float, else None (a string number, NaN and None all become None)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pos(v) -> float | None:
    x = _f(v)
    return x if x is not None and x > 0 else None


def _placeholders(n: int) -> str:
    return ", ".join("?" for _ in range(n))


def _result(session: date | None, skipped: str | None) -> dict:
    return {"session": session.isoformat() if session else None, "skipped": skipped, "offered": 0,
            "no_cap": 0, "rows_inserted": 0, "errors": []}


def snapshot_session(now: datetime | None = None) -> tuple[date | None, str | None]:
    """``(session, None)`` for the New York session a run at ``now`` writes, else ``(None, reason)``.

    Rule B (``calendar.session_for``): after 16:30 New York the day's own session, before 09:30 or on a closed day the
    previous one. Inside the session the reason is ``"intraday"``; a clock outside the holiday table is
    ``"calendar"``.
    """
    try:
        stamp = calendar.session_for(_now(now))
    except ValueError:
        return None, "calendar"
    if stamp.quality != calendar.QUALITY_COMPLETE:
        return None, "intraday"
    return stamp.session_date, None


def _row(ticker: str, session: date, source: str, *, cap, price, shares, high, low, payload,
         fetched_at: float, run_id: str, run_at: float) -> dict:
    return {"ticker": ticker, "session_date": session, "source": source, "market_cap": cap,
            "price": _f(price), "shares_outstanding": _f(shares), "week_52_high": _f(high),
            "week_52_low": _f(low), "payload": payload, "fetched_at": fetched_at, "run_id": run_id,
            "run_at": run_at}


# ── row builders (pure) ──────────────────────────────────────────────────────

def rows_from_yahoo(quoted: list[dict], *, session: date, fetched_at: float, run_id: str,
                    run_at: float) -> tuple[list[dict], int]:
    """``(rows, no_cap)`` from the screener rows ``[{"ticker", "quote"}]`` of one pull."""
    rows, no_cap = [], 0
    for r in quoted:
        q = r.get("quote") or {}
        t = norm_ticker(r.get("ticker") or q.get("symbol"))
        if not t:
            continue
        cap = _pos(q.get("marketCap"))
        if cap is None:
            no_cap += 1
            continue
        rows.append(_row(t, session, SOURCE_YAHOO, cap=cap, price=q.get("regularMarketPrice"),
                         shares=q.get("sharesOutstanding"), high=q.get("fiftyTwoWeekHigh"),
                         low=q.get("fiftyTwoWeekLow"), payload=q, fetched_at=fetched_at, run_id=run_id,
                         run_at=run_at))
    return rows, no_cap


def rows_from_fast_info(fills: list[dict], *, session: date, fetched_at: float, run_id: str,
                        run_at: float) -> tuple[list[dict], int]:
    """``(rows, no_cap)`` from Phase B's ``[{"ticker", "shares", "price"}]``: cap = shares × last price."""
    rows, no_cap = [], 0
    for r in fills:
        t = norm_ticker(r.get("ticker"))
        shares, price = _pos(r.get("shares")), _pos(r.get("price"))
        if not t or shares is None or price is None:
            no_cap += 1
            continue
        rows.append(_row(t, session, SOURCE_YAHOO, cap=shares * price, price=price, shares=shares, high=None,
                         low=None, payload={"via": "fast_info", "shares": shares, "last_price": price},
                         fetched_at=fetched_at, run_id=run_id, run_at=run_at))
    return rows, no_cap


def rows_from_uw(items: dict, asked: list[str], *, session: date, fetched_at: float, run_id: str,
                 run_at: float) -> tuple[list[dict], int, int]:
    """``(rows, no_cap, not_returned)`` from UW screener rows for the tickers ``asked`` (universe spelling)."""
    by_ticker = {}
    for key, item in (items or {}).items():
        t = norm_ticker((item or {}).get("ticker") or key)
        if t:
            by_ticker.setdefault(t, item)
    rows, no_cap, missing = [], 0, 0
    for t in asked:
        item = by_ticker.get(t)
        if item is None:
            missing += 1
            continue
        cap = _pos(item.get("marketcap"))
        if cap is None:
            no_cap += 1
            continue
        rows.append(_row(t, session, SOURCE_UW, cap=cap, price=item.get("close"), shares=None,
                         high=item.get("week_52_high"), low=item.get("week_52_low"), payload=item,
                         fetched_at=fetched_at, run_id=run_id, run_at=run_at))
    return rows, no_cap, missing


# ── writer ───────────────────────────────────────────────────────────────────

def _stored(session: date, source: str, tickers: list[str]) -> set[str]:
    out: set[str] = set()
    for i in range(0, len(tickers), 1000):
        part = tickers[i:i + 1000]
        got = rawstore.query(
            f"SELECT ticker FROM {TABLE} WHERE session_date = ? AND source = ? "
            f"AND ticker IN ({_placeholders(len(part))})", [session, source, *part])
        out.update(r["ticker"] for r in got)
    return out


def write_snapshot(rows: list[dict]) -> int:
    """Append ``rows``; returns how many keys were new. A stored key keeps its row (first write wins)."""
    if not rows:
        return 0
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["session_date"], r["source"]), []).append(r)
    fresh: list[dict] = []
    for (session, source), group in groups.items():
        stored = _stored(session, source, sorted({r["ticker"] for r in group}))
        seen: set[str] = set()
        for r in group:
            if r["ticker"] in stored or r["ticker"] in seen:
                continue
            seen.add(r["ticker"])
            fresh.append(r)
    if fresh:
        rawstore.write_rows(TABLE, fresh)
    return len(fresh)


def _write_session(started: tuple | None, now: datetime | None) -> tuple[date | None, str | None]:
    """The session a write at ``now`` may use: :func:`snapshot_session`, and ``(None, "session_moved")`` when it is
    not the one the run started in (``started``, the run's :func:`snapshot_session` at its start)."""
    current = snapshot_session(now)
    if started is not None and tuple(started) != current:
        return None, "session_moved"
    return current


def _record(build, items: list[dict], started: tuple | None, now: datetime | None, run_id: str | None) -> dict:
    """Build and write one batch of Yahoo rows; never raises."""
    session, why = _write_session(started, now)
    res = _result(session, why)
    res["offered"] = len(items)
    if session is None or not items:
        return res
    try:
        at = _now(now).timestamp()
        rows, res["no_cap"] = build(items, session=session, fetched_at=at, run_id=run_id or rawstore.writer_run_id(),
                                    run_at=at)
        res["rows_inserted"] = write_snapshot(rows)
    except Exception as exc:  # noqa: BLE001 - the universe pull must never fail on this table
        logger.warning("quote_snapshot write failed: %s", exc)
        res["errors"].append("write")
    return res


def record_yahoo(quoted: list[dict], *, started: tuple | None = None, now: datetime | None = None,
                 run_id: str | None = None) -> dict:
    """Phase A: one ``yfinance`` row per screener quote ``[{"ticker", "quote"}]`` that carries a cap.

    ``started`` is :func:`snapshot_session` taken before the first screener call; when the session differs now,
    nothing is written."""
    return _record(rows_from_yahoo, quoted, started, now, run_id)


def record_fast_info(fills: list[dict], *, started: tuple | None = None, now: datetime | None = None,
                     run_id: str | None = None) -> dict:
    """Phase B: one ``yfinance`` row per filled cap ``[{"ticker", "shares", "price"}]``; ``started`` as above."""
    return _record(rows_from_fast_info, fills, started, now, run_id)


def record_rate_limit_stop(*, now: datetime | None = None, run_id: str | None = None) -> None:
    """Stamp Phase B's stop on Yahoo's rate limit, for Data Health; never raises."""
    try:
        dataset, source, key = RATE_LIMIT_CHECK
        rawstore.write_rows("source_checks", [{"dataset": dataset, "source": source, "key": key,
                                               "checked_at": _now(now).timestamp(),
                                               "run_id": run_id or rawstore.writer_run_id()}])
    except Exception as exc:  # noqa: BLE001
        logger.warning("quote_snapshot rate-limit stamp failed: %s", exc)


def _uw_health(uw) -> dict:
    fn = getattr(uw, "health", None)
    if not callable(fn):
        return {}
    try:
        return fn() or {}
    except Exception:  # noqa: BLE001 - reporting a client must never fail the run
        return {}


def _uw_stop(uw) -> str | None:
    if getattr(uw, "auth_error", None):
        return "uw_auth"
    if _uw_health(uw).get("breaker_open"):
        return "uw_breaker"
    return None


def universe_tickers() -> list[str]:
    return [r["ticker"] for r in rawstore.query("SELECT ticker FROM equity_universe ORDER BY ticker")]


def ensure_quote_snapshot(tickers=None, *, uw, now: datetime | None = None, run_id: str | None = None) -> dict:
    """UW's rows for the session: the screener in batches of ``UW_BATCH`` over ``tickers`` (default: the universe).

    Asks only the tickers with no ``uw`` row for the session yet, so a second run of a session makes no call for a
    ticker it already has. Stops before the next batch once the key is rejected or the client's breaker is open.
    Each batch is written as it comes back. The session is checked again before each call and before each write: a
    run that crosses the open (or any session boundary) stops with ``"session_moved"`` and the batch answered after
    it is not written. Never raises, except a job's cancel or deadline (``job_control.checkpoint``) between batches.
    """
    started = snapshot_session(now)
    session, why = started
    res = {**_result(session, why), "scope": 0, "stored": 0, "asked": 0, "not_returned": 0, "uw_calls": 0}
    if session is None:
        return res
    try:
        scope = sorted({norm_ticker(t) for t in tickers} - {""}) if tickers is not None else universe_tickers()
        res["scope"] = len(scope)
        stored = _stored(session, SOURCE_UW, scope) if scope else set()
    except Exception as exc:  # noqa: BLE001
        logger.warning("quote_snapshot scope read failed: %s", exc)
        res["errors"].append("store")
        return res
    res["stored"] = len(stored)
    due = [t for t in scope if t not in stored]
    rid = run_id or rawstore.writer_run_id()
    calls_at_start = int(_uw_health(uw).get("calls") or 0)
    for i in range(0, len(due), UW_BATCH):
        job_control.checkpoint()
        stop = _uw_stop(uw) or _write_session(started, now)[1]
        if stop:
            res["errors"].append(stop)
            break
        batch = due[i:i + UW_BATCH]
        try:
            items = uw.screener_batch(batch)
        except Exception as exc:  # noqa: BLE001 - one failed call ends the UW side, never the job
            logger.warning("UW screener failed: %s", exc)
            res["errors"].append("uw_failed")
            break
        res["asked"] += len(batch)
        moved = _write_session(started, now)[1]
        if moved:                              # answered after the boundary: its values belong to no stored session
            res["errors"].append(moved)
            break
        at = _now(now).timestamp()
        rows, no_cap, missing = rows_from_uw(items, batch, session=session, fetched_at=at, run_id=rid, run_at=at)
        res["no_cap"] += no_cap
        res["not_returned"] += missing
        res["offered"] += len(rows) + no_cap
        try:
            res["rows_inserted"] += write_snapshot(rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning("quote_snapshot UW write failed: %s", exc)
            res["errors"].append("write")
            break
    else:
        stop = _uw_stop(uw) if due else None
        if stop:
            res["errors"].append(stop)
    res["uw_calls"] = int(_uw_health(uw).get("calls") or 0) - calls_at_start
    return res


def summary_line(res: dict) -> str:
    """One human line for the universe job's notes."""
    if res.get("skipped") == "intraday":
        return "Quote snapshot not written: the US session is still open."
    if res.get("skipped") == "calendar":
        return "Quote snapshot not written: the session calendar has no entry for today."
    if res.get("skipped") == "session_moved":
        return "Quote snapshot not written: the US session changed while the run was going."
    line = (f"Quote snapshot {res.get('session')}: UW {res.get('rows_inserted', 0)} new rows "
            f"({res.get('uw_calls', 0)} calls, {res.get('no_cap', 0)} without a cap, "
            f"{res.get('not_returned', 0)} not returned)")
    if res.get("errors"):
        line += f"; stopped: {', '.join(res['errors'])}"
    return line + "."


# ── read side ────────────────────────────────────────────────────────────────

_SOURCE_RANK = "CASE source WHEN 'yfinance' THEN 0 WHEN 'uw' THEN 1 ELSE 2 END"


def latest_caps(tickers=None) -> dict[str, dict]:
    """The current cap per ticker: its newest session's row, Yahoo's where both sources have one (2.6-2).

    ``{ticker: {"market_cap", "price", "source", "session_date"}}``; a ticker with no row is absent. Store only.
    """
    where, params = "", []
    if tickers is not None:
        names = sorted({norm_ticker(t) for t in tickers} - {""})
        if not names:
            return {}
        where, params = f"WHERE ticker IN ({_placeholders(len(names))})", names
    rows = rawstore.query(
        f"SELECT ticker, session_date, source, market_cap, price FROM {TABLE} {where} "
        f"QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY session_date DESC, {_SOURCE_RANK}) = 1", params)
    return {r["ticker"]: {"market_cap": r["market_cap"], "price": r["price"], "source": r["source"],
                          "session_date": r["session_date"]} for r in rows}


def _avg_volume(source: str, payload) -> float | None:
    """The average daily volume a row's vendor payload carries: Yahoo's 10-day, else its 3-month average.

    A UW row and a ``fast_info`` row carry none: ``None``; ``prefilter_quotes`` then looks further back.
    """
    if source != SOURCE_YAHOO or not isinstance(payload, dict):
        return None
    return _pos(payload.get("averageDailyVolume10Day")) or _pos(payload.get("averageDailyVolume3Month"))


VOLUME_LOOKBACK = 10        # Yahoo rows per name searched for an average volume when the newest row has none


def _older_avg_volumes(names: list[str]) -> dict[str, float]:
    """``{ticker: average volume}`` from each name's newest Yahoo row that carries one, among its newest
    ``VOLUME_LOOKBACK`` Yahoo rows (S4, #434 review). The payload is compressed, so the search is in Python."""
    out: dict[str, float] = {}
    for i in range(0, len(names), 1000):
        part = names[i:i + 1000]
        rows = rawstore.query(
            f"SELECT ticker, session_date, source, payload FROM {TABLE} "
            f"WHERE source = ? AND ticker IN ({_placeholders(len(part))}) "
            f"QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY session_date DESC) <= {VOLUME_LOOKBACK} "
            f"ORDER BY ticker, session_date DESC", [SOURCE_YAHOO, *part])
        for r in rows:
            if r["ticker"] in out:
                continue
            r = rawstore._decode_row(TABLE, r)
            volume = _avg_volume(r["source"], r.get("payload"))
            if volume:
                out[r["ticker"]] = volume
    return out


def prefilter_quotes(tickers) -> dict[str, dict]:
    """The screener prefilter's inputs per ticker (#383 R3 C12): price and average daily volume from each ticker's
    newest session row, Yahoo's where both sources have one (the :func:`latest_caps` rule).

    ``{ticker (the caller's spelling): {"price", "avg_volume", "session_date", "source"}}``; a ticker with no row is
    absent. ``price`` is the row's own column; ``avg_volume`` comes from the vendor payload (:func:`_avg_volume`).
    When that row carries none (a UW row, a ``fast_info`` row), it is the newest Yahoo screener row's average
    (:func:`_older_avg_volumes`, S4 of the #434 review), so the dollar-volume rule is not skipped; ``None`` only when
    no recent Yahoo row has one. Store only.
    """
    asked: dict[str, list[str]] = {}
    for t in tickers or ():
        n = norm_ticker(t)
        if n:
            asked.setdefault(n, []).append(t)
    if not asked:
        return {}
    newest: dict[str, dict] = {}
    names = sorted(asked)
    for i in range(0, len(names), 1000):
        part = names[i:i + 1000]
        rows = rawstore.query(
            f"SELECT ticker, session_date, source, price, payload FROM {TABLE} "
            f"WHERE ticker IN ({_placeholders(len(part))}) "
            f"QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY session_date DESC, {_SOURCE_RANK}) = 1", part)
        for r in rows:
            r = rawstore._decode_row(TABLE, r)
            newest[r["ticker"]] = {"price": _f(r["price"]), "avg_volume": _avg_volume(r["source"], r.get("payload")),
                                   "session_date": r["session_date"], "source": r["source"]}
    missing = sorted(t for t, got in newest.items() if got["avg_volume"] is None)
    if missing:
        older = _older_avg_volumes(missing)
        for t in missing:
            newest[t]["avg_volume"] = older.get(t)
    out: dict[str, dict] = {}
    for t, got in newest.items():
        for spelled in asked.get(t, ()):
            out[spelled] = got
    return out


def health_summary() -> dict:
    """The Data Health numbers (#294), store only: the newest session's rows per source against the universe, the
    universe tickers with no row per source, the caps more than ``CAP_GAP_PCT`` percent apart (UW against Yahoo) and
    Phase B's last stop on Yahoo's rate limit."""
    out: dict = {"newest_session": None, "universe": 0, "rows": {s: 0 for s in SOURCES},
                 "missing": {}, "gaps": 0, "gap_examples": [], "rate_limit_stop": None}
    universe = set(universe_tickers())
    out["universe"] = len(universe)
    got = rawstore.query(f"SELECT MAX(session_date) AS m FROM {TABLE}")
    newest = got[0]["m"] if got else None
    have: dict[str, set[str]] = {s: set() for s in SOURCES}
    if newest is not None:
        out["newest_session"] = newest.isoformat()
        for r in rawstore.query(f"SELECT ticker, source FROM {TABLE} WHERE session_date = ?", [newest]):
            if r["source"] in have:
                have[r["source"]].add(r["ticker"])
        out["rows"] = {s: len(have[s]) for s in SOURCES}
        gaps = rawstore.query(
            f"SELECT y.ticker FROM {TABLE} y JOIN {TABLE} u ON u.ticker = y.ticker AND u.session_date = y.session_date "
            f"WHERE y.session_date = ? AND y.source = ? AND u.source = ? "
            f"AND ABS(u.market_cap - y.market_cap) * 100 > ? * y.market_cap "
            f"ORDER BY ABS(u.market_cap - y.market_cap) / y.market_cap DESC, y.ticker",
            [newest, SOURCE_YAHOO, SOURCE_UW, CAP_GAP_PCT])
        out["gaps"] = len(gaps)
        out["gap_examples"] = [r["ticker"] for r in gaps[:GAP_EXAMPLES]]
    out["missing"] = {s: len(universe - have[s]) for s in SOURCES}
    stamp = rawstore.query("SELECT checked_at FROM source_checks WHERE dataset = ? AND source = ? AND key = ?",
                           list(RATE_LIMIT_CHECK))
    out["rate_limit_stop"] = stamp[0]["checked_at"] if stamp else None
    return out
