"""Baselines for the hit-rate ledger (#25 HR-1): what the same universe did after each freeze date.

A hit rate on its own says nothing — 41% means a different thing in a market where the average name rose 20% than in
one where it fell. ``core/outcomes.py`` records what happened to each frozen pick; this module records what the same
day's universe did, so the board can say *41.3% against 48.1%*, the measurement that told the truth in the reference
app (a 6.6 point gap a raw hit rate hid).

Two baselines, both computed from the store only (no provider call, no ledger write):

- **universe** — every ticker in that day's equity-universe snapshot, same MFE/MAE window and same nominal basis as
  ``forecast_outcomes`` (``outcomes.outcome`` is reused, so the two cannot drift apart on the rule).
- **vol_decile_<n>** — the same universe split into ten groups by trailing 20-session realised volatility, so a pick
  is judged against names that moved like it. Deciles, not quintiles: a coarse bucket hides the gap.

The snapshot is the store's own history (``rawstore.changes_as_of("equity_universe", ...)``), never today's
membership: a name that has since left the universe still counts on the day it was in it, or the baseline is
survivorship-biased. A freeze date before the store's first change capture has no snapshot, and the row says ``n = 0``
rather than quietly using today's list.

Rules (HR-1 of #25; every number below is a constant, so a change is one edit plus a ``MODEL_VERSION`` bump):
- One row per freeze date, horizon (``outcomes.HORIZONS``: 30 and 90 calendar days) and basis, always all eleven
  bases, so the table's key set never changes shape.
- A member counts when it has a usable reference price (``outcomes.outcome``) and is not flagged ``split_check``: a
  row on two price bases shows a return that is an artefact of the split, not a return. It is never silently dropped —
  ``n`` and the counts exclude it, and the frozen pick's own statuses stay the outcomes table's business.
- ``n`` counts the members behind the row; ``ge20``/``ge10``/``ge5`` are the cumulative counts of them whose MFE
  reached 20%, 10% and 5%, so the board reads any threshold off one row.
- The trailing vol is the annualised standard deviation of the last ``VOL_RETURNS`` daily log returns on the
  **adjusted** basis (splits and dividends), from the closes up to and including the freeze date: the board had that
  day's close when it picked. Nominal closes would read a split as a 50% day. A member with fewer returns is in the
  universe baseline and in no decile.
- A decile's ``vol_lo``/``vol_hi`` are its boundaries (lo inclusive, hi exclusive, NULL at either end), which is what
  ``basis_of`` reads: a pick's own trailing vol lands in exactly one row.
- The rows are ``open`` until the horizon's window has ended (``outcomes.window_end``), the run at or after that moment
  writes ``closed``, and the following nightly skips the date; ``full`` recomputes every date (after a bar backfill,
  or a rule change).
- With no daily bar stored at all while a freeze date is in the past, the run refuses before any write: it would
  otherwise turn every baseline into ``n = 0`` (the same guard ``core/outcomes.py`` carries).

Reads ``forecast_ledger`` (which dates to compute), ``equity_universe_changes`` and the D01 bars; writes
``forecast_baselines`` only, through ``rawstore.write_rows``. ``prices.daily_bars_bases`` reads the store only.
"""
from __future__ import annotations

import bisect
import logging
import math
import statistics
import time
from collections import Counter
from datetime import date, datetime, time as dt_time, timedelta

from RevenueChaser.core import calendar, outcomes, prices, rawstore

logger = logging.getLogger(__name__)

TABLE = "forecast_baselines"
UNIVERSE = "universe"
DECILES = 10
# Trailing returns of the vol estimate: 21 closes, the last one on the freeze date.
VOL_RETURNS = 20
# Calendar days read back for those sessions (20 sessions is about 29 days; the margin covers holidays).
VOL_LOOKBACK_DAYS = 45
TRADING_DAYS = 252                       # annualisation: a monotone scale, so the deciles do not feel it
GE = (("ge20", 0.20), ("ge10", 0.10), ("ge5", 0.05))     # the issue's buckets, cumulative
HORIZONS = outcomes.HORIZONS             # one definition of 30 and 90, never a second copy
OPEN, CLOSED = outcomes.OPEN, outcomes.CLOSED
# Bump when a rule above changes: every date is then recomputed, because the skip reads this column.
MODEL_VERSION = "b1:universe+trailing20v-deciles"
CHUNK = 200                              # tickers per bar read (the outcomes writer's chunk)


def day_end(day: date) -> float:
    """The epoch of ``day``'s last second in New York: the universe as the freeze evening left it."""
    return datetime.combine(day, dt_time(23, 59, 59), tzinfo=calendar.NY).timestamp()


def universe_at(day: date) -> list[str]:
    """The tickers the equity universe held at the end of ``day`` (empty: no snapshot that far back).

    A failed read is no snapshot (logged), never a reason to fall back on today's membership.
    """
    try:
        rows = rawstore.changes_as_of("equity_universe", day_end(day))
    except Exception as exc:  # noqa: BLE001 - no change history is no snapshot, never a wrong one
        logger.warning("baselines: the universe snapshot of %s could not be read (%s)", day, exc)
        return []
    return sorted({str(r.get("ticker") or "").strip().upper() for r in rows} - {""})


