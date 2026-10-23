"""SPX daily levels (D03, #293, M-SPX-0 #376): the one writer of ``index_levels_daily`` (prices.duckdb).

The source is yfinance ``^GSPC`` with ``auto_adjust=False`` (UW's SPX OHLC needs paid access, G2-06). avo's typed
words (2026-10-16): "a one-time period=max fill, then at most 1 yfinance request a night. Writes prices.duckdb
index_levels_daily only." Two entry points, one lock (``job_lock`` name ``"index_levels"``):

- :func:`nightly` is the best-effort last step of the RC-Daily-Bars task (``scripts/daily_bars.py
  --nightly --index-levels``), after the bars. **At most one yfinance request a night**, and none when:
  - the table already holds the latest complete New York session (a weekend, a holiday, a rerun);
  - the table is empty: the fill is attended and never automatic;
  - this session's marker exists. Before its request the step takes the marker ``<session>_GSPC`` under
    ``config.OUTPUT_DIR/index_levels`` (``O_EXCL``, the #301 rule), so a rerun or a second process the same night
    asks nothing; no marker, no request.
  The request is one ``yf.download`` of ``^GSPC`` from ``LOOKBACK_DAYS`` before the newest stored session through
  the latest complete one, with no retry. A night that fails or misses a bar is covered by the next night's window.
- :func:`fill` is the attended one-time ``period="max"`` fill (``scripts/daily_bars.py --index-fill --confirm``):
  one request. It needs SPY's D01 fill (its marker and its stored bars) for the rule-C dates below; without it the
  fill asks nothing.

Both write ``index_levels_daily`` only, append, in one transaction that skips every stored key: the first write of a
key wins, so a rerun, a second fill or an overlap adds nothing and never rewrites a row. Only complete sessions are
kept (``prices.keep_session``): from 2025 on a calendar trading day up to the latest complete session (rule B: a run
inside the session writes nothing of it); before 2025 a rule-C date, i.e. a date of SPY's stored D01 bars, or any
``^GSPC`` date before SPY's first bar. ``core/prices.py`` never stores an index level; this module does.

Neither entry point raises: a failure is a ``failed`` status with its error. The daily-bars exit code never depends
on the nightly step, because RC-Screeners-Daily, RC-Forecast-Outcomes and RC-Earnings-Inputs wait on that exit 0
(the #301 rule).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from RevenueChaser.core import calendar, config, job_lock, ledger, prices, rawstore
from RevenueChaser.core.datahealth import Health

logger = logging.getLogger(__name__)

SYMBOL = "^GSPC"
SOURCE = "yfinance"
TABLE = "index_levels_daily"
LOCK = "index_levels"
LOOKBACK_DAYS = 10             # calendar days before the newest stored session the nightly asks again
MARKER_NAME = "GSPC"           # the marker file suffix (no caret in a file name)
MARKER_KEEP_DAYS = 7
_DB = rawstore._db_of(TABLE)   # "prices"


def _yf_download(tickers, **kw):
    """The real yfinance call (``core/prices``' seam), imported lazily so no test or store-only path loads it."""
    return prices._yf_download(tickers, **kw)


def _blank(run_id: str) -> dict:
    return {"status": "failed", "reason": None, "error": None, "symbol": SYMBOL, "latest_session": None,
            "newest_stored": None, "newest_after": None, "requests": 0, "rows_offered": 0, "rows_inserted": 0,
            "dropped_off_calendar": 0, "dropped_off_reference": 0, "dropped_no_close": 0, "run_id": run_id}


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


# --- store reads ----------------------------------------------------------------------------------------------------

def levels_between(start: date | str, end: date | str, symbol: str = SYMBOL) -> list[dict]:
    """Stored sessions in ``[start, end]`` in the snapshot's candle shape (store only, no network).

    The SPX Gamma snapshot reads its 8-week window from here; this module is the table's only writer.
    """
    lo = start if isinstance(start, date) else date.fromisoformat(start)
    hi = end if isinstance(end, date) else date.fromisoformat(end)
    rows = rawstore.query(f"SELECT session_date, open, high, low, close, volume FROM {TABLE} "
                          "WHERE symbol = ? AND session_date BETWEEN ? AND ? ORDER BY session_date",
                          [symbol, lo, hi], db=_DB)
    return [{"date": r["session_date"].isoformat(), "open": r["open"], "high": r["high"], "low": r["low"],
             "close": r["close"], "volume": r["volume"]} for r in rows]


def health_summary(now: datetime | None = None) -> dict:
    """Read-only summary for the Data Health row (§9.6): newest stored session vs the latest complete one, the
    row count and the source. Store only, zero network; a calendar error is reported, never raised."""
    now = now or calendar._now()
    out: dict = {"newest_session": None, "latest_complete": None, "calendar_error": False, "rows": 0,
                 "source": None}
    try:
        latest = calendar.latest_complete_session(now)
        out["latest_complete"] = latest.isoformat()
    except ValueError:
        out["calendar_error"] = True
    stats = rawstore.query(f"SELECT COUNT(*) AS n, MAX(session_date) AS newest FROM {TABLE} WHERE symbol = ?",
                           [SYMBOL], db=_DB)
    if not stats:  # the table does not exist yet: no rows, not a failure
        return out
    out["rows"] = int(stats[0]["n"] or 0)
    newest = stats[0]["newest"]
    out["newest_session"] = newest.isoformat() if newest is not None else None
    if newest is not None:
        src = rawstore.query(f"SELECT source FROM {TABLE} WHERE symbol = ? AND session_date = ?",
                             [SYMBOL, newest], db=_DB)
        out["source"] = src[0]["source"] if src else None
    return out


def health(now: datetime | None = None) -> Health:
    """The numbers :func:`health_summary` reports, typed (#124 S4).

    The population is the one symbol this table stores: ``covered`` is 1 when it has a level, ``expected`` 1.
    How many sessions are stored is not a population, so it stays ``rows`` in the summary and shows here as
    freshness: ``newest`` against the latest complete ``expected_as_of``. A calendar error is the one problem.
    """
    n = health_summary(now)
    return Health(
        covered=1 if n["rows"] else 0,
        expected=1,
        newest=n["newest_session"],
        expected_as_of=n["latest_complete"],
        source_share={n["source"]: 1.0} if n["source"] else None,
        problems=("calendar error: the latest complete session is unavailable",)
        if n["calendar_error"] else (),
    )


def _newest() -> date | None:
    rows = rawstore.query(f"SELECT MAX(session_date) AS m FROM {TABLE} WHERE symbol = ?", [SYMBOL], db=_DB)
    return rows[0]["m"] if rows else None


def _spy_reference() -> set[date] | None:
    """SPY's stored D01 dates, or None when SPY's D01 fill is not done (no fill marker or no stored bar)."""
    mark = rawstore.query("SELECT 1 AS x FROM price_checks WHERE dataset = ? AND source = ? AND key = 'SPY'",
                          list(prices.CHECK_FILL), db=_DB)
    if not mark:
        return None
    rows = rawstore.query("SELECT session_date FROM price_bars_daily WHERE ticker = 'SPY'", db=_DB)
    dates = {r["session_date"] for r in rows if r.get("session_date") is not None}
    return dates or None


# --- the per-night marker (the #301 rule) ---------------------------------------------------------------------------

def _reserve(marker_dir: Path, night: date) -> bool:
    """Take tonight's marker; False when it was taken before. Raises ``OSError`` when it cannot be written."""
    marker_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(marker_dir / f"{night.isoformat()}_{MARKER_NAME}", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(fd)
    _prune(marker_dir, night)
    return True


def _prune(marker_dir: Path, night: date) -> None:
    """Drop this step's markers older than ``MARKER_KEEP_DAYS``; bookkeeping only, never a failure."""
    cutoff = night - timedelta(days=MARKER_KEEP_DAYS)
    for f in marker_dir.glob(f"????-??-??_{MARKER_NAME}"):
        try:
            if date.fromisoformat(f.name[:10]) < cutoff:
                f.unlink()
        except (OSError, ValueError):
            logger.debug("index_levels: old marker %s kept", f.name, exc_info=True)


# --- the write ------------------------------------------------------------------------------------------------------

def _rows(frame, *, latest: date, fetched_at: float, run_id: str, run_at: float, res: dict) -> list[dict]:
    """The complete-session ``index_levels_daily`` rows of a ``^GSPC`` frame, one per date (``res`` counts drops)."""
    bars, _ = prices.rows_from_yf(frame, [SYMBOL], fetched_at=fetched_at, run_id=run_id, run_at=run_at,
                                  counts=res)
    by_date: dict[date, dict] = {}
    for b in bars.get(SYMBOL, []):
        by_date.setdefault(b["session_date"], b)
    res["rows_offered"] = len(by_date)
    if not by_date:
        raise RuntimeError(f"the yfinance reply holds no {SYMBOL} row")
    reference: set[date] = set()           # without SPY's bars no date before 2025 is kept
    if any(d < prices.CALENDAR_START for d in by_date):
        spy = _spy_reference()
        if spy:
            reference = spy | {d for d in by_date if d < min(spy)}
    out: list[dict] = []
    for d in sorted(by_date):
        reason = prices.keep_session(d, latest, reference)
        if reason is not None:
            res[f"dropped_{reason}"] += 1
            continue
        b = by_date[d]
        out.append({"symbol": SYMBOL, "session_date": d, "open": b["open"], "high": b["high"], "low": b["low"],
                    "close": b["close"], "volume": b["volume"], "source": SOURCE, "fetched_at": b["fetched_at"],
                    "run_id": run_id, "run_at": run_at})
    return out


def _append(rows: list[dict]) -> int:
    """Append the rows whose key is not stored, in one transaction of prices.duckdb; returns how many."""
    def _write(tx):
        tx.ensure_table(TABLE)
        stored = {r[0] for r in tx.execute(f"SELECT session_date FROM {TABLE} WHERE symbol = ?",
                                           [SYMBOL]).fetchall()}
        new = [r for r in rows if r["session_date"] not in stored]
        tx.write_rows(TABLE, new)
        return len(new)

    return rawstore.atomic(_write, db=_DB) if rows else 0


def _request(dl, res: dict, **kw):
    """The one yfinance request of a run, counted before it is sent; an empty reply is a failure."""
    res["requests"] += 1
    frame = dl([SYMBOL], **{**prices.YF_KW, **kw})
    if frame is None or len(frame) == 0:
        raise RuntimeError(f"yfinance returned no {SYMBOL} rows")
    return frame


def _stamp(now_given: bool, run_at: float) -> float:
    """``fetched_at``: when the reply arrived (the calendar seam), or the test's fixed ``now``."""
    return run_at if now_given else calendar._now().timestamp()


# --- the nightly step -----------------------------------------------------------------------------------------------

def nightly(*, run_id: str | None = None, now: datetime | None = None, yf_download=None,
            marker_dir: Path | None = None) -> dict:
    """Append the sessions ``^GSPC`` is missing up to the latest complete one, with at most one yfinance request a
    night; see the module docstring. Never raises.

    ``status``: ``ok`` (tonight's request brought the store to the latest complete session), ``partial`` (the
    request succeeded but Yahoo had not served that session yet), ``current`` (nothing missing, no request),
    ``skipped`` (no fill yet, the lock is held, or tonight's request was spent), ``failed``."""
    rid = run_id or ledger.new_run_id()
    res = _blank(rid)
    try:
        now_given = now is not None
        now = now if now_given else calendar._now()
        run_at = now.timestamp()
        latest = calendar.latest_complete_session(now)
        res["latest_session"] = latest.isoformat()
        newest = _newest()
        res["newest_stored"] = res["newest_after"] = _iso(newest)
        if newest is None:
            res.update(status="skipped", reason="no stored level yet: the one-time fill is attended "
                                                "(scripts/daily_bars.py --index-fill --confirm)")
            return res
        if newest >= latest:
            res["status"] = "current"
            return res
        with job_lock.acquire(LOCK, wait=0) as got:
            if not got:
                res.update(status="skipped", reason="another index-levels run holds the lock")
                return res
            try:
                if not _reserve(Path(marker_dir or (config.OUTPUT_DIR / "index_levels")), latest):
                    res.update(status="skipped",
                               reason=f"the one request for session {latest.isoformat()} was made already")
                    return res
            except OSError as exc:
                res["error"] = f"marker not writable, no request made ({type(exc).__name__})"
                return res
            dl = yf_download or _yf_download
            frame = _request(dl, res, start=newest - timedelta(days=LOOKBACK_DAYS), end=latest + timedelta(days=1))
            rows = _rows(frame, latest=latest, fetched_at=_stamp(now_given, run_at), run_id=rid, run_at=run_at,
                         res=res)
            res["rows_inserted"] = _append(rows)
        after = _newest()
        res["newest_after"] = _iso(after)
        res["status"] = "ok" if after is not None and after >= latest else "partial"
    except Exception as exc:  # noqa: BLE001 - the step never raises
        res.update(status="failed", error=_err(exc))
    return res


# --- the one-time fill ----------------------------------------------------------------------------------------------

def fill(*, run_id: str | None = None, now: datetime | None = None, yf_download=None) -> dict:
    """The attended ``period="max"`` fill of ``^GSPC``: one request, idempotent. Never raises."""
    rid = run_id or ledger.new_run_id()
    res = _blank(rid)
    res.update(first_session=None, rows_stored=0)
    try:
        now_given = now is not None
        now = now if now_given else calendar._now()
        run_at = now.timestamp()
        latest = calendar.latest_complete_session(now)
        res["latest_session"] = latest.isoformat()
        res["newest_stored"] = _iso(_newest())
        if _spy_reference() is None:
            res["error"] = ("SPY's D01 fill is not done (scripts/daily_bars.py --fill --confirm): the dates before "
                            "2025 come from SPY's bars (rule C); no request made")
            return res
        with job_lock.acquire(LOCK, wait=0) as got:
            if not got:
                res.update(status="skipped", reason="another index-levels run holds the lock")
                return res
            frame = _request(yf_download or _yf_download, res, period="max")
            rows = _rows(frame, latest=latest, fetched_at=_stamp(now_given, run_at), run_id=rid, run_at=run_at,
                         res=res)
            res["rows_inserted"] = _append(rows)
        stats = rawstore.query(f"SELECT COUNT(*) AS n, MIN(session_date) AS first, MAX(session_date) AS last "
                               f"FROM {TABLE} WHERE symbol = ?", [SYMBOL], db=_DB)[0]
        res.update(status="ok", rows_stored=int(stats["n"]), first_session=_iso(stats["first"]),
                   newest_after=_iso(stats["last"]))
    except Exception as exc:  # noqa: BLE001 - an attended run reports, it does not trace back
        res.update(status="failed", error=_err(exc))
    return res


def summary_line(res: dict) -> str:
    """One human line for the CLI's stderr and the task log."""
    n = int(res.get("requests") or 0)
    asked = "no request" if n == 0 else f"{n} yfinance request{'s' if n != 1 else ''}"
    head = f"Index levels {SYMBOL}"
    status = res.get("status")
    if status in ("ok", "partial"):
        text = (f"{head} {res.get('latest_session')}: {res.get('rows_inserted', 0)} rows "
                f"(newest {res.get('newest_after')})")
        if res.get("rows_stored"):
            text += f", {res['rows_stored']} stored from {res.get('first_session')}"
        if status == "partial":
            text += "; behind: Yahoo has not served the latest session"
        return f"{text}; {asked}"
    if status == "current":
        return f"{head}: current at {res.get('latest_session')}; {asked}"
    if status == "skipped":
        return f"{head}: skipped ({res.get('reason')}); {asked}"
    return f"{head}: failed ({res.get('error')}); {asked}"


__all__ = ["SYMBOL", "SOURCE", "TABLE", "LOCK", "LOOKBACK_DAYS", "nightly", "levels_between",
           "health", "health_summary", "fill", "summary_line"]
