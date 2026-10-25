"""Forecast-ledger writer (#128): the one place that writes ``forecast_ledger``.

The ledger is the shared, append-only record of what each board forecast on a day (#62 D-3).
A board *freezes* its shown picks once per session, after that session's close, so a later
scorer can judge them against what happened next. The table and its key live in
``core/rawstore.py`` (schema 8); this module owns the rules for when a freeze is allowed and
how rows reach the table.

Rules (decisions D1 / 4-A):
- Only a Fetch freezes. Refresh (``no_fetch=True``) never reaches this module.
- The market data behind a freeze is one complete New York session D, and D is the latest
  complete session when the freeze runs (``core.calendar``: 30 minutes after the 16:00 ET close).
- A freeze never runs inside the US trading window (09:30-16:30 ET): that run would mix
  unfinished and finished bars.
- The payload must be real (not a fixture, not "no data", not empty).
- All or nothing: every shown pick must have one complete D bar in the OHLC cache. One pick without it
  refuses the whole board for the day, and the note says which picks and why (see ``ohlc_gaps``). A
  partial board would be a different, silently thinner forecast than the one the user saw.
- SPY anchors the Swing and Fundamentals boards: its complete D bar comes from D01 when D01 has it (U1, #402),
  else from the OHLC cache (``spy_anchor_problem``).
- Bars cached before D's close + 30 min are refetched, not skipped (option A, avo 2026-10-03). The refetch
  happens when the board loads its bars (``load_ohlc``, Fetch only), so the board and its frozen picks
  come from the same complete bars; a pick that is still partial here (a failed refetch) refuses the day.
- The first freeze of a (source, day) wins. The table key includes ``subject``, so a second run
  with another candidate set would add its new tickers and turn the day into a union of two
  runs; ``write_picks`` therefore checks the whole (source, day) before writing.

The writer calls ``rawstore.write_rows`` and ``rawstore.query`` only, and never ``update_columns``,
``replace_all``, ``delete`` or ``clear``: rawstore does not refuse those for this table, so the
append-only guarantee is this module's discipline (a test pins it).

``forecast_date`` and ``target_date`` are always ``datetime.date`` (``rawstore._dedupe`` keys on the
Python value, so text, ``datetime`` and ``date`` would be three different keys).

Dated forecasts (S3b, #38; avo's cards 2026-10-25): a board that forecasts one subject for a later session writes
one row per (source, day, ``target_type``) through ``write_dated`` / ``freeze_dated``, with ``target_date`` after
``forecast_date`` and the forecast itself in ``probs``. The first family is SPX Gamma's weekly three-class
scenario (``module_spx_gamma/freeze.py``). The session rule is the picks' one: only the latest complete session,
never inside the US session window, only a Fetch, first write of (source, day, target_type) wins. Its outcome is
not in ``forecast_outcomes``: the SPX Brier score stays in ``brier.json`` (avo, 2026-10-25).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date

from RevenueChaser.core import calendar, rawstore

logger = logging.getLogger(__name__)

TABLE = "forecast_ledger"
PICK = "pick"

# One lock around "is this (source, day) already frozen?" + the write. A Swing job (short lane)
# and the universe job (long lane) can run at the same time.
_LOCK = threading.Lock()


# ── notes: what a freeze says about itself, per worker thread ────────────────

_NOTES = threading.local()


def add_note(text: str) -> None:
    """Record one freeze outcome sentence for the job running on this thread.

    Runners must not raise ``JobMessage`` for a skipped freeze: ``run_universe_refresh`` runs
    three families in a row and the exception would stop the next one. The note travels here
    instead, and ``app.jobs`` (``_with_freeze_notes``) turns it into an entry of the job's own ``notes`` list
    (served by ``/api/jobs``) and a ``forecast ledger:`` line in the Server log. It never becomes the job's
    ``message``, because the dashboard opens a modal alert for any job that has one.
    """
    if not hasattr(_NOTES, "items"):
        _NOTES.items = []
    _NOTES.items.append(text)


def take_notes() -> list[str]:
    """Return this thread's notes in order and clear them."""
    items = getattr(_NOTES, "items", [])
    _NOTES.items = []
    return items


