"""The Welcome board's incremental build: stored inputs -> ``boards/welcome.duckdb`` (welcome plan §4, S16).

Three parts, each upserted only when its source changed since its mark in ``welcome_meta``; nothing is deleted:

- **Snapshot**: the last stored close of SPX, VIX, MOVE and the US 10-year yield, with the change on the session
  before and a 20-session line. A row is written only for a session newer than the series' stored one, or when the
  stored session's close changed. All four come from the builder's ``market.duckdb`` through their needs
  (#643 PR 5): the old ``prices.index_levels_daily``, ``raw.vol_index_daily`` and ``raw.macro_series`` reads are
  gone. A need that answers nothing writes no row, exactly as an empty old table did.
- **Earnings**: the names reporting today and on the next trading day (New York dates), from the newest
  ``raw.earnings_calendar`` snapshot of each day and report session. A name that snapshot no longer lists gets
  ``retired_at``. The watch list is applied at render time, so a pin shows without a rebuild.
- **Freshness**: each job's last finished run, its last success and its last failure, from ``raw.job_runs`` rows
  finished after the mark. A scheduled task is named by its registry name, a dashboard job by its module. A "done"
  run whose message says it finished with gaps (failed provider calls) is stored as ``gaps``, not as a success.

No network, no provider call. The calendar card reads ``boards/news.duckdb`` at render time and copies nothing.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from RevenueChaser.core import calendar as mcal
from RevenueChaser.core import rawstore
from RevenueChaser.rcdata import needs
from RevenueChaser.app.jobs import GAPS

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
SNAPSHOT, EARNINGS, FRESHNESS, META = "welcome_snapshot", "welcome_earnings", "welcome_freshness", "welcome_meta"
SPARK_DAYS = 20

#: series -> (need, its id in ``market_series``). The four share one table in ``market.duckdb``, so each is asked
#: for its own id: SPX and VIX are Cboe's, MOVE is Yahoo's ``^MOVE``, the 10-year yield FRED's ``DGS10``. MOVE has
#: no need of its own and goes through ``market.series``, which is why that need requires a named id.
SERIES = {
    "SPX": ("market.spx_close", "SPX"),
    "VIX": ("market.vix", "VIX"),
    "MOVE": ("market.series", "MOVE"),
    "US10Y": ("macro.dgs10", "DGS10"),
}
#: Calendar days read back to hold ``SPARK_DAYS`` sessions: 20 sessions are 4 weeks, and a holiday or a provider's
#: gap makes them longer. The reader has no row limit, so the window is the bound.
SPARK_LOOKBACK_DAYS = 60


def ny_day(ts: float):
    return datetime.fromtimestamp(float(ts), NY).date()


def _value(row: dict) -> float | None:
    """The close of an OHLC row, else the one value column (Cboe's SPX and VIX, FRED's yields)."""
    v = row.get("close")
    return float(v) if v is not None else (float(row["value"]) if row.get("value") is not None else None)


def _observations(need: str, series_id: str, day: date) -> list[dict]:
    """The newest ``SPARK_DAYS`` observations of one series, newest first.

    An unreadable need is no rows, the way an empty old table was: the series keeps the row it already has and the
    next run tries again. ``needs.ask`` is the read, so a missing ``market.duckdb`` is a state and not an error.
    """
    try:
        ans = needs.ask(need, as_of=day, series_ids=[series_id], start=day - timedelta(days=SPARK_LOOKBACK_DAYS),
                        end=day)
    except Exception as exc:  # noqa: BLE001 - a need that cannot answer must not stop the other three
        logger.warning("welcome snapshot: %s (%s) could not be read: %s", need, series_id, exc)
        return []
    # One row per session: the table is keyed on the source as well, so a series served by two sources (or a
    # revision filed under a new one) would otherwise put two rows in the line and in the previous close.
    newest: dict[date, dict] = {}
    for r in ans.rows:
        if r.get("series_id") != series_id or _value(r) is None:
            continue
        held = newest.get(r["obs_date"])
        if held is None or (float(r.get("fetched_at") or 0.0), str(r.get("source") or "")) >= (
                float(held.get("fetched_at") or 0.0), str(held.get("source") or "")):
            newest[r["obs_date"]] = r
    days = sorted(newest)[-SPARK_DAYS:]
    return [{"session_date": d, "close": _value(newest[d])} for d in reversed(days)]


def earnings_days(now: float) -> tuple:
    """Today's New York trading day (or the next one on a closed day) and the trading day after it."""
    d = ny_day(now)
    first = d if mcal.is_trading_day(d) else mcal.next_trading_day(d)
    return first, mcal.next_trading_day(first)


def _meta(key: str) -> str | None:
    rows = rawstore.query(f"SELECT value FROM {META} WHERE key = ?", [key], db="welcome")
    return rows[0]["value"] if rows else None


def _set_meta(key: str, value: str, now: float) -> None:
    rawstore.write_rows(META, [{"key": key, "value": value, "run_at": now}])


