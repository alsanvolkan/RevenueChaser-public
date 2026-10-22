"""Baselines for the hit-rate ledger (#25 HR-1): what the same universe did after each freeze date.

A hit rate on its own says nothing — 41% means a different thing in a market where the average name rose 20% than in
one where it fell. ``core/outcomes.py`` records what happened to each frozen pick; this module records what the same
day's universe did, so the board can say *41.3% against 48.1%*, the measurement that told the truth in the reference
app (a 6.6 point gap a raw hit rate hid).

Two baselines, both computed from the store only (no provider call, no ledger write):

- **universe** — every ticker in that day's equity-universe snapshot, same MFE/MAE window as ``forecast_outcomes``
  (``outcomes.outcome`` is reused, so the two cannot drift apart on the rule).
- **vol_decile_<n>** — the same universe split into ten groups by trailing 20-session realised volatility, so a pick
  is judged against names that moved like it. Deciles, not quintiles: a coarse bucket hides the gap.

**Two price bases, one stated choice.** The outcomes (MFE/MAE) are on the **nominal** basis; the risk measure (the
trailing volatility that places a member in a decile) is on the **adjusted** one. Nominal already applies every
split the store has recorded (``prices.apply_events``), so a split is the reason for neither choice — the reason for
the adjusted basis is **dividends**: an ex-dividend date is a real one-day fall of the printed price that is not a
move of the stock, and one such day inside a 20-session window distorts a volatility badly. A MFE/MAE over 30 or 90
days is not distorted by it, and the pick a baseline is compared with is measured on nominal prices
(``core/outcomes.py``), so the baseline is too.

The snapshot is the store's own history (``rawstore.changes_as_of("equity_universe", ...)``), never today's
membership: a name that has since left the universe still counts on the day it was in it, or the baseline is
survivorship-biased. A freeze date before the store's first change capture has no snapshot, and the row says ``n = 0``
rather than quietly using today's list.

Rules (HR-1 of #25; every number below is a constant, so a change is one edit plus a ``MODEL_VERSION`` bump):
- One row per freeze date, horizon (``outcomes.HORIZONS``: 30 and 90 calendar days) and basis, always all eleven
  bases, so the table's key set never changes shape.
- A member counts when it has a usable reference price (``outcomes.outcome``) and is not flagged ``split_check``.
  ``split_check`` is not a comparison of two price bases: it is a previous close -> open ratio at or past
  ``outcomes.SPLIT_JUMP`` (2.5) inside the window, on that one nominal series (``core/outcomes.py``) — a split whose
  action the store does not hold, so the window's prices are not one series and its MFE is not a return. It is never
  silently dropped: ``n`` and the counts exclude it, and the frozen pick's own statuses stay the outcomes table's
  business.
- ``n`` counts the members whose MFE is measurable; ``ge20``/``ge10``/``ge5`` are the cumulative counts of them whose
  MFE reached 20%, 10% and 5%, so the board reads any threshold off one row. ``mfe_mean`` averages those same members.
  ``mae_mean`` averages their MAE, which a bar can lack when its high is there and its low is not, so it can rest on
  fewer members than ``n`` says.
- The trailing vol is the annualised standard deviation of the last ``VOL_RETURNS`` daily log returns of the
  **adjusted** closes, from the closes up to and including the freeze date: the board had that day's close when it
  picked. A member with fewer returns is in the universe baseline and in no decile.
- Ten deciles need at least ``DECILES`` members with a trailing vol. With fewer, the boundaries would separate single
  names, and a "decile" of one name is not a peer group: every decile row is then written with ``n = 0`` and those
  names are counted in the universe row alone (a single-name universe is in no decile, not in decile 10).
- A decile's ``vol_lo``/``vol_hi`` are its boundaries (lo inclusive, hi exclusive, NULL at either end), which is what
  ``basis_of`` reads: a pick's own trailing vol lands in exactly one row.
- The rows are ``open`` until the horizon's window has ended (``outcomes.window_end``) **and** the store holds a bar on
  or after that end (``outcomes._newest_bar``), which is the rule ``core/outcomes.py`` closes a pick on; the run past
  both moments writes ``closed``, and the following nights recompute only the rows still open (a date whose 30-day
  window has ended keeps its 30-day rows and is recomputed for its 90-day ones); ``full`` recomputes every date (after
  a bar backfill, or a rule change). A close on the calendar alone would freeze a row on a night whose Daily Bars had
  failed, because a closed row is never rewritten.
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
from typing import NamedTuple

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
MODEL_VERSION = "b2:universe+trailing20v-deciles"
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
    missing bar does not throw the estimate away — the return that follows it spans two sessions instead. Fewer than
    ``VOL_RETURNS`` returns is no estimate (``None``).
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
    same number of ordered boundaries. On fewer values than ``DECILES`` those boundaries would separate single names:
    that is not a peer grouping, so ``_rows_for`` writes no deciles at all (see the rules above).
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


def _open_rows(dates: list[date]) -> dict[date, tuple[int, ...]]:
    """The horizons still to compute, per freeze date: those whose universe row is not ``closed`` on this
    ``MODEL_VERSION``.

    A date whose 30-day window has ended keeps its 30-day rows and is recomputed for its 90-day ones alone, so a
    nightly never rewrites a row that is already final.
    """
    if not dates:
        return {}
    marks = ", ".join("?" for _ in HORIZONS)
    rows = rawstore.query(
        f"SELECT forecast_date AS d, horizon_days AS h FROM {TABLE} WHERE basis = ? AND model_version = ? "
        f"AND status = ? AND horizon_days IN ({marks})",
        [UNIVERSE, MODEL_VERSION, CLOSED, *HORIZONS])
    closed = {(r["d"], int(r["h"])) for r in rows}
    todo: dict[date, tuple[int, ...]] = {}
    for day in dates:
        open_horizons = tuple(h for h in HORIZONS if (day, h) not in closed)
        if open_horizons:
            todo[day] = open_horizons
    return todo


def _by_ticker(frame) -> dict[str, list[dict]]:
    """The bars of a ``prices`` frame, per ticker, in session order (the shape ``outcomes.outcome`` reads)."""
    out: dict[str, list[dict]] = {}
    if frame.empty:
        return out
    for t, d, o, h, lo, c in zip(frame["ticker"], frame["session_date"], frame["open"], frame["high"], frame["low"],
                                 frame["close"]):
        out.setdefault(t, []).append({"session_date": d.date(), "open": o, "high": h, "low": lo, "close": c})
    return out


class Member(NamedTuple):
    """One member of a freeze date: its trailing vol and, per horizon in ``HORIZONS`` order, its MFE and its MAE.

    A horizon the run does not need, or one flagged ``split_check``, holds ``None``. One flat tuple rather than a
    dict per measure: a nightly builds hundreds of thousands of these.
    """
    vol: float | None
    mfe: tuple[float | None, ...]
    mae: tuple[float | None, ...]


def _usable_closes(bars: list[dict]) -> tuple[list[date], list[float]]:
    """The sessions and adjusted closes of a ticker that can enter a log return (a usable price only), in order."""
    got = [(b["session_date"], _value(b["close"])) for b in bars]
    got = [(d, c) for d, c in got if c is not None and c > 0]
    return [d for d, _ in got], [c for _, c in got]


def _vols_at(days: list[date], dates: list[date], closes: list[float]) -> list[float | None]:
    """The trailing vol of each of ``days`` (ascending) from the usable ``closes``, one 21-close window each.

    The window is found by bisect instead of re-filtering the whole series per day: the values and the order are the
    ones ``realised_vol`` would see, so the estimate is the same number.
    """
    out = []
    for day in days:
        i = bisect.bisect_right(dates, day)
        out.append(realised_vol(closes[max(0, i - VOL_RETURNS - 1):i]))
    return out


def _member(vol: float | None, got: dict[int, dict]) -> Member:
    """A member from its trailing ``vol`` and its outcomes ``{horizon: outcome}`` (a flagged horizon has no MFE)."""
    mfe, mae = [], []
    for h in HORIZONS:
        skip = h not in got or got[h]["status"] == outcomes.SPLIT_CHECK
        mfe.append(None if skip else got[h]["mfe"])
        mae.append(None if skip else got[h]["mae"])
    return Member(vol, tuple(mfe), tuple(mae))


def _membership(wanted: list[str], universes: dict[date, list[str]], horizons: dict[date, tuple[int, ...]],
                latest: date) -> tuple[dict[date, list[Member]], int]:
    """``{date: [Member]}`` for the tickers of that date's snapshot, read in chunks.

    One read per chunk of both bases (nominal for the outcomes, adjusted for the vol), bounded to the latest complete
    session. Per ticker the usable adjusted closes are filtered once and each day's vol is the 21-close window ending
    there; the day's outcomes for every horizon still open come from one pass over its bars
    (``outcomes.outcome_by_horizon``). A member the store has no bars for is **not** absent: it is a ``Member`` with
    ``vol=None`` and a ``None`` MFE per horizon, which the ``mfe[pos] is not None`` filter in ``_rows_for`` keeps out
    of ``n`` (and out of every decile, since it has no vol).
    """
    members: dict[date, list[Member]] = {d: [] for d in universes}
    seen_in = {d: set(tickers) for d, tickers in universes.items()}      # a snapshot is read once per ticker
    bars_seen = 0
    for i in range(0, len(wanted), CHUNK):
        chunk = wanted[i:i + CHUNK]
        frames = prices.daily_bars_bases(chunk, start=min(universes) - timedelta(days=VOL_LOOKBACK_DAYS), end=latest)
        nominal, adjusted = _by_ticker(frames["nominal"]), _by_ticker(frames["adjusted"])
        for ticker in chunk:
            days = sorted(d for d in universes if ticker in seen_in[d])
            series = nominal.get(ticker, [])
            bars_seen += len(series)
            if not days:
                continue
            dates, closes = _usable_closes(adjusted.get(ticker, []))
            for day, vol in zip(days, _vols_at(days, dates, closes)):
                members[day].append(_member(vol, outcomes.outcome_by_horizon(day, horizons[day], series, latest)))
    return members, bars_seen


def _rows_for(day: date, members: list[Member], horizons: tuple[int, ...], latest: date, newest: date | None,
              stamp: float, run_id: str | None) -> list[dict]:
    """The eleven rows of one freeze date (the universe and its ten deciles) for the ``horizons`` still open.

    ``latest`` is the newest complete session (the clock) and ``newest`` the newest bar the store holds
    (``outcomes._newest_bar``): a row closes only when both have passed its window end, so a night without Daily Bars
    leaves it open instead of freezing it on the bars the store happens to have.
    """
    vols = [m.vol for m in members if m.vol is not None]
    # Fewer than ten usable vols cannot make ten groups, and a "decile" of one name is not a peer group: then every
    # decile row is written empty and the names are counted in the universe row alone.
    cuts = decile_cuts(vols) if len(vols) >= DECILES else []
    groups: list[tuple[str, list[Member]]] = [(UNIVERSE, members)]
    by_decile: dict[str, list[Member]] = {decile_basis(k): [] for k in range(1, DECILES + 1)}
    if cuts:
        for m in members:
            if m.vol is not None:
                by_decile[basis_of(m.vol, cuts)].append(m)
    groups += list(by_decile.items())

    rows = []
    for horizon in horizons:
        pos = HORIZONS.index(horizon)
        ended = outcomes.window_end(day, horizon)
        status = CLOSED if ended <= latest and newest is not None and newest >= ended else OPEN
        for basis, group in groups:
            usable = [m for m in group if m.mfe[pos] is not None]
            counts = bucket_counts(m.mfe[pos] for m in usable)
            lo, hi = (None, None) if basis == UNIVERSE else vol_range(cuts, int(basis.rsplit("_", 1)[1]))
            rows.append({
                "forecast_date": day, "horizon_days": horizon, "basis": basis,
                "n": counts["n"], "ge20": counts["ge20"], "ge10": counts["ge10"], "ge5": counts["ge5"],
                "mfe_mean": counts["mean"],
                "mae_mean": _mean(m.mae[pos] for m in usable if m.mae[pos] is not None),
                "vol_lo": lo, "vol_hi": hi, "status": status,
                "computed_at": stamp, "model_version": MODEL_VERSION, "run_id": run_id,
            })
    return rows


def run(*, now: datetime | None = None, run_id: str | None = None, full: bool = False, write: bool = True) -> dict:
    """Compute and upsert the baseline rows of every freeze date that needs them; returns a summary.

    ``full=False`` (the nightly) leaves a row alone once it is closed on the current ``MODEL_VERSION``, and reads a
    date once for the horizons still open; ``write=False`` computes and counts only (a dry run). ``now`` is the clock
    (default: ``calendar``'s seam); ``computed_at`` is its epoch.
    """
    t0 = time.monotonic()
    latest = calendar.latest_complete_session(now)
    stamp = (now or calendar.now_ny()).timestamp()
    dates = _freeze_dates()
    todo = {d: HORIZONS for d in dates} if full else _open_rows(dates)
    res = {"run_id": run_id, "latest_session": latest.isoformat(), "freeze_dates": len(dates), "dates": 0,
           "rows": 0, "written": 0, "tickers": 0, "no_snapshot": 0, "status": {}, "full": full,
           "dry_run": not write}
    if todo:
        universes = {d: universe_at(d) for d in todo}
        res["no_snapshot"] = sum(1 for tickers in universes.values() if not tickers)
        wanted = sorted({t for tickers in universes.values() for t in tickers})
        members, bars_seen = _membership(wanted, universes, todo, latest)
        newest = outcomes._newest_bar()          # the store's newest bar, not the clock (see _rows_for)
        if wanted and not bars_seen and min(todo) < latest:
            raise RuntimeError("no daily bars in the price store: refusing to compute baselines "
                               "(every row would turn into n = 0)")
        status: Counter = Counter()
        rows_total = written = 0
        for day in todo:
            rows = _rows_for(day, members[day], todo[day], latest, newest, stamp, run_id)
            status.update(r["status"] for r in rows)
            rows_total += len(rows)
            if write:
                written += rawstore.write_rows(TABLE, rows)
        res.update(dates=len(todo), rows=rows_total, written=written, tickers=len(wanted),
                   status=dict(status))
    res["seconds"] = round(time.monotonic() - t0, 2)
    logger.info("forecast baselines: %s", res)
    return res
