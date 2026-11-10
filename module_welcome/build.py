"""The Welcome board's incremental build: stored inputs -> ``boards/welcome.duckdb`` (welcome plan §4, S16).

Three parts, each upserted only when its source changed since its mark in ``welcome_meta``; nothing is deleted:

- **Snapshot**: the last stored close of SPX (``prices.index_levels_daily`` ``^GSPC``), VIX and MOVE
  (``raw.vol_index_daily``, the newest fetch of a day across sources) and the US 10-year yield (``raw.macro_series``
  ``DGS10``), with the change on the session before and a 20-session line. A row is written only for a session
  newer than the series' stored one, or when the stored session's close changed.
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
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from RevenueChaser.core import calendar as mcal
from RevenueChaser.core import rawstore
from RevenueChaser.app.jobs import GAPS

NY = ZoneInfo("America/New_York")
SNAPSHOT, EARNINGS, FRESHNESS, META = "welcome_snapshot", "welcome_earnings", "welcome_freshness", "welcome_meta"
SPARK_DAYS = 20

#: series -> (db, SQL giving ``session_date, close`` newest first, one row per session).
SERIES = {
    "SPX": ("prices", "SELECT session_date, close FROM index_levels_daily WHERE symbol = '^GSPC' AND close IS NOT NULL "
                      "ORDER BY session_date DESC LIMIT ?"),
    "VIX": ("raw", "SELECT session_date, close FROM vol_index_daily WHERE symbol = 'VIX' AND close IS NOT NULL "
                   "QUALIFY row_number() OVER (PARTITION BY session_date ORDER BY fetched_at DESC, source) = 1 "
                   "ORDER BY session_date DESC LIMIT ?"),
    "MOVE": ("raw", "SELECT session_date, close FROM vol_index_daily WHERE symbol = 'MOVE' AND close IS NOT NULL "
                    "QUALIFY row_number() OVER (PARTITION BY session_date ORDER BY fetched_at DESC, source) = 1 "
                    "ORDER BY session_date DESC LIMIT ?"),
    "US10Y": ("raw", "SELECT obs_date AS session_date, value AS close FROM macro_series "
                     "WHERE series_id = 'DGS10' AND value IS NOT NULL "
                     "QUALIFY row_number() OVER (PARTITION BY obs_date ORDER BY fetched_at DESC, source) = 1 "
                     "ORDER BY obs_date DESC LIMIT ?"),
}


def ny_day(ts: float):
    return datetime.fromtimestamp(float(ts), NY).date()


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
    rows = []
    for series, (db, sql) in SERIES.items():
        got = rawstore.query(sql, [SPARK_DAYS], db=db)
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
        "WHERE finished_at > ? AND coalesce(registry_name, module) IS NOT NULL", [mark])
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