# ── outcome ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Outcome:
    """What one board's freeze did. ``frozen == 0`` means nothing was written; ``reason`` says why."""
    source: str
    label: str
    frozen: int = 0
    forecast_date: date | None = None
    reason: str | None = None
    dropped: int = 0
    dropped_why: str = ""

    @property
    def message(self) -> str:
        if self.frozen:
            text = f"Froze {self.frozen:,} {self.label} rows for {self.forecast_date}"
            if self.dropped:
                why = f": {self.dropped_why}" if self.dropped_why else ""
                text += f" ({self.dropped:,} left out{why})"
            return text + "."
        return f"{self.label} not frozen: {self.reason}."


def skipped(source: str, label: str, reason: str, forecast_date: date | None = None) -> Outcome:
    return Outcome(source, label, reason=reason, forecast_date=forecast_date)


# ── when may a freeze run? ───────────────────────────────────────────────────

def session_gate(now=None) -> tuple[date | None, str | None]:
    """``(D, None)`` when a freeze may run now, else ``(None, reason)``.

    D is the latest complete New York session. Outside the holiday table the calendar raises;
    that becomes a reason, never a guess.
    """
    try:
        if calendar.in_us_session_window(now):
            return None, "inside the US session window (09:30-16:30 ET), when bars are unfinished"
        return calendar.latest_complete_session(now), None
    except ValueError as exc:
        return None, f"the market calendar cannot place this run ({exc})"


def payload_problem(payload: dict, rows_key: str, *, check_spy: bool = True) -> str | None:
    """Why a board payload must not be frozen, or ``None`` when it is real.

    ``check_spy=False`` is for a payload without the ``spy_stale`` flag (the AG board).
    """
    if not isinstance(payload, dict):
        return "no payload"
    if payload.get("is_fixture"):
        return "the payload is fixture data"
    if payload.get("no_data"):
        return "the run reported no data"
    if not payload.get(rows_key):
        return "the board has no candidates"
    if check_spy and (payload.get("data_quality") or {}).get("spy_stale"):
        return "SPY's bars were stale, so the board's baseline is stale"
    return None


# ── is the daily-bar cache complete for session D? ───────────────────────────

def last_bars(tickers) -> dict[str, tuple[str, float | None]]:
    """``{ticker: (last bar day 'YYYY-MM-DD', cached_at of that bar)}`` from the shared OHLC cache."""
    tickers = sorted({str(t).strip().upper() for t in tickers if t})
    if not tickers:
        return {}
    rows = rawstore.query(
        "SELECT ticker, max(substr(ts, 1, 10)) AS last_day, arg_max(cached_at, ts) AS cached_at "
        "FROM swing_ohlc WHERE ticker IN (SELECT unnest(?)) GROUP BY ticker", [tickers])
    return {r["ticker"]: (r["last_day"], r["cached_at"]) for r in rows}


# Why a shown pick has no complete D bar (the values of ``ohlc_gaps``). Only the first can be fixed by
# running a Fetch again (after Daily Bars, for D01), and the note says so for that cause only.
NEEDS_REFETCH = "needs_refetch"        # the last bar is D or older and was written before D's close + 30 min
                                       # (on D01: Daily Bars has not written D's SPY bar yet)
NO_SESSION_BAR = "no_session_bar"      # the bars are post-close, yet there is no D bar: a halt, a vendor gap, no bars
                                       # (on D01: SPY has its D bar and the pick has none)


def needs_session_refetch(last_day, cached_at, session: date | None = None) -> bool:
    """True when a cached daily series ends on ``session`` or before and was written before its close + 30 min.

    Such a series cannot hold the complete D bar, whatever its age, so a Fetch after the close has to
    refetch it. Two shapes: a series that ends before D (the 1-day TTL alone called it fresh when it was
    under 24 h old, e.g. written 23 h 57 min before a 17:00 ET run: #128's F1) and a series whose D bar
    is a partial one, cached by a mid-session Fetch (option A of #128, chosen by avo on 2026-10-03). A
    series written *after* close + 30 min whose last bar is older is a name with no newer bar (halted,
    delisted): refetching it only spends a request. A bar later than D (the clock seam only) is no
    reason to refetch for D.

    ``last_day`` is a date or a timestamp text (only its first ten characters are read: plain date
    comparison, never the calendar, so fixtures from 2024 work). ``session`` defaults to the latest
    complete session on the clock seam, so a mid-session run (D is yesterday) leaves today's partial bar
    alone. A clock past the holiday table makes the calendar raise; the rule then steps aside (False)
    and the caller's TTL alone decides.
    """
    try:
        if session is None:
            session = calendar.latest_complete_session()
        if str(last_day)[:10] > session.isoformat():
            return False
        return not calendar.bars_complete(session, cached_at or 0)
    except ValueError:
        return False