def _snapshot(now: float, run_id: str) -> int:
    stored = {r["series"]: (r["session_date"], r["close"]) for r in rawstore.query(
        f"SELECT series, session_date, close FROM {SNAPSHOT} "
        "QUALIFY row_number() OVER (PARTITION BY series ORDER BY session_date DESC) = 1", db="welcome")}
    day = ny_day(now)
    rows = []
    for series, (need, series_id) in SERIES.items():
        got = _observations(need, series_id, day)
        old = stored.get(series)
        # A newer session, or a corrected close for the session already held (a revision, a later fetch).
        if not got or (old is not None and (got[0]["session_date"] < old[0] or
                                            (got[0]["session_date"] == old[0] and float(got[0]["close"]) == old[1]))):
            continue
        close = float(got[0]["close"])
        prev = float(got[1]["close"]) if len(got) > 1 else None
        rows.append({"series": series, "session_date": got[0]["session_date"], "close": close, "prev_close": prev,
                     "change": None if prev is None else close - prev,
                     "change_pct": None if not prev else (close - prev) / prev,
                     "spark": json.dumps([float(r["close"]) for r in reversed(got)]),
                     "run_id": run_id, "run_at": now})
    rawstore.write_rows(SNAPSHOT, rows)
    return len(rows)


def _earnings(now: float, run_id: str) -> tuple[int, int]:
    days = earnings_days(now)
    newest = rawstore.query("SELECT max(fetched_at) AS t FROM earnings_calendar")
    mark = f"{days[0].isoformat()}|{days[1].isoformat()}|{newest[0]['t'] if newest else None}"
    if _meta("earnings_mark") == mark:
        return 0, 0
    fresh = rawstore.query(
        "SELECT * FROM earnings_calendar WHERE earnings_date IN (?, ?) "
        "QUALIFY session_date = max(session_date) OVER (PARTITION BY earnings_date, report_session)",
        [days[0], days[1]])
    pairs = {(r["earnings_date"], r["report_session"]) for r in fresh}
    keys = {(r["earnings_date"], r["ticker"], r["report_session"]) for r in fresh}
    stored = {(r["earnings_date"], r["ticker"], r["report_session"]): r for r in rawstore.query(
        f"SELECT * FROM {EARNINGS} WHERE earnings_date IN (?, ?)", [days[0], days[1]], db="welcome")}
    rows = []
    for r in fresh:
        key = (r["earnings_date"], r["ticker"], r["report_session"])
        old = stored.get(key) or {}
        rows.append({"earnings_date": key[0], "ticker": key[1], "report_session": key[2],
                     "full_name": r.get("full_name"), "expected_move_perc": r.get("expected_move_perc"),
                     "report_time": r.get("report_time"), "source_session": r["session_date"],
                     "first_seen": old.get("first_seen") or float(r["fetched_at"]),
                     "last_seen": float(r["fetched_at"]), "retired_at": None, "run_id": run_id, "run_at": now})
    upserted = len(rows)
    for key, old in stored.items():
        if key not in keys and (key[0], key[2]) in pairs and old.get("retired_at") is None:
            rows.append({**old, "retired_at": now, "run_id": run_id, "run_at": now})
    rawstore.write_rows(EARNINGS, rows)
    _set_meta("earnings_mark", mark, now)
    return upserted, len(rows) - upserted


def _freshness(now: float, run_id: str) -> int:
    try:
        mark = float(_meta("freshness_mark") or 0.0)
    except ValueError:
        mark = 0.0
    touched = rawstore.query(
        "SELECT DISTINCT coalesce(registry_name, module) AS job FROM job_runs "
        # The listener's 5-minute News pass (#673) is not a job a person waits on; the News page shows its gaps.
        "WHERE finished_at > ? AND coalesce(registry_name, module) IS NOT NULL "
        "AND coalesce(registry_name, module) <> 'news_headlines'", [mark])
    if not touched:
        return 0
    jobs = [r["job"] for r in touched]
    marks = ", ".join("?" for _ in jobs)
    runs = rawstore.query(
        "SELECT coalesce(registry_name, module) AS job, status, finished_at, error, message FROM job_runs "
        f"WHERE finished_at IS NOT NULL AND coalesce(registry_name, module) IN ({marks}) ORDER BY finished_at",
        jobs)
    out: dict[str, dict] = {}
    for r in runs:                                              # oldest first, so the last run wins
        row = out.setdefault(r["job"], {"job": r["job"], "last_ok_at": None, "last_failed_at": None,
                                        "last_error": None, "run_id": run_id, "run_at": now})
        row["last_status"], row["last_finished_at"] = r["status"], float(r["finished_at"])
        if r["status"] == "done" and GAPS in (r.get("message") or ""):    # finished, but its provider calls failed
            row["last_status"], row["last_error"] = "gaps", r["message"][:200]
        elif r["status"] == "done":
            row["last_ok_at"] = float(r["finished_at"])
        elif r["status"] == "error":
            row["last_failed_at"] = float(r["finished_at"])
            row["last_error"] = (r.get("error") or "")[:200] or None
    rawstore.write_rows(FRESHNESS, list(out.values()))
    _set_meta("freshness_mark", repr(max(row["last_finished_at"] for row in out.values())), now)
    return len(out)


def build(now: float | None = None, run_id: str | None = None) -> dict:
    """One incremental pass. Returns counts: ``snapshot``, ``earnings``, ``retired``, ``freshness``."""
    now = time.time() if now is None else now
    run_id = run_id or f"welcome-{int(now)}"
    snap = _snapshot(now, run_id)
    earn, retired = _earnings(now, run_id)
    fresh = _freshness(now, run_id)
    return {"snapshot": snap, "earnings": earn, "retired": retired, "freshness": fresh}