def _value(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def realised_vol(closes) -> float | None:
    """The annualised standard deviation of the last ``VOL_RETURNS`` daily log returns of ``closes``.

    ``closes`` are the adjusted closes in session order; one without a usable price is dropped before the count, so a
    gap costs a return rather than the whole estimate. Fewer than ``VOL_RETURNS`` returns is no estimate (``None``).
    """
    vals = [f for f in (_value(c) for c in closes) if f is not None and f > 0]
    if len(vals) < VOL_RETURNS + 1:
        return None
    tail = vals[-(VOL_RETURNS + 1):]
    returns = [math.log(b / a) for a, b in zip(tail, tail[1:])]
    return statistics.stdev(returns) * math.sqrt(TRADING_DAYS)


def decile_cuts(vols) -> list[float]:
    """The ``DECILES - 1`` boundaries that split ``vols`` into ten groups, ascending.

    Each boundary is the linear interpolation between the nearest two values, so a universe of any size yields the
    same number of ordered boundaries (and one of a single name yields nine equal ones).
    """
    xs = sorted(float(v) for v in vols)
    if not xs:
        return []
    cuts = []
    for k in range(1, DECILES):
        pos = k / DECILES * (len(xs) - 1)
        lo = math.floor(pos)
        hi = min(lo + 1, len(xs) - 1)
        cuts.append(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))
    return cuts


def decile_basis(index: int) -> str:
    """``"vol_decile_<index>"``: the basis name of decile ``index``, ``1`` the calmest .. ``DECILES`` the wildest."""
    return f"vol_decile_{index}"


def basis_of(vol: float, cuts: list[float]) -> str:
    """The decile basis a trailing ``vol`` belongs to, given the date's ``cuts``.

    Boundary ``k`` starts decile ``k + 2``, so a vol equal to a cut goes to the upper decile: ``vol_lo`` is inclusive
    and ``vol_hi`` exclusive, which is what ``vol_range`` stores.
    """
    return decile_basis(bisect.bisect_right(cuts, vol) + 1)


def vol_range(cuts: list[float], index: int) -> tuple[float | None, float | None]:
    """``(vol_lo, vol_hi)`` of decile ``index`` (1-based): lo inclusive, hi exclusive, NULL outside the data."""
    lo = cuts[index - 2] if 0 <= index - 2 < len(cuts) else None
    hi = cuts[index - 1] if 0 <= index - 1 < len(cuts) else None
    return lo, hi


def bucket_counts(values) -> dict:
    """``n``, the cumulative ``ge20``/``ge10``/``ge5`` counts and the mean of ``values`` (MFE fractions)."""
    vals = [float(v) for v in values]
    return {"n": len(vals),
            **{name: sum(1 for v in vals if v >= cut) for name, cut in GE},
            "mean": (sum(vals) / len(vals)) if vals else None}


def _mean(values) -> float | None:
    vals = [float(v) for v in values]
    return (sum(vals) / len(vals)) if vals else None


def _freeze_dates() -> list[date]:
    """The dates the ledger froze a pick on: the only dates a baseline exists for."""
    rows = rawstore.query(f"SELECT DISTINCT forecast_date AS d FROM {outcomes.LEDGER} WHERE target_type = ? "
                          "ORDER BY d", [outcomes.PICK])
    return [r["d"] for r in rows if r["d"] is not None]


def _done() -> set[date]:
    """The dates already final on the current ``MODEL_VERSION``: every horizon's universe row is closed."""
    marks = ", ".join("?" for _ in HORIZONS)
    rows = rawstore.query(
        f"SELECT forecast_date AS d FROM {TABLE} WHERE basis = ? AND model_version = ? AND status = ? "
        f"AND horizon_days IN ({marks}) GROUP BY forecast_date HAVING count(*) = ?",
        [UNIVERSE, MODEL_VERSION, CLOSED, *HORIZONS, len(HORIZONS)])
    return {r["d"] for r in rows}


def _by_ticker(frame) -> dict[str, list[dict]]:
    """The bars of a ``prices`` frame, per ticker, in session order (the shape ``outcomes.outcome`` reads)."""
    out: dict[str, list[dict]] = {}
    if frame.empty:
        return out
    for t, d, o, h, lo, c in zip(frame["ticker"], frame["session_date"], frame["open"], frame["high"], frame["low"],
                                 frame["close"]):
        out.setdefault(t, []).append({"session_date": d.date(), "open": o, "high": h, "low": lo, "close": c})
    return out


def _closes(bars: list[dict]) -> list[tuple[date, float]]:
    return [(b["session_date"], b["close"]) for b in bars]