def _norm(ticker) -> str:
    return str(ticker or "").strip().upper()


def _bar_cause(session: date, last_bar) -> str | None:
    """``None`` when ``last_bar`` (``(last_day, cached_at)`` or ``None``) is one complete ``session`` bar."""
    if last_bar is None:
        return NO_SESSION_BAR
    last_day, cached_at = last_bar
    if last_day == session.isoformat() and cached_at is not None and calendar.bars_complete(session, cached_at):
        return None
    return NEEDS_REFETCH if needs_session_refetch(last_day, cached_at, session) else NO_SESSION_BAR


def ohlc_gaps(session: date, tickers) -> dict[str, str]:
    """``{ticker: cause}`` for each ticker without one complete ``session`` bar; complete tickers are absent.

    The bars are D01's (``d01_last_bars``; K8b, #383 R3): Swing and Fundamentals score every pick from D01 since C12,
    so the freeze and the history snapshot check the bars the board scored from. A failed D01 read is no bars: every
    pick is a gap and the day is refused, never frozen on bars nobody checked. A pick without its ``session`` bar is
    ``NEEDS_REFETCH`` only while D01 lacks SPY's complete ``session`` bar (Daily Bars has not run); after that it is
    ``NO_SESSION_BAR`` (S2, #434 review).
    """
    wanted = sorted({_norm(t) for t in tickers if _norm(t)})
    bars = d01_last_bars([*wanted, "SPY"])
    # D01 is append-only: a pick's newest bar carries its own session's ``fetched_at``, never a later one, so the bar
    # alone cannot say whether a later Fetch helps (S2, #434 review). SPY's does: once Daily Bars has written SPY's
    # complete ``session`` bar, a pick without its own will not get one from another Fetch.
    written = _bar_cause(session, bars.get("SPY")) is None
    gaps = {}
    for ticker in wanted:
        last = bars.get(ticker)
        cause = _bar_cause(session, last)
        if cause == NEEDS_REFETCH and written and last[0] < session.isoformat():
            cause = NO_SESSION_BAR          # a bar before the session; a partial session bar still needs a refetch
        if cause:
            gaps[ticker] = cause
    return gaps


def ohlc_complete(session: date, tickers) -> set[str]:
    """Tickers whose newest D01 bar is ``session`` and was written after its close + 30 min."""
    wanted = {_norm(t) for t in tickers if _norm(t)}
    return wanted - set(ohlc_gaps(session, wanted))      # a ticker with no bars at all is a gap too


# D01, the daily bars RC-Daily-Bars writes (``core/prices.py``, #156): one row per session, written once, after the
# session's close + 30 min. It lives in its own file (prices.duckdb).
_D01 = "price_bars_daily"


def d01_last_bars(tickers) -> dict[str, tuple[str, float | None]]:
    """``{ticker: (last session 'YYYY-MM-DD', fetched_at of that bar)}`` from D01: the shape of ``last_bars``.

    A store without the price file or the table reads as no bars, and so does a failed read (logged): ``ohlc_gaps``
    then refuses every pick, and ``spy_anchor_problem`` decides SPY on ``swing_ohlc`` (the board's SPY fallback).
    """
    tickers = sorted({_norm(t) for t in tickers if _norm(t)})
    if not tickers:
        return {}
    try:
        rows = rawstore.query(
            "SELECT ticker, max(session_date) AS last_day, arg_max(fetched_at, session_date) AS fetched_at "
            f"FROM {_D01} WHERE ticker IN (SELECT unnest(?)) GROUP BY ticker", [tickers],
            db=rawstore.MANIFEST[_D01].get("db", "raw"))
    except Exception as exc:  # noqa: BLE001 - no D01 means the old check, never a failed freeze
        logger.warning("forecast ledger: the D01 bars could not be read (%s)", exc)
        return {}
    return {r["ticker"]: (str(r["last_day"])[:10], r["fetched_at"]) for r in rows if r.get("last_day") is not None}


