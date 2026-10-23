"""Rotation snapshot persistence — DuckDB-backed session cross-sections + backfill.

A snapshot is the rotation engine's cross-section (z-normalized) keyed by its session (``as_of_date``, #283), stored
in ``.store/raw.duckdb`` (``rotation_snapshots``). Backfill reconstructs past sessions from the stored daily bars (D01,
one read for the whole window, every session computed locally with ``engine.cross_section``, the live Fetch's
function: D-M3 "weekly, both", M-ROT-2), so the trail view works without waiting for live snapshots to accumulate.
It makes no provider call; the macro series have no D01 bars and stay live-only.
"""
import logging
from datetime import date, datetime, timedelta

from RevenueChaser.core import calendar, rawstore
from .universe import MACRO_SERIES, ROTATION_ETFS  # noqa: F401 - re-exported names tests patch
from . import engine
from .engine import _finite

logger = logging.getLogger(__name__)


class BackfillResult(int):
    """Days written (an int, so every caller and test that compares it keeps working) plus what
    stopped the rest: ``skipped`` days left unwritten. ``failed`` names tickers whose candles could not be read; since
    the backfill reads D01 (M-ROT-2) nothing is fetched, so it stays empty."""

    def __new__(cls, written: int, failed=(), skipped: int = 0, left_out=()):
        self = super().__new__(cls, written)
        self.failed = list(failed)
        self.skipped = int(skipped)
        self.left_out = list(left_out)
        return self

    def message(self) -> str:
        """One line for the job message, or ``""`` when every day was written whole or already stored."""
        parts = []
        if self.skipped:
            noun = "day" if self.skipped == 1 else "days"
            if self.failed:
                parts.append(f"{self.skipped} {noun} not written, ETF fetch failed: {', '.join(self.failed)}.")
            else:
                parts.append(f"{self.skipped} {noun} not written: the stored daily bars (D01) hold no SPY bar of that "
                             f"session or fewer than {engine.MIN_WEEKS} weeks of SPY history before it.")
        if self.left_out:
            # The sessions are still written (D01 is what it is; a re-run gives the same cross-section), but a gap in
            # a trail or a z-score shift between sessions is explained (review 2026-10-16).
            parts.append(f"{len(self.left_out)} rotation ETF(s) left out of written sessions (no D01 bar in the "
                         f"session's week, or under {engine.MIN_WEEKS} weeks of them): {', '.join(self.left_out)}.")
        return " ".join(parts)


def load_holdings_cache(ticker: str, any_age: bool = False) -> dict | None:
    """Cached ETF holdings + RS rows (None if absent, or not from today unless ``any_age``).

    The store-only holdings view passes ``any_age=True``: the last fetched rows
    beat an empty row until the next "Fetch holdings".
    """
    row = rawstore.get("rotation_holdings", ticker.upper())
    if not row:
        return None
    if not any_age and row.get("date") != date.today().isoformat():
        return None
    return row.get("rows")


def save_holdings_cache(ticker: str, rows: list[dict]) -> None:
    import time
    rawstore.write_rows("rotation_holdings", [{
        "ticker": ticker.upper(),
        "date": date.today().isoformat(),
        "rows": rows,
        "fetched_at": time.time(),
    }])


def _snap_to_dict(s) -> dict:
    def _val(v):
        return v if _finite(v) else None
    return {
        "as_of_date": s.as_of_date.isoformat(),
        "ticker": s.ticker,
        "name": ROTATION_ETFS.get(s.ticker, {}).get("name", ""),
        "group": s.group_name,
        "close_price": s.close_price if _finite(s.close_price) else None,
        "rs_1w": _val(s.rs_1w), "rs_1m": _val(s.rs_1m), "rs_3m": _val(s.rs_3m),
        "rs_ratio": _val(s.rs_ratio), "rs_momentum": _val(s.rs_momentum),
        "rs_ratio_z": _val(s.rs_ratio_z), "rs_momentum_z": _val(s.rs_momentum_z),
        "quadrant": s.rrg_quadrant,
    }


def save_snapshot(snapshots: list, as_of: date = None, benchmark: str = "SPY") -> list[dict]:
    """Persist one snapshot per day (same-day rerun overwrites). Returns the rows written (``source`` ``live``), which a
    Rotation Fetch also records in its result history (#374)."""
    d = as_of or date.today()
    generated_at = datetime.now().isoformat()
    rows = []
    for s in snapshots:
        r = _snap_to_dict(s)
        r["as_of_date"] = d.isoformat()  # key by the requested as-of (file-name semantics)
        r["benchmark"] = benchmark
        r["generated_at"] = generated_at
        r["source"] = "live"
        rows.append(r)
    rawstore.write_rows("rotation_snapshots", rows)
    return rows


def load_snapshot(as_of: date) -> dict | None:
    rows = rawstore.query(
        "SELECT * FROM rotation_snapshots WHERE as_of_date = ? ORDER BY ticker",
        [as_of.isoformat()])
    if not rows:
        return None
    return {
        "as_of_date": as_of.isoformat(),
        "benchmark": rows[0].get("benchmark") or "SPY",
        "generated_at": rows[0].get("generated_at") or "",
        "etfs": rows,
    }


