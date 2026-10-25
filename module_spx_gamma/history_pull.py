"""The 2-year SPX Gamma history pull (decision D1, 25 Oct 2026): past sessions into the raw day folders.

The UW account ends on 6 Dec 2026 and its lookback reaches about two years back (a 403 at three years, a 200 at
two, measured by the coordinator), and that edge moves forward one session a day. This job pulls the past sessions
the board's Fetch never stored, oldest first, so the days closest to the edge go first.

Each session is one ``fetch_snapshot(history_only=True)`` (``HISTORY_CALLS_PER_SESSION`` dated calls) into the same
``raw/<date>/`` folder and manifest a Fetch writes, with ``origin: history_pull``. It is not the 10-day backfill in
``run._backfill_history``: that one asks for every expiry's strike ladder and the live files, which suits a few
recent days and not five hundred past ones.

Nothing is known about UW's rate limit beyond the 150,000 calls a day of the plan, so the job measures instead of
assuming: every run has a call cap (``max_calls``) and a pause between calls (``sleep_s``), and it returns what it
sent and what came back (``counts``, stored in the run's ``job_runs`` row). It stops on the first of:

* the cap: ``max_calls`` bounds the logical calls and, when the client lets them be counted, the HTTP requests
  (the client retries a 429/5xx up to ``MAX_ATTEMPTS`` times, and UW counts requests). A session is started only
  when all of its calls fit; one cut short by the request count is not kept. The last call's own retries can pass
  the cap by at most ``MAX_ATTEMPTS - 1`` requests;
* ``RATE_LIMIT_STREAK_STOP`` final 429s in a row (the universe fetch's rule; the client's own breaker opens on
  five failures of any provider kind and stops the job too);
* ``REJECTED_STREAK_STOP`` sessions in a row whose first call (gex-levels) is rejected with a 4xx other than 429:
  ``auth_streak`` for 401/403 (a dead key, or a range past the lookback edge), ``rejected_streak`` for the rest (a
  date UW will not serve, e.g. 422). Each such day costs one call. So a ``start`` before the lookback edge stops the
  run after five sessions: probe the edge first and start inside it;
* the US session opening: the job never runs inside it, and refuses to start in it.

A day that already has its manifest costs no request and is never replaced; a day with a transient failure is not
kept, so a later run pulls it again. Each run also writes its counts to ``history_pull/run_<start>_<end>_<utc>.json``.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from RevenueChaser.core import calendar as nyse_calendar
from RevenueChaser.core import job_control

from . import safe_json
from .config import DATA_ROOT, TICKER
from .run import _is_session
from .snapshot import HISTORY_CALLS_PER_SESSION, HISTORY_ORIGIN, NoStoredClose, SnapshotError, fetch_snapshot

logger = logging.getLogger(__name__)

DEFAULT_MAX_CALLS = 500          # a first run measures; raise it once the counts are known
DEFAULT_SLEEP_S = 0.5            # pause between two calls (no published rate limit to derive it from)
RATE_LIMIT_STREAK_STOP = 5       # = core.universe_fetch._RATE_LIMIT_STREAK_STOP
REJECTED_STREAK_STOP = 5
SHAPE_SESSIONS = 20              # sessions whose dated volatility answers are described in the counts
IV_RANK_TIMESPAN = "2y"
# The endpoints whose ``date`` behaviour is not known (the value on that day, or a series ending there): their
# answers' shape is reported for the first SHAPE_SESSIONS sessions so the owner can see it before a long run.
SHAPE_ENDPOINTS = ("interpolated_iv", "max_pain", "vol_term_structure", "variance_risk_premium", "realized_vol")
_MAX_FAILURE_RECORDS = 20
_AUTH = (401, 403)


ROW_FAILURES = 3                 # failure records kept in the job_runs row (the run file keeps all of them)


def row_counts(counts: dict) -> dict:
    """The counts for the run's ``job_runs`` row: everything but the answer shapes, and only the first
    ``ROW_FAILURES`` failure records. The run's file (``report_file``) has the full set (#194 review)."""
    row = {k: v for k, v in counts.items() if k != "shapes"}
    if isinstance(row.get("failures"), list):
        row["failures"] = row["failures"][:ROW_FAILURES]
    return row