def d01_has_session(session: date, ticker: str) -> bool:
    """True when D01's last bar of ``ticker`` is ``session`` and was written after its close + 30 min."""
    return _bar_cause(session, d01_last_bars([ticker]).get(_norm(ticker))) is None


def spy_anchor_problem(session: date) -> str | None:
    """Why SPY's bars are not one complete ``session``, or ``None``. SPY is the board anchor.

    D01 first (U1, #402; K8b's SPY part): a complete ``session`` SPY bar in D01 anchors the board. That is the test the
    Swing and Fundamentals baseline uses (``screen_common.load_spy``), so a board whose SPY came from D01 is never
    refused for SPY's cache. Only when D01 lacks that bar does the ``swing_ohlc`` check below decide, with its old
    reasons (the board's SPY fallback, ``load_ohlc``, reads that cache). The shown picks' own bars are D01's
    (``ohlc_gaps``): the board scores them from D01 since C12.
    """
    if d01_has_session(session, "SPY"):
        return None
    bar = last_bars(["SPY"]).get("SPY")
    if bar is None:
        return "no SPY bars in the store"
    last_day, cached_at = bar
    if last_day != session.isoformat():
        return f"SPY's last bar is {last_day}, not the {session} session"
    if cached_at is None or not calendar.bars_complete(session, cached_at):
        return (f"the {session} bars were cached before the close + 30 min "
                "(an intraday Fetch; the next Fetch refetches them)")
    return None


# ── model version ────────────────────────────────────────────────────────────

def model_version(selection_version, config: dict) -> str:
    """``"<SELECTION_VERSION>+<8 hex of a hash of the effective config>"``.

    The config hash is order-free. Editing a threshold changes it, so a golden test in each board
    fails until ``SELECTION_VERSION`` is bumped by hand in the same change.
    """
    blob = json.dumps(config, sort_keys=True, separators=(",", ":"), default=repr)
    return f"{selection_version}+{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:8]}"


def new_run_id() -> str:
    """A fresh run id, except under a scheduled task: there it is the task's ``job_runs`` id (``rawstore.task_run``,
    #129 A1), so every write of the run joins that row."""
    task = rawstore.task_run()
    return task[0] if task else uuid.uuid4().hex[:12]


# ── the writer ───────────────────────────────────────────────────────────────