def _membership(wanted: list[str], universes: dict[date, list[str]], latest: date
                ) -> tuple[dict[date, dict[str, dict]], int]:
    """``{date: {ticker: {'vol', 'mfe', 'mae'}}}`` for the tickers of that date's snapshot, read in chunks.

    One read per chunk of both bases (nominal for the outcomes rule, adjusted for the vol), bounded to the latest
    complete session. A member without bars is simply absent, which is what keeps it out of ``n``.
    """
    members: dict[date, dict[str, dict]] = {d: {} for d in universes}
    seen_in = {d: set(tickers) for d, tickers in universes.items()}      # a snapshot is read once per ticker
    bars_seen = 0
    for i in range(0, len(wanted), CHUNK):
        chunk = wanted[i:i + CHUNK]
        frames = prices.daily_bars_bases(chunk, start=min(universes) - timedelta(days=VOL_LOOKBACK_DAYS), end=latest)
        nominal, adjusted = _by_ticker(frames["nominal"]), _by_ticker(frames["adjusted"])
        for ticker in chunk:
            series, closes = nominal.get(ticker, []), _closes(adjusted.get(ticker, []))
            bars_seen += len(series)
            for day in [d for d in universes if ticker in seen_in[d]]:
                got = {h: outcomes.outcome(day, h, series, latest) for h in HORIZONS}
                flagged = {h: got[h]["status"] == outcomes.SPLIT_CHECK for h in HORIZONS}
                members[day][ticker] = {
                    "vol": realised_vol([c for sd, c in closes if sd <= day]),
                    "mfe": {h: None if flagged[h] else got[h]["mfe"] for h in HORIZONS},
                    "mae": {h: None if flagged[h] else got[h]["mae"] for h in HORIZONS},
                }
    return members, bars_seen


def _rows_for(day: date, members: dict[str, dict], latest: date, stamp: float, run_id: str | None) -> list[dict]:
    """The eleven rows of one freeze date (the universe and its ten deciles) at both horizons."""
    cuts = decile_cuts([m["vol"] for m in members.values() if m["vol"] is not None])
    groups: list[tuple[str, list[dict]]] = [(UNIVERSE, list(members.values()))]
    by_decile: dict[str, list[dict]] = {decile_basis(k): [] for k in range(1, DECILES + 1)}
    for m in members.values():
        if m["vol"] is not None:
            by_decile[basis_of(m["vol"], cuts)].append(m)
    groups += list(by_decile.items())

    rows = []
    for horizon in HORIZONS:
        status = CLOSED if outcomes.window_end(day, horizon) <= latest else OPEN
        for basis, group in groups:
            usable = [m for m in group if m["mfe"][horizon] is not None]
            counts = bucket_counts(m["mfe"][horizon] for m in usable)
            lo, hi = (None, None) if basis == UNIVERSE else vol_range(cuts, int(basis.rsplit("_", 1)[1]))
            rows.append({
                "forecast_date": day, "horizon_days": horizon, "basis": basis,
                "n": counts["n"], "ge20": counts["ge20"], "ge10": counts["ge10"], "ge5": counts["ge5"],
                "mfe_mean": counts["mean"],
                "mae_mean": _mean(m["mae"][horizon] for m in usable if m["mae"][horizon] is not None),
                "vol_lo": lo, "vol_hi": hi, "status": status,
                "computed_at": stamp, "model_version": MODEL_VERSION, "run_id": run_id,
            })
    return rows


def run(*, now: datetime | None = None, run_id: str | None = None, full: bool = False, write: bool = True) -> dict:
    """Compute and upsert the baseline rows of every freeze date that needs them; returns a summary.

    ``full=False`` (the nightly) leaves a date alone once its rows are closed on the current ``MODEL_VERSION``;
    ``write=False`` computes and counts only (a dry run). ``now`` is the clock (default: ``calendar``'s seam);
    ``computed_at`` is its epoch.
    """
    t0 = time.monotonic()
    latest = calendar.latest_complete_session(now)
    stamp = (now or calendar.now_ny()).timestamp()
    dates = _freeze_dates()
    todo = dates if full else [d for d in dates if d not in _done()]
    res = {"run_id": run_id, "latest_session": latest.isoformat(), "freeze_dates": len(dates), "dates": 0,
           "rows": 0, "written": 0, "tickers": 0, "no_snapshot": 0, "status": {}, "full": full,
           "dry_run": not write}
    if todo:
        universes = {d: universe_at(d) for d in todo}
        res["no_snapshot"] = sum(1 for tickers in universes.values() if not tickers)
        wanted = sorted({t for tickers in universes.values() for t in tickers})
        members, bars_seen = _membership(wanted, universes, latest)
        if wanted and not bars_seen and min(todo) < latest:
            raise RuntimeError("no daily bars in the price store: refusing to compute baselines "
                               "(every row would turn into n = 0)")
        status: Counter = Counter()
        rows_total = written = 0
        for day in todo:
            rows = _rows_for(day, members[day], latest, stamp, run_id)
            status.update(r["status"] for r in rows)
            rows_total += len(rows)
            if write:
                written += rawstore.write_rows(TABLE, rows)
        res.update(dates=len(todo), rows=rows_total, written=written, tickers=len(wanted),
                   status=dict(status))
    res["seconds"] = round(time.monotonic() - t0, 2)
    logger.info("forecast baselines: %s", res)
    return res