class HistoryPullRefused(RuntimeError):
    """The pull did not start (the US session is open, or its clock cannot tell)."""


class _Stop(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _Meter:
    """The job's view of the UW client: counts each call and its outcome, paces calls, enforces the cap.

    ``fetch_snapshot`` only calls ``get_result``, so this is all it sees. A stop raised from here leaves the
    snapshot through its staging cleanup, never through a promote: the day in progress is not kept.
    """

    def __init__(self, uw, max_calls: int, sleep_s: float, sleep, requests=None):
        self.uw = uw
        self._requests = requests            # the HTTP request count, a one-item list (or None: not countable)
        self.max_calls = max_calls
        self.sleep_s = sleep_s
        self.sleep = sleep
        self.calls = 0
        self.status: Counter = Counter()
        self.statuses: list[int] = []        # the current session's, in order
        self._streak_429 = 0

    @property
    def used(self) -> int:
        """What the cap is charged: the calls, or the HTTP requests when they are counted and more."""
        return max(self.calls, self._requests[0] if self._requests else 0)

    def get_result(self, ep, params=None):
        if self.used >= self.max_calls:
            raise _Stop("max_calls")
        if self.calls and self.sleep_s:
            self.sleep(self.sleep_s)
        res = self.uw.get_result(ep, params)
        self.calls += 1
        status = 200 if res.ok else int((res.error or {}).get("status") or 0)
        self.status[str(status)] += 1
        self.statuses.append(status)
        self._streak_429 = self._streak_429 + 1 if status == 429 else 0
        if self._streak_429 >= RATE_LIMIT_STREAK_STOP:
            raise _Stop("rate_limit_streak")
        return res


def _http_counter(uw):
    """Count the HTTP requests the real client sends (its retries included); ``(hook, remove)`` or ``(None, noop)``."""
    client = getattr(uw, "_client", None)
    hooks = getattr(client, "event_hooks", None)
    if not isinstance(hooks, dict):
        return None, lambda: None
    seen = [0]

    def hook(_request):
        seen[0] += 1
    client.event_hooks = {**hooks, "request": [*hooks.get("request", []), hook]}

    def remove():
        current = client.event_hooks
        client.event_hooks = {**current, "request": [h for h in current.get("request", []) if h is not hook]}
    return seen, remove


def _shape(payload) -> dict:
    """Type, length, the first row's keys and the span of a ``date`` field: what came back, not what it means."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    out: dict = {"type": type(data).__name__}
    if isinstance(data, list):
        out["rows"] = len(data)
        first = data[0] if data and isinstance(data[0], dict) else {}
        out["keys"] = sorted(first)[:20]
        dates = sorted(str(r["date"]) for r in data if isinstance(r, dict) and r.get("date"))
        out["dates"] = [dates[0], dates[-1]] if dates else None
    elif isinstance(data, dict):
        out["keys"] = sorted(data)[:20]
    return out


def _session_open() -> bool:
    try:
        return nyse_calendar.in_us_session_window()
    except ValueError as exc:            # past the holiday table: not "closed", so do not run
        raise HistoryPullRefused(f"the session clock cannot tell whether the market is open ({exc})") from exc


def sessions_between(start: date, end: date) -> list[str]:
    """NYSE sessions in ``[start, end]``, oldest first (before 2025 the calendar knows weekends only; a holiday
    then has no stored close and costs no request)."""
    out, d = [], start
    while d <= end:
        if _is_session(d):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def newest_fetch_day(root=None) -> str | None:
    """The newest raw day a board Fetch wrote (it has a manifest whose ``origin`` is not the pull's), or None."""
    raw_root = (Path(root) if root else DATA_ROOT) / "raw"
    if not raw_root.exists():
        return None
    for day in sorted((d for d in raw_root.iterdir() if d.is_dir() and not d.name.startswith(".")),
                      key=lambda d: d.name, reverse=True):
        try:
            manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict) and manifest.get("origin") != HISTORY_ORIGIN:
            return day.name
    return None


def check_range(start: str, end: str, root=None) -> tuple[date, date]:
    """The range as dates; ``ValueError`` unless ``start <= end < today`` (New York) and ``end`` is not after the
    board's newest Fetch day.

    The last rule keeps the board on a Fetch day: Refresh rebuilds the newest stored day and moves
    ``latest.json`` to it, and a history day has no chain, iv-rank or options volume (#194 review). A day up to the
    newest Fetch day is fine (that day itself is stored, so it costs nothing).
    """
    lo, hi = date.fromisoformat(start), date.fromisoformat(end)
    if lo > hi:
        raise ValueError(f"start {start} is after end {end}")
    if hi >= nyse_calendar.today_ny():
        raise ValueError(f"end {end} is not a past day; the pull fetches past sessions only")
    newest = newest_fetch_day(root)
    if newest is None:
        raise ValueError("no board Fetch day is stored; Fetch the board once before the history pull")
    if hi.isoformat() > newest:
        raise ValueError(f"end {end} is after the board's newest Fetch day {newest}; a history day there would "
                         f"become the day Refresh shows")
    return lo, hi


def pull_history(uw, start: str, end: str, *, max_calls: int = DEFAULT_MAX_CALLS,
                 sleep_s: float = DEFAULT_SLEEP_S, data_root=None, ticker: str = TICKER,
                 shape_sessions: int = SHAPE_SESSIONS, sleep=None) -> dict:
    """Pull the sessions in ``[start, end]`` that are not stored yet; returns ``{"counts": {...}}``.

    ``uw`` is the job's ``UWClient``. Raises ``ValueError`` for a bad range and :class:`HistoryPullRefused`
    inside the US session, both before any request.
    """
    lo, hi = check_range(start, end, data_root)
    start, end = lo.isoformat(), hi.isoformat()     # fromisoformat also takes 20251024 or 2025-W43-5
    if max_calls <= 0:
        raise ValueError("max_calls must be positive")
    if _session_open():
        raise HistoryPullRefused("the US session is open; the history pull runs outside session hours")
    root = Path(data_root) if data_root else DATA_ROOT
    raw_root = root / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    http, remove_hook = _http_counter(uw)
    meter = _Meter(uw, max_calls, sleep_s, sleep or time.sleep, requests=http)
    days = sessions_between(lo, hi)
    counts: dict = {"start": start, "end": end, "max_calls": max_calls, "sleep_s": sleep_s,
                    "calls_per_session": HISTORY_CALLS_PER_SESSION, "sessions": len(days), "written": 0,
                    "skipped_existing": 0, "skipped_no_close": 0, "failed": 0, "failures": [],
                    "first_written": None, "last_written": None, "stop_reason": None,
                    "iv_rank_file": None, "shapes": {"sessions": {}}}
    try:
        _iv_rank_series(meter, root, ticker, end, counts)
        rejected_streak = 0
        for day in days:
            job_control.checkpoint(f"session {day}")
            if (raw_root / day / "manifest.json").exists():
                counts["skipped_existing"] += 1
                continue
            if _session_open():
                counts["stop_reason"] = "session_open"
                break
            if meter.used + HISTORY_CALLS_PER_SESSION > max_calls:
                counts["stop_reason"] = "max_calls"
                break
            meter.statuses = []
            try:
                raw = fetch_snapshot(meter, day, ticker=ticker, data_root=raw_root, history_only=True,
                                     overwrite=False)
            except NoStoredClose:
                counts["skipped_no_close"] += 1
                continue
            except SnapshotError as exc:
                counts["failed"] += 1
                if len(counts["failures"]) < _MAX_FAILURE_RECORDS:
                    counts["failures"].append({"day": day, "statuses": meter.statuses, "error": str(exc)[:200]})
                first = meter.statuses[0] if meter.statuses else None
                rejected = first is not None and 400 <= first < 500 and first != 429
                rejected_streak = rejected_streak + 1 if rejected else 0
                if rejected_streak >= REJECTED_STREAK_STOP:
                    counts["stop_reason"] = "auth_streak" if first in _AUTH else "rejected_streak"
                    break
            else:
                rejected_streak = 0
                if raw is not None:
                    counts["written"] += 1
                    counts["first_written"] = counts["first_written"] or day
                    counts["last_written"] = day
                    if len(counts["shapes"]["sessions"]) < shape_sessions:
                        shapes = {name: _shape(raw.get(name)) for name in SHAPE_ENDPOINTS}
                        counts["shapes"]["sessions"][day] = shapes
                        logger.info("spx history %s: dated volatility answers %s", day, json.dumps(shapes))
            # After a failed day too: that is where an outage opens the client's breaker (#194 review).
            if _breaker_open(uw):
                counts["stop_reason"] = "breaker_open"
                break
    except _Stop as stop:
        counts["stop_reason"] = stop.reason
    except job_control.JobStopped as exc:
        counts["stop_reason"] = "cancelled" if isinstance(exc, job_control.JobCancelled) else "deadline"
        raise
    except Exception as exc:
        counts["stop_reason"] = f"error: {type(exc).__name__}: {exc}"[:200]
        raise
    finally:
        # Also on a cancel or an error: the calls already sent are what the owner needs to know (#194 review).
        remove_hook()
        _finish(counts, meter, http, root, start, end)
    return {"counts": counts}


def _finish(counts: dict, meter: _Meter, http, root: Path, start: str, end: str) -> None:
    """Close the counts, write the run's report file and log the summary; never raises (it runs in ``finally``)."""
    counts["calls"] = meter.calls
    counts["status"] = dict(meter.status)
    counts["http_requests"] = http[0] if http else None
    # The dashboard holds the store while it runs, so the counts are also a file the owner can open.
    report = root / "history_pull" / f"run_{start}_{end}_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    counts["report_file"] = str(report.relative_to(root))
    try:
        safe_json.write_json_atomic(report, counts)
    except OSError:
        counts["report_file"] = None
        logger.warning("spx history pull: could not write %s", report, exc_info=True)
    logger.info("spx history pull %s..%s: %s written, %s already stored, %s without a stored close, %s failed; "
                "%s calls (%s), stop: %s", start, end, counts["written"], counts["skipped_existing"],
                counts["skipped_no_close"], counts["failed"], counts["calls"], counts["status"],
                counts["stop_reason"] or "end of range")


def _iv_rank_series(meter: _Meter, root: Path, ticker: str, end: str, counts: dict) -> None:
    """One ``iv-rank`` call for the whole range (``timespan=2y`` ending on ``end``), stored once beside the days."""
    target = root / "history_pull" / f"iv_rank_{end}_{IV_RANK_TIMESPAN}.json"
    counts["iv_rank_file"] = str(target.relative_to(root))
    if target.exists():
        return
    res = meter.get_result(f"/stock/{ticker}/iv-rank", {"date": end, "timespan": IV_RANK_TIMESPAN})
    if not res.ok:
        counts["iv_rank_file"] = None
        counts["shapes"]["iv_rank"] = {"error": res.error}
        return
    safe_json.write_json_atomic(target, res.data)
    counts["shapes"]["iv_rank"] = _shape(res.data)


def _breaker_open(uw) -> bool:
    health = getattr(uw, "health", None)
    if not callable(health):
        return False
    try:
        return bool((health() or {}).get("breaker_open"))
    except Exception:  # noqa: BLE001 - accounting must not stop the pull
        return False