def _num(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _text(value) -> str | None:
    return None if value is None else str(value)


def _day_frozen(source: str, forecast_date: date) -> bool:
    rows = rawstore.query(
        f"SELECT 1 AS hit FROM {TABLE} WHERE source = ? AND forecast_date = ? AND target_type = ? LIMIT 1",
        [source, forecast_date, PICK])
    return bool(rows)


def write_picks(source: str, label: str, rows: list[dict], *, model_version: str,
                forecast_date: date, run_id: str, frozen_at: float | None = None) -> Outcome:
    """Append one day of picks for ``source``; the first freeze of (source, day) wins.

    ``rows`` carry ``subject``, ``rank`` (1-based), ``score``, ``bucket``, ``market_cap``,
    ``sector``, ``shown_price``, ``stop``, ``target``; the rest of the ledger row is filled here.
    Returns an ``Outcome``: ``frozen == 0`` with a reason when the day was already frozen or
    nothing usable was handed in. Raises on a bad ``forecast_date`` or ``rank`` (a code error).
    """
    if type(forecast_date) is not date:
        raise TypeError(f"forecast_date must be a datetime.date, got {type(forecast_date).__name__}")
    stamp = time.time() if frozen_at is None else float(frozen_at)

    built: dict[str, dict] = {}
    duplicates = 0
    for r in rows:
        subject = str(r.get("subject") or "").strip().upper()
        if not subject:
            continue
        rank = r.get("rank")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise ValueError(f"rank is 1-based; got {rank!r} for {subject}")
        if subject in built:
            duplicates += 1                   # first one wins, as the table's DO NOTHING would
            continue
        built[subject] = {
            "source": source, "forecast_date": forecast_date, "subject": subject,
            "target_type": PICK, "target_date": forecast_date,
            "frozen_at": stamp, "run_id": run_id, "model_version": model_version,
            "rank": rank, "score": _num(r.get("score")), "bucket": _text(r.get("bucket")),
            "probs": None, "market_cap": _num(r.get("market_cap")), "sector": _text(r.get("sector")),
            "shown_price": _num(r.get("shown_price")), "stop": _num(r.get("stop")),
            "target": _num(r.get("target")),
        }
    if not built:
        return skipped(source, label, "there were no usable picks", forecast_date)

    with _LOCK:
        if _day_frozen(source, forecast_date):
            return skipped(source, label, f"already frozen for {forecast_date}", forecast_date)
        rawstore.write_rows(TABLE, list(built.values()))
    return Outcome(source, label, frozen=len(built), forecast_date=forecast_date,
                   dropped=duplicates, dropped_why="duplicate ticker" if duplicates else "")


# ── dated forecasts (S3b) ────────────────────────────────────────────────────

def _dated_frozen(source: str, forecast_date: date, target_type: str) -> bool:
    rows = rawstore.query(
        f"SELECT 1 AS hit FROM {TABLE} WHERE source = ? AND forecast_date = ? AND target_type = ? LIMIT 1",
        [source, forecast_date, target_type])
    return bool(rows)


def write_dated(source: str, label: str, row: dict, *, target_type: str, forecast_date: date,
                target_date: date, model_version: str, run_id: str, frozen_at: float | None = None) -> Outcome:
    """Append one dated forecast for ``source``; the first write of (source, day, ``target_type``) wins.

    ``row`` carries ``subject``, ``probs`` (a dict, the JSON column) and optionally ``shown_price``, ``score`` and
    ``bucket``; ``rank``, ``stop`` and ``target`` stay NULL (they belong to picks). Raises on a code error: a text
    date, the ``pick`` type or none, or a ``target_date`` that is not after ``forecast_date``.
    """
    for name, value in (("forecast_date", forecast_date), ("target_date", target_date)):
        if type(value) is not date:
            raise TypeError(f"{name} must be a datetime.date, got {type(value).__name__}")
    if not target_type or target_type == PICK:
        raise ValueError(f"a dated forecast needs its own target_type, got {target_type!r}")
    if target_date <= forecast_date:
        raise ValueError(f"target_date {target_date} must be after forecast_date {forecast_date}")
    subject = str(row.get("subject") or "").strip().upper()
    if not subject:
        return skipped(source, label, "there was no usable forecast", forecast_date)
    probs = row.get("probs")
    built = {
        "source": source, "forecast_date": forecast_date, "subject": subject,
        "target_type": target_type, "target_date": target_date,
        "frozen_at": time.time() if frozen_at is None else float(frozen_at), "run_id": run_id,
        "model_version": model_version, "rank": None, "score": _num(row.get("score")),
        "bucket": _text(row.get("bucket")),
        "probs": None if probs is None else dict(probs),      # rawstore serializes a JSON column once
        "market_cap": None, "sector": None, "shown_price": _num(row.get("shown_price")),
        "stop": None, "target": None,
    }
    with _LOCK:
        if _dated_frozen(source, forecast_date, target_type):
            return skipped(source, label, f"already frozen for {forecast_date}", forecast_date)
        rawstore.write_rows(TABLE, [built])
    return Outcome(source, label, frozen=1, forecast_date=forecast_date)


def freeze_dated(source: str, label: str, row: dict, *, target_type: str, forecast_date: date,
                 target_date: date, model_version: str, run_id: str | None = None, now=None,
                 problem: str | None = None) -> Outcome:
    """Apply the session rule to one dated forecast, write it when it passes, and note the outcome.

    ``forecast_date`` must be the latest complete New York session at ``now`` and the run must be outside the US
    session window (``session_gate``). ``problem`` is the caller's own reason to refuse (no bands, bad
    probabilities). Never raises for a refusal; a code error propagates, as in ``freeze_board``.
    """
    session, why = session_gate(now)
    if not why and forecast_date != session:
        why = f"{forecast_date} is not the latest complete session ({session})"
    if not why and problem:
        why = problem
    if why:
        outcome = skipped(source, label, why, forecast_date)
    else:
        outcome = write_dated(source, label, row, target_type=target_type, forecast_date=forecast_date,
                              target_date=target_date, model_version=model_version,
                              run_id=run_id or new_run_id())
    add_note(outcome.message)
    logger.info("forecast ledger: %s", outcome.message)
    return outcome


# ── one board, the whole rule ────────────────────────────────────────────────

def freeze_board(source: str, label: str, payload: dict, *, rows_key: str, to_rows, model_version: str,
                 run_id: str | None = None, now=None, check_spy: bool = True, payload_check=None,
                 anchor=None, bar_gaps=None) -> Outcome:
    """Apply the freeze rule to one board's payload, write what passes, and note the outcome.

    ``to_rows(items)`` maps the board's shown items to ledger picks (the mapping stays in each
    module). ``payload_check(payload)`` and ``anchor(session)`` return a reason string to refuse
    the freeze, else ``None`` (the AG board's own guards; SPY's bars for the OHLC boards).
    ``bar_gaps(session, subjects)`` returns ``{subject: cause}`` for the shown subjects whose own data
    is not one complete ``session`` bar (``ohlc_gaps``); any entry refuses the whole board, never a
    part of it. Never raises for a refused freeze: the runners call this after the board files are
    written, and a refusal is a note, not an error. A code error (a bad mapping, a store failure) does
    propagate, so the job shows "error".
    """
    outcome = _freeze_board(source, label, payload, rows_key, to_rows, model_version,
                            run_id or new_run_id(), now, check_spy, payload_check, anchor, bar_gaps)
    add_note(outcome.message)
    logger.info("forecast ledger: %s", outcome.message)
    return outcome


# How each cause reads in a refusal note. Only the first may say a later Fetch can complete the day.
_GAP_TEXT = {
    NEEDS_REFETCH: ("need their {session} bar (none in D01 written past the close + 30 min yet; "
                    "a later Fetch can complete the day once Daily Bars has run)"),
    NO_SESSION_BAR: ("no {session} bar after the close (a halt or a Daily Bars gap; "
                     "a later Fetch will not change it unless Daily Bars is run again)"),
}
_LIST_CAP = 10          # names listed per cause; the rest are counted


def _gap_refusal(session: date, rows: list[dict], bar_gaps) -> str | None:
    """The reason a board is refused for incomplete bars, or ``None`` when every shown pick is complete.

    A ticker shown twice counts once, and is looked up once. Names come in board rank order.
    """
    shown = list(dict.fromkeys(t for t in (_norm(r.get("subject")) for r in rows) if t))
    gaps = bar_gaps(session, shown)
    by_cause: dict[str, list[str]] = {}
    for ticker in shown:
        cause = gaps.get(ticker)
        if cause:
            by_cause.setdefault(cause, []).append(ticker)
    if not by_cause:
        return None
    parts = []
    for cause in [c for c in _GAP_TEXT if c in by_cause] + [c for c in by_cause if c not in _GAP_TEXT]:
        names = by_cause[cause]
        listed = ", ".join(names[:_LIST_CAP])
        if len(names) > _LIST_CAP:
            listed += f" +{len(names) - _LIST_CAP} more"
        text = _GAP_TEXT.get(cause, "no complete {session} bar").format(session=session)
        parts.append(f"{text}: {listed}")
    count = sum(len(v) for v in by_cause.values())
    return f"{count:,} of {len(shown):,} picks lack a complete {session} daily bar; " + "; ".join(parts)


def _freeze_board(source, label, payload, rows_key, to_rows, model_version, run_id, now, check_spy,
                  payload_check, anchor, bar_gaps) -> Outcome:
    session, why = session_gate(now)
    if why:
        return skipped(source, label, why)
    why = payload_problem(payload, rows_key, check_spy=check_spy)
    if not why and payload_check is not None:
        why = payload_check(payload)
    if not why and anchor is not None:
        why = anchor(session)
    if why:
        return skipped(source, label, why, session)

    rows = to_rows(payload[rows_key])
    if bar_gaps is not None:
        why = _gap_refusal(session, rows, bar_gaps)
        if why:
            return skipped(source, label, why, session)
    return write_picks(source, label, rows, model_version=model_version,
                       forecast_date=session, run_id=run_id)