def list_snapshot_dates() -> list[str]:
    rows = rawstore.query("SELECT DISTINCT as_of_date AS d FROM rotation_snapshots ORDER BY d")
    return [r["d"] for r in rows]


def load_range(start: date, end: date) -> dict[str, list[dict]]:
    """Return {as_of_date: [etf dicts]} for every stored snapshot in range."""
    rows = rawstore.query(
        "SELECT * FROM rotation_snapshots WHERE as_of_date >= ? AND as_of_date <= ? ORDER BY as_of_date, ticker",
        [start.isoformat(), end.isoformat()])
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["as_of_date"], []).append(r)
    return out


def _sessions_back(latest: date, n: int) -> list[date]:
    """The ``n`` trading sessions up to ``latest``, oldest first (no weekend or holiday, #283)."""
    out: list[date] = []
    d = latest
    while len(out) < n:
        d = calendar.previous_trading_day(d)
        out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def backfill_snapshots(uw=None, days: int = 20, force: bool = False) -> BackfillResult:
    """Reconstruct the last ``days`` complete sessions of rotation snapshots from D01.

    ``force`` recomputes sessions already stored. A session is not written when D01 holds no SPY bar of it or too
    little SPY history before it; the result counts them. ``uw`` is unused (D01, no provider call); kept for callers.
    """
    try:
        sessions = _sessions_back(calendar.latest_complete_session(), days)
    except ValueError as exc:                  # outside the holiday table
        logger.warning("rotation backfill: no sessions: %s", exc)
        return BackfillResult(0)
    return _write_days(sessions, force)


def _session_of(day: date, latest: date) -> date | None:
    """The session a requested day stands for: the latest trading day on or before it; ``None`` past ``latest`` (not
    complete yet) or outside the holiday table."""
    try:
        s = calendar.previous_trading_day(day)
    except ValueError:
        return None
    return s if s <= latest else None


def _write_days(days: list[date], force: bool) -> BackfillResult:
    """Read D01 once for the window and write every requested session that can be computed whole.

    Each day stands for its session (a Wednesday holiday for the Tuesday before), and a session that is not complete
    yet is not written. A session D01 has no SPY bar of, or too little SPY history before, is skipped and counted.
    """
    try:
        latest = calendar.latest_complete_session()
    except ValueError as exc:
        logger.warning("rotation backfill: no latest session: %s", exc)
        return BackfillResult(0)
    sessions = sorted({s for d in days if (s := _session_of(d, latest))})
    todo = [s for s in sessions if force or load_snapshot(s) is None]
    if not todo:
        return BackfillResult(0)
    tickers = engine.rrg_tickers()
    points = engine.d01_points(["SPY", *tickers], todo[0] - timedelta(weeks=engine.READ_WEEKS), todo[-1])
    spy_points = points.get("SPY") or []
    spy_days = {d for d, _ in spy_points}
    written = skipped = 0
    left: set[str] = set()
    generated_at = datetime.now().isoformat()
    for as_of in todo:
        spy = engine.weekly_closes(spy_points, as_of)
        if as_of not in spy_days or len(spy) < engine.MIN_WEEKS:
            skipped += 1
            continue
        closes = {t: engine.aligned_weekly_closes(points.get(t) or [], spy_points, as_of) for t in tickers}
        snaps = engine.cross_section(closes, spy, as_of)
        if not snaps:
            continue
        rows = []
        for snap in snaps:
            r = _snap_to_dict(snap)
            r["benchmark"] = "SPY"
            r["generated_at"] = generated_at
            r["source"] = "backfill"
            rows.append(r)
        rawstore.write_rows("rotation_snapshots", rows)
        written += 1
        left |= set(tickers) - {snap.ticker for snap in snaps}
    if skipped:
        logger.warning("rotation backfill: %d session(s) not written: D01 lacks SPY for them", skipped)
    return BackfillResult(written, skipped=skipped, left_out=[t for t in tickers if t in left])


def _wednesdays(start: date, end: date) -> list[date]:
    """Every Wednesday in [start, end]."""
    out = []
    d = start
    while d.weekday() != 2:  # advance to the first Wednesday
        d += timedelta(days=1)
    while d <= end:
        out.append(d)
        d += timedelta(days=7)
    return out


def backfill_weekly(uw, start_date: date, end_date: date = None,
                    force: bool = False) -> BackfillResult:
    """Reconstruct one snapshot per Wednesday from start_date through end_date.

    One D01 read for the window (no provider call; ``uw`` is unused and kept for callers). Each Wednesday is written
    as its session (the trading day on or before it); one that is not complete yet is not written. Skips stored
    sessions (idempotent). Sessions D01 cannot cover (no SPY bar, SPY history short) are counted, not written."""
    end_date = end_date or calendar.today_ny()
    wednesdays = _wednesdays(start_date, end_date)
    if not wednesdays:
        return BackfillResult(0)
    return _write_days(wednesdays, force)
