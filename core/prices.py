"""Daily price bars: the writer, the fill, the read API and the pure helpers (#156).

One row per ``(ticker, New York session date)`` in ``price_bars_daily``, nominal as served
at ``fetched_at``. Splits and dividends are never written into a bar; they are rows of
``price_actions`` and are applied on read by :func:`daily_bars` (R2). The first write of a
key wins (the table is ``append``), and a bar is never rewritten.

Two entry points share one lock (R7):

- :func:`ensure_daily_bars` — the nightly: UW ``ohlc/1d`` for the gap, a per-chunk yfinance
  download for the fallback, the adj_close and the actions, up to the latest complete
  session. It never fills a whole history by itself (that is slice B's attended ``--fill``).
- :func:`fill_history` — the attended ``period="max"`` fill, SPY written first as the
  pre-2025 reference.

The clock is ``now`` (an aware datetime) when given, else the calendar seam
(``core/calendar._now``). This module never calls ``datetime.now`` itself.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone

import duckdb
import numpy as np
import pandas as pd

from RevenueChaser.core import calendar, job_control, job_lock, ledger, rawstore
from RevenueChaser.core import datahealth
from RevenueChaser.core.datahealth import Health
from RevenueChaser.core.providers.ohlc import regular_session_bars
from RevenueChaser.core.rotation_reference import ROTATION_ETFS

logger = logging.getLogger(__name__)

UW_NIGHTLY_CAP = 9000          # D2; counted from uw.health()["calls"] since the run started
UW_MAX_DAYS = 240              # UW ohlc/1d defaults to timeframe=1Y; the client sends only `limit`
CHUNK = 100
OVERLAP_SESSIONS = 4
ACTIONS_LOOKBACK_DAYS = 10     # calendar days before the watermark
FAR_DAYS = 30                  # a ticker whose start is older goes in its own download
MAX_NEW_FILL = 50              # new tickers given a period="max" fill per nightly run
FILL_RETRY_DAYS = 7            # a ticker whose fill returned no rows is retried after this
BENCHMARKS = ("SPY", "DIA", "QQQ")
BAR_TIERS = ("A", "B", "C")    # the spine tiers the nightly asks, in order (rcdata/build/spine/tier.py):
                               # A and B listed, C over the counter; D has no exchange to trade on
REFERENCE_INDEX = "^GSPC"      # dates only; never stored here (#293 stores index levels)
CALENDAR_START = date(2025, 1, 1)
CHECK_ACTIONS = ("price_actions", "yfinance")        # price_checks (dataset, source); key = ticker
CHECK_FILL = ("price_bars_daily", "yfinance_max")    # fill marker; key = ticker
# The file of the three price tables (prices.duckdb since #156, avo 2026-10-09): every read and every chunk
# transaction here names it, so a chunk's bars, actions and watermarks commit together (R5).
_DB = rawstore.MANIFEST["price_bars_daily"].get("db", "raw")
YF_KW = dict(interval="1d", auto_adjust=False, back_adjust=False, actions=True, repair=False,
             keepna=False, group_by="ticker", threads=True, progress=False)

_BAR_COLUMNS = ["ticker", "session_date", "open", "high", "low", "close", "volume", "source"]
_YF_FIELDS = ("Open", "High", "Low", "Close", "Adj Close", "Volume",
              "Dividends", "Stock Splits", "Capital Gains")


# ── small helpers ────────────────────────────────────────────────────────────

def _now(now: datetime | None) -> datetime:
    """The run clock: ``now`` when given, else the calendar seam (never ``datetime.now``)."""
    return now if now is not None else calendar._now()


def _f(v):
    """A finite float, else None (a string number, NaN and None all become None)."""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def _stamper(now_given: bool, run_at: float):
    """``fetched_at`` for data that just came back (§9.2): the moment it arrived, read from the
    calendar seam right after each call returns. A caller that passes ``now`` (a test) gets that
    fixed instant instead. The split gate compares this value with 09:30 NY on the ex-date, so a
    run-start stamp would divide rows a long run fetched after a split's open a second time."""
    if now_given:
        return lambda: run_at
    return lambda: calendar._now().timestamp()


def _price(v):
    """A positive finite price, else None. Yahoo sometimes serves 0 where trades occurred
    (``repair=False`` passes it through); under first write wins a stored 0 could never be
    replaced, while a hole can still be filled by a later run's overlap (N11)."""
    x = _f(v)
    return x if x is not None and x > 0 else None


def _threshold(ex_date: date) -> datetime:
    """``ex_date`` at 09:30 New York: the split's ex-date open (R6)."""
    return datetime.combine(ex_date, calendar.SESSION_OPEN, tzinfo=calendar.NY)


def _threshold_epoch(ex_date: date) -> float:
    return _threshold(ex_date).timestamp()


def _epoch_date(ts: float | None) -> date | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()


def _add_error(res: dict, name: str) -> None:
    if name not in res["errors"]:
        res["errors"].append(name)


def _chunks(items: list[str], size: int):
    size = max(1, int(size))
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _placeholders(n: int) -> str:
    return ", ".join("?" for _ in range(n))


# ── scope ────────────────────────────────────────────────────────────────────

def bar_scope() -> list[str]:
    """The tickers the nightly keeps bars for (R8): the universe, the benchmarks and the
    sector/theme rotation ETFs, upper-cased, de-duplicated, benchmarks first.

    The universe is the boards' (``core.universe``): while they read the identity spine, its names of
    ``BAR_TIERS`` in tier order, Tier A (the names a board can screen; the prices store asks those,
    ``rcdata.build.prices.scope``) before the rotation ETFs and Tiers B and C after them (#262); on the old store,
    its ``equity_universe``. The run spends its UW cap (``UW_NIGHTLY_CAP``) and its new-name fills
    (``MAX_NEW_FILL``) in this order, so the caps leave out Tier B and C first: a name past the UW cap still gets
    Yahoo's bar, and a name the night's fills miss is filled on a later night."""
    from RevenueChaser.core import universe as universe_mod
    etfs = [str(t).strip().upper() for t, info in ROTATION_ETFS.items()
            if (info or {}).get("group") not in ("yield", "commodity")]
    if universe_mod.universe_source() == universe_mod.SPINE:
        from RevenueChaser.rcdata import read
        rows = read.universe(tiers=BAR_TIERS)                       # by ticker
        tier = {t: [r["ticker"] for r in rows if r["tier"] == t] for t in BAR_TIERS}
        return _norm([*BENCHMARKS, *tier["A"], *etfs, *(x for t in BAR_TIERS[1:] for x in tier[t])])
    try:
        rows = rawstore.query("SELECT ticker FROM equity_universe")
    except Exception:  # noqa: BLE001 - a missing table is "no universe", not a crash
        rows = []
    universe = [str(r["ticker"]).strip().upper() for r in rows if r.get("ticker")]
    return _norm([*BENCHMARKS, *universe, *etfs])


def _norm(tickers) -> list[str]:
    """Upper-case, strip, de-duplicate, benchmarks first."""
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(tickers, str):            # one ticker, not its characters
        tickers = [tickers]
    # An index level (^GSPC, ^VIX) is never a bar here (#293 stores index levels): only the
    # fill's reference download names one, and it never writes it.
    values = [v for v in (str(t).strip().upper() for t in (tickers or [])) if not v.startswith("^")]
    for b in BENCHMARKS:
        if b in values and b not in seen:
            seen.add(b)
            out.append(b)
    for t in values:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ── pure helpers (no store, no network) ──────────────────────────────────────

def sessions_behind(last: date | None, latest: date) -> int | None:
    """Trading days in ``(last, latest]``; ``None`` when ``last`` predates the calendar."""
    if last is None or last < CALENDAR_START:
        return None
    n = 0
    d = last + timedelta(days=1)
    while d <= latest:
        if calendar.is_trading_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def uw_days(n: int | None) -> int | None:
    """Sessions to ask UW for, or ``None`` to use yfinance (deep gap or pre-2025)."""
    if n is None:
        return None
    days = max(5, n + OVERLAP_SESSIONS)
    return None if days > UW_MAX_DAYS else days


def keep_session(d: date, latest: date, reference: set[date]) -> str | None:
    """``None`` to keep ``d``, else the drop reason (``off_calendar`` / ``off_reference``).

    Rows from 2025 on must be complete calendar sessions; rows before 2025 are kept only on
    a reference date (SPY's dates, plus ``^GSPC`` before SPY's first). The calendar is never
    called for a pre-2025 date (R3).
    """
    if d < CALENDAR_START:
        return None if d in reference else "off_reference"
    if d > latest or not calendar.is_trading_day(d):
        return "off_calendar"
    return None


def _action(ticker: str, ex_date: date, action: str, value: float,
            fetched_at: float, run_id: str, run_at: float) -> dict:
    return {"ticker": ticker, "ex_date": ex_date, "action": action, "value": float(value),
            "session_date": ex_date, "source": "yfinance",
            "fetched_at": fetched_at, "run_id": run_id, "run_at": run_at}


def rows_from_uw(ticker: str, bars, *, fetched_at: float, run_id: str, run_at: float,
                 counts: dict | None = None) -> list[dict]:
    """``price_bars_daily`` rows from a UW ``ohlc/1d`` reply (regular-session bars). A bar without
    a positive close is dropped and counted in ``counts['dropped_no_close']`` when given."""
    out: list[dict] = []
    for row in regular_session_bars(bars):
        raw = str(row.get("date") or row.get("datetime") or "")[:10]
        if not raw:
            continue
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            continue
        close = _price(row.get("close"))
        if close is None:
            _count_no_close(counts)
            continue
        out.append({
            "ticker": ticker, "session_date": d,
            "open": _price(row.get("open")), "high": _price(row.get("high")),
            "low": _price(row.get("low")), "close": close,
            "adj_close": None, "volume": _f(row.get("volume")),
            "split_ratio": None, "adj_source": None, "source": "uw",
            "fetched_at": fetched_at, "run_id": run_id, "run_at": run_at,
        })
    return out


def _frame_tickers(frame, tickers: list[str]) -> list[str]:
    if isinstance(frame.columns, pd.MultiIndex):
        present = set(frame.columns.get_level_values(0))
        return [t for t in tickers if t in present]
    return list(tickers) if len(tickers) == 1 else []


def _sub_frame(frame, ticker: str):
    if isinstance(frame.columns, pd.MultiIndex):
        return frame[ticker]
    return frame


def _count_no_close(counts: dict | None) -> None:
    """Count one bad bar *seen* (#156 N12): not rows lost. A date can count once per source and again
    each night while it stays inside the overlap window, so read it as a diagnostic."""
    if counts is not None:
        counts["dropped_no_close"] = counts.get("dropped_no_close", 0) + 1


def rows_from_yf(frame, tickers, *, fetched_at: float, run_id: str,
                 run_at: float, counts: dict | None = None) -> tuple[dict, dict]:
    """``(bars, actions)`` by ticker from a ``yf.download`` frame.

    Accepts a ``(ticker, field)`` MultiIndex frame, or a flat frame when one ticker is asked;
    the index is normalised with ``pd.Timestamp(ts).date()`` (naive or tz-aware).
    """
    bars: dict[str, list[dict]] = {}
    actions: dict[str, list[dict]] = {}
    if frame is None or len(frame) == 0:
        return bars, actions
    for t in _frame_tickers(frame, list(tickers)):
        sub = _sub_frame(frame, t)
        b_rows: list[dict] = []
        div: dict[date, float] = {}
        gains: dict[date, float] = {}
        splits: dict[date, float] = {}
        for idx, row in sub.iterrows():
            d = pd.Timestamp(idx).date()
            dv = _f(row.get("Dividends"))
            if dv is not None and dv > 0:
                div[d] = div.get(d, 0.0) + dv
            gv = _f(row.get("Capital Gains"))
            if gv is not None and gv > 0:
                gains[d] = gains.get(d, 0.0) + gv
            sv = _f(row.get("Stock Splits"))
            if sv is not None and sv > 0 and sv != 1:
                splits[d] = splits.get(d, 1.0) * sv
            close = _price(row.get("Close"))
            if close is None:
                # A NaN close is a union-index row the ticker never traded; a 0 close is a bad bar.
                if _f(row.get("Close")) is not None:
                    _count_no_close(counts)
                continue
            adj = _price(row.get("Adj Close"))
            b_rows.append({
                "ticker": t, "session_date": d,
                "open": _price(row.get("Open")), "high": _price(row.get("High")),
                "low": _price(row.get("Low")), "close": close,
                "adj_close": adj, "volume": _f(row.get("Volume")),
                "split_ratio": None, "adj_source": "yfinance" if adj is not None else None,
                "source": "yfinance", "fetched_at": fetched_at, "run_id": run_id, "run_at": run_at,
            })
        bars[t] = b_rows
        a_rows: list[dict] = []
        for d, v in sorted(div.items()):
            a_rows.append(_action(t, d, "dividend", v, fetched_at, run_id, run_at))
        for d, v in sorted(splits.items()):
            if v != 1:                      # duplicates that multiply to 1 are no split (§9.2)
                a_rows.append(_action(t, d, "split", v, fetched_at, run_id, run_at))
        for d, v in sorted(gains.items()):
            a_rows.append(_action(t, d, "capital_gain", v, fetched_at, run_id, run_at))
        actions[t] = a_rows
    return bars, actions


def _yf_download(tickers, **kw):
    """The real yfinance call — imported lazily so no test or store-only path loads it."""
    import yfinance as yf
    return yf.download(list(tickers), **kw)


class _EventRows(list):
    """A list of bar rows that also carries ``attrs`` (``skipped_events``)."""

    def __init__(self, *args, attrs: dict | None = None):
        super().__init__(*args)
        self.attrs = attrs or {}


# ── the event engine (K8a speed, #383): numpy over every bar of every ticker at once ────────────────────────
# Each factor is a left fold in the actions' list order, ``acc = op(acc, v)`` for every event whose condition holds,
# exactly as the per-row loop did it (k *= r, a' /= r, m *= f), so the floats are bit-identical, not merely close:
# the loop runs over the event position j (a ticker's 0th, 1st, ... event) and is vectorised over the items.

def _group_index(groups: np.ndarray, n_groups: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(order, first, count)``: a stable sort of the items by group (list order kept within a group), and each
    group's first position and size in that order."""
    order = np.argsort(groups, kind="stable")
    count = np.bincount(groups, minlength=n_groups)
    return order, np.cumsum(count) - count, count


def _fold(acc: np.ndarray, owner: np.ndarray, first: np.ndarray, count: np.ndarray, values: np.ndarray,
          cond, op) -> np.ndarray:
    """``acc[i] = op(acc[i], values[j])`` over the events ``j`` of item ``i``'s group, in list order, where
    ``cond(i, j)``. ``first``/``count`` index the events by group (from :func:`_group_index`)."""
    n_ev = count[owner]
    for j in range(int(n_ev.max(initial=0))):
        items = np.flatnonzero(n_ev > j)
        evs = first[owner[items]] + j
        hit = cond(items, evs)
        items, evs = items[hit], evs[hit]
        acc[items] = op(acc[items], values[evs])
    return acc


def _event_factors(g: np.ndarray, d: np.ndarray, fetched: np.ndarray, close: np.ndarray, close_null: np.ndarray,
                   n_groups: int, splits: tuple, amounts: tuple | None
                   ) -> tuple[np.ndarray, np.ndarray | None, int]:
    """``(k, m, skipped)`` per bar: the split divisor ``k`` and, when ``amounts`` is given, the amount multiplier.

    - ``g``/``d``: each bar's group and day number, sorted by ``(g, d)`` with ties in list order; ``fetched`` its
      fetched_at (None as 0); ``close``/``close_null`` the stored close and where it is None.
    - ``splits``: ``(group, ex_day, T(e) epoch, ratio)`` and ``amounts``: ``(group, ex_day, fetched_at, value)``,
      each in the actions' list order and already filtered to a usable value.
    """
    sg, s_ex, s_thr, s_val = splits
    s_ord, s_first, s_count = _group_index(sg, n_groups)
    s_ex, s_thr, s_val = s_ex[s_ord], s_thr[s_ord], s_val[s_ord]
    k = _fold(np.ones(len(g)), g, s_first, s_count, s_val,
              lambda i, j: (d[i] < s_ex[j]) & (fetched[i] < s_thr[j]), np.multiply)
    if amounts is None:
        return k, None, 0
    ag, a_ex, a_fetched, a_val = amounts
    if not len(ag):
        return k, np.ones(len(g)), 0
    if not len(g):
        return k, np.ones(0), len(ag)                      # no bar: every amount lacks C_prev
    base = int(d.min()) - 1                                # a bar's offset is in [1, width - 2]
    width = int(d.max()) - base + 2
    bar_key = g * width + (d - base)

    def key(groups, days):
        return groups * width + np.clip(days - base, 0, width - 1)

    # C_prev: the nominal close of the group's last bar before the ex-date (the last of equal dates, as the loop)
    pos = np.searchsorted(bar_key, key(ag, a_ex), side="left") - 1
    at = np.clip(pos, 0, None)
    has = (pos >= 0) & (g[at] == ag) & ~close_null[at]
    with np.errstate(all="ignore"):
        c_prev = close[at] / k[at]
        a_prime = _fold(a_val.copy(), ag, s_first, s_count, s_val,
                        lambda i, j: (a_ex[i] < s_ex[j]) & (a_fetched[i] < s_thr[j]), np.divide)
        m = 1.0 - a_prime / c_prev
        bad = ~has | (c_prev <= 0) | (m <= 0.0) | (m >= 1.0)
    keep = ~bad
    fg, fe, fm = ag[keep], a_ex[keep], m[keep]
    if not len(fg):
        return k, np.ones(len(g)), int(bad.sum())
    # A bar's multiplier depends only on how many of its group's distinct factor ex-dates are on or before it (its
    # segment), so the fold runs once per (group, segment), not once per bar.
    fkey = key(fg, fe)
    ukey = np.unique(fkey)
    u_count = np.bincount(ukey // width, minlength=n_groups)
    u_first = np.cumsum(u_count) - u_count
    rank = np.searchsorted(ukey, fkey) - u_first[fg]       # a factor applies to segment s while rank >= s
    p_count = u_count + 1
    p_first = np.cumsum(p_count) - p_count
    p_group = np.repeat(np.arange(n_groups), p_count)
    p_seg = np.arange(len(p_group)) - p_first[p_group]
    f_ord, f_first, f_count = _group_index(fg, n_groups)
    f_rank, f_m = rank[f_ord], fm[f_ord]
    per_seg = _fold(np.ones(len(p_group)), p_group, f_first, f_count, f_m,
                    lambda i, j: f_rank[j] >= p_seg[i], np.multiply)
    seg = np.searchsorted(ukey, bar_key, side="right") - u_first[g]
    return k, per_seg[p_first[g] + seg], int(bad.sum())


def _usable(actions, kinds: tuple[str, ...]) -> list[dict]:
    """The actions of ``kinds`` with a usable value, in list order."""
    return [a for a in actions if a.get("action") in kinds and _f(a.get("value"))]


def _split_arrays(splits: list[tuple[int, dict]]) -> tuple:
    return (np.array([gi for gi, _ in splits], dtype=np.int64),
            np.array([a["ex_date"].toordinal() for _, a in splits], dtype=np.int64),
            np.array([_threshold_epoch(a["ex_date"]) for _, a in splits], dtype=float),
            np.array([float(a["value"]) for _, a in splits], dtype=float))


def _amount_arrays(amounts: list[tuple[int, dict]]) -> tuple:
    return (np.array([gi for gi, _ in amounts], dtype=np.int64),
            np.array([a["ex_date"].toordinal() for _, a in amounts], dtype=np.int64),
            np.array([float(a.get("fetched_at") or 0.0) for _, a in amounts], dtype=float),
            np.array([float(a["value"]) for _, a in amounts], dtype=float))


def apply_events(bars: list[dict], actions: list[dict], basis: str) -> list[dict]:
    """The nominal or split/dividend-adjusted series of one ticker (R2/R6).

    - Nominal: OHLC ÷ ``k_i`` and volume × ``k_i``, ``k_i = Π r`` over splits with
      ``d_i < e`` and ``fetched_at_i < T(e)``.
    - Adjusted: nominal × ``Π m`` over dividends and capital gains with ``d_i < e``, where
      ``m = 1 − a'/C_prev``; ``a'`` divides the amount by each split that took effect after
      the amount was fetched. An event with no ``C_prev`` or ``m`` outside ``(0, 1)`` is
      skipped and counted in ``attrs['skipped_events']``. Volume is never dividend-adjusted.

    Computed by the vectorised engine (:func:`_event_factors`, #383 K8a speed): the same floats as the per-row
    formulas. The read path (:func:`daily_bars`) runs the engine over every ticker at once, not this per ticker.
    """
    if basis not in ("nominal", "adjusted"):
        raise ValueError(f"unknown basis {basis!r}")
    ordered = sorted(bars, key=lambda b: b["session_date"])
    fields = _FIELDS
    raw = {f: [b.get(f) for b in ordered] for f in fields}
    null = {f: [v is None for v in raw[f]] for f in fields}
    vals = {f: np.array([0.0 if v is None else v for v in raw[f]], dtype=float) for f in fields}
    d = np.array([b["session_date"].toordinal() for b in ordered], dtype=np.int64)
    fetched = np.array([float(b.get("fetched_at") or 0.0) for b in ordered], dtype=float)
    splits = _split_arrays([(0, a) for a in _usable(actions, ("split",))])
    amounts = (_amount_arrays([(0, a) for a in _usable(actions, _AMOUNTS)])
               if basis == "adjusted" else None)
    k, m, skipped = _event_factors(np.zeros(len(ordered), dtype=np.int64), d, fetched, vals["close"],
                                   np.array(null["close"], dtype=bool), 1, splits, amounts)
    nominal = {f: (vals[f] * k if f == "volume" else vals[f] / k) for f in fields}
    out = {f: (nominal[f] * m if m is not None and f != "volume" else nominal[f]).tolist() for f in fields}
    rows: list[dict] = []
    for i, b in enumerate(ordered):
        row = dict(b)
        for f in fields:
            row[f] = None if null[f][i] else out[f][i]
        row["split_ratio"] = None
        rows.append(row)
    return _EventRows(rows, attrs={"skipped_events": skipped})


def _stat_block(drifts: list[float]) -> dict:
    n = len(drifts)
    if not n:
        return {"n": 0, "median": None, "p90": None, "over_1pct": 0}
    s = sorted(drifts)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    p90 = s[min(n - 1, max(0, math.ceil(0.9 * n) - 1))]
    return {"n": n, "median": median, "p90": p90,
            "over_1pct": sum(1 for x in drifts if abs(x) > 0.01)}


def overlap_stats(pairs: list[tuple[dict, dict]]) -> dict:
    """Compare UW rows with the stored rows for the same keys (R1 diagnostics)."""
    drifts: list[float] = []
    by_source: dict[str, list[float]] = {}
    close_mismatch = close_step = 0
    examples: list[dict] = []
    for uw_row, stored in pairs:
        sv, uv = _f(stored.get("volume")), _f(uw_row.get("volume"))
        if sv and uv is not None:
            drift = uv / sv - 1.0
            src = stored.get("source") or "unknown"
            drifts.append(drift)
            by_source.setdefault(src, []).append(drift)
            if abs(drift) > 0.01 and len(examples) < 20:
                examples.append({"ticker": stored.get("ticker"), "kind": "volume",
                                 "stored": sv, "now": uv, "drift": drift})
        sc, uc = _f(stored.get("close")), _f(uw_row.get("close"))
        if sc and uc is not None:
            rel = abs(uc / sc - 1.0)
            if rel > 0.20:
                close_step += 1
                if len(examples) < 20:
                    examples.append({"ticker": stored.get("ticker"), "kind": "close_step",
                                     "stored": sc, "now": uc, "rel": rel})
            elif rel > 0.001:
                close_mismatch += 1
                if len(examples) < 20:
                    examples.append({"ticker": stored.get("ticker"), "kind": "close",
                                     "stored": sc, "now": uc, "rel": rel})
    return {
        "rows": len(pairs),
        "prev_volume": _stat_block(drifts),
        "prev_volume_by_source": {src: _stat_block(v) for src, v in by_source.items()},
        "close_mismatch": close_mismatch,
        "close_step": close_step,
        "examples": examples,
    }


# ── store reads ──────────────────────────────────────────────────────────────

def _read(sql: str, params: list | None = None) -> list[dict]:
    """A read of the price tables, in their file."""
    return rawstore.query(sql, params, db=_DB)


def _last_session(scope: list[str]) -> dict[str, date]:
    if not scope:
        return {}
    rows = _read(
        f"SELECT ticker, MAX(session_date) AS last FROM price_bars_daily "
        f"WHERE ticker IN ({_placeholders(len(scope))}) GROUP BY ticker", list(scope))
    return {r["ticker"]: r["last"] for r in rows if r.get("last") is not None}


def _checks(check: tuple[str, str], scope: list[str]) -> dict[str, float]:
    if not scope:
        return {}
    rows = _read(
        f"SELECT key, checked_at FROM price_checks WHERE dataset = ? AND source = ? "
        f"AND key IN ({_placeholders(len(scope))})",
        [check[0], check[1], *scope])
    return {r["key"]: r["checked_at"] for r in rows if r.get("checked_at") is not None}


def _stored_keys(scope: list[str]) -> set[tuple[str, date]]:
    if not scope:
        return set()
    rows = _read(
        f"SELECT ticker, session_date FROM price_bars_daily "
        f"WHERE ticker IN ({_placeholders(len(scope))})", list(scope))
    return {(r["ticker"], r["session_date"]) for r in rows}


def _stored_action_keys(scope: list[str]) -> set[tuple[str, date, str]]:
    if not scope:
        return set()
    rows = _read(
        f"SELECT ticker, ex_date, action FROM price_actions "
        f"WHERE ticker IN ({_placeholders(len(scope))})", list(scope))
    return {(r["ticker"], r["ex_date"], r["action"]) for r in rows}


def _stored_rows(scope: list[str], since: date) -> dict[tuple[str, date], dict]:
    """The stored rows of ``scope`` from ``since`` on: one bounded read per chunk, so a nightly
    never loads a filled history (thousands of rows per ticker) just to skip existing keys."""
    if not scope:
        return {}
    rows = _read(
        f"SELECT * FROM price_bars_daily WHERE ticker IN ({_placeholders(len(scope))}) "
        f"AND session_date >= ?", [*scope, since])
    return {(r["ticker"], r["session_date"]): r for r in rows}


def _spy_stored_dates() -> set[date]:
    rows = _read("SELECT session_date FROM price_bars_daily WHERE ticker = 'SPY'")
    return {r["session_date"] for r in rows if r.get("session_date") is not None}


_READ_COLUMNS = ("ticker", "session_date", "open", "high", "low", "close", "volume", "source", "fetched_at")
_AMOUNTS = ("dividend", "capital_gain")
_FIELDS = ("open", "high", "low", "close", "volume")
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()        # datetime64 day 0 as a date ordinal
# The session_date dtype a frame of stored dates gets (datetime64[s] on pandas 3), as the per-row read built it.
_DATE_DTYPE = pd.to_datetime(pd.Series([date(2000, 1, 3)], dtype=object)).dtype


def _read_columns(sql: str, params: list) -> dict[str, np.ndarray]:
    """A read of the price bars as numpy columns, NULLs masked (#383 K8a speed): the rows ``_read`` would return,
    without a Python dict per bar. Lenient as ``rawstore.query``: no file or no table reads as no rows, and a read
    never creates the file. ``_DB`` is ``prices`` (#156)."""
    if not rawstore.prices_db_path().exists():
        return {}
    try:
        with rawstore.connect(read_only=True, db=_DB) as conn:
            return conn.execute(sql, params).fetchnumpy()
    except duckdb.CatalogException:
        return {}


def _plain(col) -> tuple[np.ndarray, np.ndarray]:
    """``(values, is_null)`` of a fetched column."""
    if isinstance(col, np.ma.MaskedArray):
        return np.ma.getdata(col), np.ma.getmaskarray(col)
    col = np.asarray(col)
    return col, np.zeros(len(col), dtype=bool)


def _columns_from_read(parts: list[dict]) -> dict[str, np.ndarray]:
    """The bar columns of one or more ``_read_columns`` results: ``ticker``, ``day`` (date ordinal), each field and
    ``<field>_null``, ``source`` (None where NULL) and ``fetched`` (``float(fetched_at or 0.0)``)."""
    parts = [p for p in parts if p and len(p["ticker"])]
    if not parts:
        return {}

    def joined(name):
        vals, nulls = zip(*(_plain(p[name]) for p in parts))
        return np.concatenate(vals), np.concatenate(nulls)

    out: dict[str, np.ndarray] = {"ticker": joined("ticker")[0].astype(object)}
    days, _ = joined("session_date")
    out["day"] = days.astype("datetime64[D]").astype(np.int64) + _EPOCH_ORDINAL
    for f in _FIELDS:
        vals, null = joined(f)
        out[f] = np.where(null, np.nan, vals.astype(float))
        out[f + "_null"] = null
    source, null = joined("source")
    out["source"] = np.where(null, None, source.astype(object))
    fetched, null = joined("fetched_at")
    out["fetched"] = np.where(null | (fetched == 0.0), 0.0, fetched.astype(float))
    return out


def _columns_from_rows(rows: list[dict]) -> dict[str, np.ndarray]:
    """The same columns from bar dicts (the ``_bars_frame`` and ``apply_events`` inputs)."""
    out: dict[str, np.ndarray] = {"ticker": np.array([r["ticker"] for r in rows], dtype=object),
                                  "day": np.array([r["session_date"].toordinal() for r in rows], dtype=np.int64)}
    for f in _FIELDS:
        raw = [r.get(f) for r in rows]
        out[f + "_null"] = np.array([v is None for v in raw], dtype=bool)
        out[f] = np.array(raw, dtype=float)
    out["source"] = np.array([r.get("source") for r in rows], dtype=object)
    out["fetched"] = np.array([float(r.get("fetched_at") or 0.0) for r in rows], dtype=float)
    return out


def _read_series(tickers: list[str], start: date | None = None, end: date | None = None, prev_closes: bool = True
                 ) -> tuple[dict[str, np.ndarray], dict[str, list[dict]]]:
    """Bar columns (:func:`_columns_from_read`) and actions of ``tickers``: every action, and the bars the read needs.

    - With ``start`` (K8a, #383 R3): the bars from ``start`` on plus each ticker's last bar before it, the previous
      close a dividend in the window needs.
    - With ``end`` (K8a speed, #383): no bar after ``end`` but each ticker's first one and, when ``prev_closes``, the
      one bar each dividend or capital gain ex after ``end`` takes its ``C_prev`` from. An event after ``end`` still
      adjusts every bar before it (both factors use ``d_i < e``), which is why the actions are never bounded: a
      split needs only its own row and the bar's fetched_at, an amount its ``C_prev`` bar.
    """
    actions: dict[str, list[dict]] = {}
    if not tickers:
        return {}, actions
    marks = _placeholders(len(tickers))
    for r in _read(f"SELECT * FROM price_actions WHERE ticker IN ({marks})", list(tickers)):
        actions.setdefault(r["ticker"], []).append(r)
    cols = ", ".join(f"b.{c}" for c in _READ_COLUMNS)
    where, params = [f"b.ticker IN ({marks})"], list(tickers)
    if start is not None:
        where.append("b.session_date >= COALESCE((SELECT MAX(p.session_date) FROM price_bars_daily p "
                     "WHERE p.ticker = b.ticker AND p.session_date < ?), ?)")
        params += [start, start]
    if end is not None:
        where.append("b.session_date <= ?")
        params.append(end)
    parts = [_read_columns(f"SELECT {cols} FROM price_bars_daily b WHERE {' AND '.join(where)}", params)]
    if end is not None:
        # Past end: each ticker's first bar (it has bars to adjust exactly when the unbounded read had: the skipped
        # count and an empty frame stay the same) and, for the adjusted basis, each C_prev bar.
        keys = [f"SELECT ticker, MIN(session_date) AS session_date FROM price_bars_daily WHERE ticker IN ({marks}) "
                f"AND session_date > ? GROUP BY ticker"]
        key_params = [*tickers, end]
        if prev_closes:
            keys.append(f"SELECT p.ticker, p.session_date FROM (SELECT DISTINCT ticker, ex_date FROM price_actions "
                        f"WHERE ticker IN ({marks}) AND action IN ('dividend', 'capital_gain') AND ex_date > ?) a "
                        f"ASOF JOIN price_bars_daily p ON a.ticker = p.ticker AND a.ex_date > p.session_date")
            key_params += [*tickers, end if start is None else max(start, end)]   # an amount ex <= start: unused
        parts.append(_read_columns(
            f"SELECT {cols} FROM price_bars_daily b JOIN (SELECT DISTINCT ticker, session_date FROM "
            f"({' UNION ALL '.join(keys)})) c ON b.ticker = c.ticker AND b.session_date = c.session_date "
            f"WHERE b.session_date > ?", [*key_params, end]))
    return _columns_from_read(parts), actions


# ── read API ─────────────────────────────────────────────────────────────────

def daily_bars(tickers, basis: str = "nominal", start: date | None = None,
               end: date | None = None) -> pd.DataFrame:
    """Stored bars for ``tickers``, sorted by ticker then date; never fetches.

    ``basis`` is ``nominal`` or ``adjusted`` (the R2 factor series). The factors are applied first and the rows
    only then filtered to ``[start, end]``. With ``start`` the read is a window (K8a, #383 R3): the bars from
    ``start`` on plus each ticker's last bar before it, and every action except one ex on or before ``start``
    (a split or an amount there divides or adjusts no bar in the window). With ``end`` no bar after ``end`` is
    read but the ``C_prev`` bars of the amounts after it (K8a speed, #383). The result equals the full read sliced;
    ``attrs['skipped_events']`` counts the events that touch the window.
    """
    if basis not in ("nominal", "adjusted"):
        raise ValueError(f"unknown basis {basis!r}")
    return daily_bars_bases(tickers, start, end, bases=(basis,))[basis]


def daily_bars_bases(tickers, start: date | None = None, end: date | None = None,
                     bases: tuple[str, ...] = ("nominal", "adjusted")) -> dict[str, pd.DataFrame]:
    """``{basis: daily_bars(tickers, basis, start, end)}`` from one read of the bars and one of the actions.

    The screeners need both bases of every scored name (levels nominal, returns adjusted; #383 R3 S3): one read
    serves both instead of one per basis.
    """
    for basis in bases:
        if basis not in ("nominal", "adjusted"):
            raise ValueError(f"unknown basis {basis!r}")
    names = _norm(tickers)
    if isinstance(start, datetime):
        start = start.date()
    last = None if end is None else pd.Timestamp(end).date()     # the bars are dates: <= end is <= its day
    cols, actions = (_read_series(names, start, last, prev_closes="adjusted" in bases) if names else ({}, {}))
    group = (pd.Index(names).get_indexer(cols["ticker"]) if cols else np.zeros(0, dtype=np.int64))
    return _frames(names, cols, group, actions, bases, start, end)


def _empty_bars(skipped: int = 0) -> pd.DataFrame:
    empty = pd.DataFrame(columns=_BAR_COLUMNS)
    empty.attrs["skipped_events"] = skipped
    return empty


def _bars_frame(names: list[str], bars: dict[str, list[dict]], actions: dict[str, list[dict]], basis: str,
                start: date | None, end: date | None) -> pd.DataFrame:
    """One basis of per-ticker bar dicts as ``daily_bars`` returns it."""
    present = [t for t in names if t in bars]
    rows = [r for t in present for r in bars[t]]
    group = np.repeat(np.arange(len(present), dtype=np.int64), [len(bars[t]) for t in present])
    cols = _columns_from_rows(rows) if rows else {}
    return _frames(present, cols, group, actions, (basis,), start, end)[basis]


def _frames(names: list[str], cols: dict[str, np.ndarray], group: np.ndarray, actions: dict[str, list[dict]],
            bases: tuple[str, ...], start: date | None, end: date | None) -> dict[str, pd.DataFrame]:
    """Each basis of the bar columns as ``daily_bars`` returns it: one engine pass over every ticker (#383).

    ``group`` is each bar's index in ``names``. The rows are laid out as the per-ticker code laid them out (``names``
    order, each ticker's bars by date, ties in read order), so the frame, its filters and its sort are the same.
    """
    if not len(group):
        return {basis: _empty_bars() for basis in bases}
    by_day = np.argsort(cols["day"], kind="stable")
    order = by_day[np.argsort(group[by_day], kind="stable")]
    g, d = group[order], cols["day"][order]
    vals = {f: cols[f][order] for f in _FIELDS}
    null = {f: cols[f + "_null"][order] for f in _FIELDS}
    splits: list[tuple[int, dict]] = []
    amounts: list[tuple[int, dict]] = []
    with_bars = np.bincount(group, minlength=len(names)) > 0
    for gi, t in enumerate(names):
        if not with_bars[gi]:
            continue                                               # a ticker with no bar: nothing to adjust
        acts = actions.get(t, [])
        if start is not None:
            acts = [a for a in acts if a["ex_date"] > start]      # one ex on or before start adjusts no bar here
        splits += [(gi, a) for a in _usable(acts, ("split",))]
        amounts += [(gi, a) for a in _usable(acts, _AMOUNTS)]
    k, m, skipped = _event_factors(g, d, cols["fetched"][order], vals["close"], null["close"], len(names),
                                   _split_arrays(splits),
                                   _amount_arrays(amounts) if "adjusted" in bases else None)
    nominal = {f: vals[f] * k if f == "volume" else vals[f] / k for f in _FIELDS}
    dates = (d - _EPOCH_ORDINAL).astype("datetime64[D]").astype(_DATE_DTYPE)
    ticker, source = cols["ticker"][order], cols["source"][order]
    out: dict[str, pd.DataFrame] = {}
    for basis in bases:
        values = nominal if basis == "nominal" else {f: nominal[f] * m if f != "volume" else nominal[f]
                                                     for f in _FIELDS}
        df = pd.DataFrame({"ticker": ticker, "session_date": dates,
                           **{f: _as_column(values[f], null[f]) for f in _FIELDS}, "source": source})
        if start is not None:
            df = df[df["session_date"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["session_date"] <= pd.Timestamp(end)]
        df = df[_BAR_COLUMNS].sort_values(["ticker", "session_date"]).reset_index(drop=True)
        df.attrs["skipped_events"] = skipped if basis == "adjusted" else 0
        out[basis] = df
    return out


def _as_column(values: np.ndarray, null: np.ndarray):
    """A float column, or all None when every value is None (the dtype a frame of the per-row dicts had)."""
    return np.full(len(values), None, dtype=object) if null.all() else values


# ── Data Health ──────────────────────────────────────────────────────────────

def _last_n_sessions(latest: date, n: int) -> list[date]:
    out: list[date] = []
    d = latest
    while d >= CALENDAR_START and len(out) < n:
        if calendar.is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return out


@datahealth.scoped
def health_summary(now: datetime | None = None) -> dict:
    """Read-only summary for the Data Health row (§9.6), zero network."""
    now = _now(now)
    out: dict = {"newest_session": None, "latest_complete": None, "calendar_error": False,
                 "scope": 0, "behind": 0, "source_share": {}, "actions_last_5": 0,
                 "last_check": None}
    scope = bar_scope()
    out["scope"] = len(scope)
    latest = None
    try:
        latest = calendar.latest_complete_session(now)
        out["latest_complete"] = latest.isoformat()
    except ValueError:
        out["calendar_error"] = True
    rows = _read("SELECT MAX(session_date) AS m FROM price_bars_daily")
    newest = rows[0]["m"] if rows else None
    out["newest_session"] = newest.isoformat() if newest is not None else None
    last = _last_session(scope)
    out["behind"] = sum(1 for t in scope
                        if last.get(t) is None or (latest is not None and last[t] < latest))
    if newest is not None:
        share = _read(
            "SELECT source, COUNT(*) AS n FROM price_bars_daily WHERE session_date = ? GROUP BY source",
            [newest])
        out["source_share"] = {r["source"]: int(r["n"]) for r in share}
    try:
        if latest is not None:
            sessions = _last_n_sessions(latest, 5)
            acts = _read(
                f"SELECT COUNT(*) AS n FROM price_actions WHERE ex_date IN ({_placeholders(len(sessions))})",
                sessions) if sessions else []
        else:
            acts = _read("SELECT COUNT(*) AS n FROM price_actions WHERE ex_date >= ?",
                         [(now - timedelta(days=7)).date()])
        out["actions_last_5"] = int(acts[0]["n"]) if acts else 0
    except Exception:  # noqa: BLE001 - a missing table must not break the page
        out["actions_last_5"] = 0
    try:
        chk = _read(
            "SELECT MAX(checked_at) AS m FROM price_checks WHERE dataset = ? AND source = ?",
            list(CHECK_ACTIONS))
        out["last_check"] = chk[0]["m"] if chk and chk[0]["m"] is not None else None
    except Exception:  # noqa: BLE001
        out["last_check"] = None
    return out


def health(now: datetime | None = None) -> Health:
    """The numbers :func:`health_summary` reports, typed (#124 S4).

    ``covered`` is the scope minus the tickers behind the latest complete session and ``expected`` the scope;
    ``newest``/``expected_as_of`` are the newest stored session and the latest complete one; ``source_share``
    is the newest session's rows per source as a share (the dict keeps the counts). A calendar error is the one
    problem: without a latest complete session no ticker can be called behind.
    """
    n = health_summary(now)
    counts = n["source_share"]
    total = sum(counts.values())
    return Health(
        covered=n["scope"] - n["behind"],
        expected=n["scope"],
        newest=n["newest_session"],
        expected_as_of=n["latest_complete"],
        source_share={source: count / total for source, count in counts.items()} if total else None,
        problems=("calendar error: the latest complete session is unavailable",)
        if n["calendar_error"] else (),
    )


def summary_line(res: dict) -> str:
    """One human line for the JobMessage / CLI."""
    if res.get("locked"):
        return "Daily bars: already running."
    if res.get("errors"):
        return (f"Daily bars stopped ({', '.join(res['errors'])}) at session "
                f"{res.get('latest_session')}.")
    return (f"Daily bars {res.get('latest_session')}: {res.get('rows_inserted', 0)} rows "
            f"(UW {res.get('rows_by_source', {}).get('uw', 0)}, yfinance "
            f"{res.get('rows_by_source', {}).get('yfinance', 0)}), "
            f"{res.get('actions_inserted', 0)} actions, {res.get('failed_both', 0)} unreachable.")


def run_ok(res: dict) -> bool:
    """False iff the run recorded an error."""
    return not res.get("errors")


def _blank_result(run_id: str) -> dict:
    return {
        "latest_session": None, "scope": 0, "up_to_date": 0, "due": 0, "no_history": 0,
        "no_history_recent": 0, "filled_new": 0, "awaiting_fill": 0,
        "uw_calls": 0, "uw_ok": 0, "uw_failed": 0, "uw_capped": 0, "uw_down": 0,
        "yf_failed": 0, "failed_both": 0, "chunk_failed": 0,
        "rows_inserted": 0, "rows_by_source": {"uw": 0, "yfinance": 0},
        "dropped_off_calendar": 0, "dropped_off_reference": 0, "dropped_no_close": 0,
        "actions_inserted": 0, "actions_checked": 0, "adj_missing": 0,
        "overlap": {"rows": 0,
                    "prev_volume": {"n": 0, "median": None, "p90": None, "over_1pct": 0},
                    "prev_volume_by_source": {}, "close_mismatch": 0, "close_step": 0,
                    "examples": []},
        "errors": [], "locked": False, "no_fetch": False, "run_id": run_id,
    }


# ── UW client access ─────────────────────────────────────────────────────────

def _uw_health(uw) -> dict:
    fn = getattr(uw, "health", None)
    if not callable(fn):
        return {}
    try:
        return fn() or {}
    except Exception:  # noqa: BLE001 - reporting a client must never fail the run
        return {}


def _uw_calls(uw) -> int:
    return int(_uw_health(uw).get("calls") or 0)


def _uw_down(uw) -> bool:
    if getattr(uw, "auth_error", None):
        return True
    return bool(_uw_health(uw).get("breaker_open"))


# ── nightly ──────────────────────────────────────────────────────────────────

def _classify(scope: list[str], latest: date, last: dict[str, date],
              fill_marks: dict[str, float], action_marks: dict[str, float],
              now_ts: float) -> dict:
    due: list[str] = []
    no_hist: list[str] = []
    recent = up = 0
    actions_due: list[str] = []
    for t in scope:
        ls = last.get(t)
        if ls is not None and ls >= latest:
            up += 1
        elif ls is None:
            mark = fill_marks.get(t)
            if mark is None or (now_ts - float(mark)) > FILL_RETRY_DAYS * 86400:
                no_hist.append(t)
            else:
                recent += 1
        else:
            due.append(t)
        if ls is not None:
            chk = action_marks.get(t)
            if chk is None or not calendar.bars_complete(latest, float(chk)):
                actions_due.append(t)
    return {"due": due, "no_hist": no_hist, "recent": recent, "up": up,
            "actions_due": actions_due}


def ensure_daily_bars(tickers=None, *, uw, yf_download=None, now=None, run_id=None,
                      no_fetch: bool = False, max_uw_calls: int = UW_NIGHTLY_CAP,
                      max_new_fill: int = MAX_NEW_FILL) -> dict:
    """Write the latest complete session's bars for ``tickers`` (or the whole scope).

    Returns a result dict; see §9.3. Never rewrites a stored key.
    """
    now_given = now is not None
    now = _now(now)
    run_at = now.timestamp()
    rid = run_id or ledger.new_run_id()
    dl = yf_download or _yf_download
    res = _blank_result(rid)
    try:
        latest = calendar.latest_complete_session(now)
    except ValueError:
        _add_error(res, "calendar")
        return res
    res["latest_session"] = latest.isoformat()
    scope = _norm(tickers) if tickers is not None else bar_scope()
    res["scope"] = len(scope)
    stamp = _stamper(now_given, run_at)

    last = _last_session(scope)
    fill_marks = _checks(CHECK_FILL, scope)
    action_marks = _checks(CHECK_ACTIONS, scope)
    cls = _classify(scope, latest, last, fill_marks, action_marks, run_at)
    res["due"] = len(cls["due"])
    res["no_history"] = len(cls["no_hist"])
    res["no_history_recent"] = cls["recent"]
    res["up_to_date"] = cls["up"]

    if no_fetch:
        res["no_fetch"] = True
        return res

    with job_lock.acquire("daily_bars", wait=0) as got:
        if not got:
            res["locked"] = True
            return res
        spy_mark = _checks(CHECK_FILL, ["SPY"]).get("SPY")
        spy_last = _last_session(["SPY"]).get("SPY")
        if spy_mark is None or spy_last is None:
            _add_error(res, "fill_not_done")
            return res

        calls_at_start = _uw_calls(uw)
        reference = _spy_stored_dates()
        due_set = set(cls["due"])
        actions_due = set(cls["actions_due"])
        chunk_set = [t for t in scope if t in due_set or t in actions_due]
        pairs: list[tuple[dict, dict]] = []      # the whole run's overlap, so median/p90 cover every chunk
        for chunk in _chunks(chunk_set, CHUNK):
            # Before the chunk's UW/yfinance calls and its one atomic write. A stop leaves the previous chunk
            # committed and this one unstarted (#26 PL4).
            job_control.checkpoint(f"daily bars {chunk[0]}")
            pairs += _process_chunk(chunk, due_set=due_set, uw=uw, dl=dl, latest=latest,
                                    last=last, action_marks=action_marks, reference=reference,
                                    stamp=stamp, rid=rid, run_at=run_at, res=res,
                                    calls_at_start=calls_at_start, max_uw_calls=max_uw_calls)
        if pairs:
            res["overlap"] = overlap_stats(pairs)

        if res["due"] and res["failed_both"] > 0.01 * res["due"]:
            _add_error(res, "failed_both")

        new_fill = cls["no_hist"][:max_new_fill]
        res["awaiting_fill"] = len(cls["no_hist"]) - len(new_fill)
        if new_fill:
            job_control.checkpoint(f"daily bars fill {new_fill[0]}")
            _fill_batch(new_fill, dl=dl, latest=latest, reference=reference, stamp=stamp,
                        rid=rid, run_at=run_at, res=res, mark_fill=True)
        return res


def _process_chunk(chunk: list[str], *, due_set: set[str], uw, dl, latest: date,
                   last: dict, action_marks: dict, reference: set[date], stamp,
                   rid: str, run_at: float, res: dict, calls_at_start: int,
                   max_uw_calls: int) -> list[tuple[dict, dict]]:
    """One chunk: UW, yfinance, then one atomic write. Returns the chunk's overlap pairs
    (empty when the write failed)."""
    uw_rows: dict[str, list[dict]] = {}
    uw_ok: set[str] = set()
    for t in chunk:
        if t not in due_set:
            continue
        n = sessions_behind(last.get(t), latest)
        days = uw_days(n)
        if days is None:
            continue
        if _uw_calls(uw) - calls_at_start >= max_uw_calls:
            res["uw_capped"] += 1
            continue
        if _uw_down(uw):
            res["uw_down"] += 1
            _add_error(res, "uw_auth" if getattr(uw, "auth_error", None) else "uw_breaker")
            continue
        result = uw.ohlc_daily_result(t, days=days)
        res["uw_calls"] += 1
        if getattr(result, "ok", False):
            res["uw_ok"] += 1
            uw_ok.add(t)
            uw_rows[t] = rows_from_uw(t, result.data, fetched_at=stamp(), run_id=rid, run_at=run_at,
                                      counts=res)
        else:
            res["uw_failed"] += 1

    yf_bars: dict[str, list[dict]] = {}
    yf_actions: dict[str, list[dict]] = {}
    yf_ok: set[str] = set()
    yf_at: dict[str, float] = {}            # when each ticker's download came back (its watermark)
    starts: dict[str, date] = {}
    for t in chunk:
        base = _epoch_date(action_marks.get(t))
        lb = last.get(t)
        cands = [x for x in (base, lb) if x is not None]
        base = min(cands) if cands else latest
        starts[t] = base - timedelta(days=ACTIONS_LOOKBACK_DAYS)
    near = [t for t in chunk if starts[t] >= latest - timedelta(days=FAR_DAYS)]
    far = [t for t in chunk if t not in set(near)]
    for group in (near, far):
        if not group:
            continue
        start = min(starts[t] for t in group)
        end = latest + timedelta(days=1)
        try:
            frame = dl(list(group), **YF_KW, start=start, end=end)
        except Exception:  # noqa: BLE001 - a failed download falls back to UW / nothing
            res["yf_failed"] += len(group)
            continue
        arrived = stamp()
        bars, actions = rows_from_yf(frame, group, fetched_at=arrived, run_id=rid, run_at=run_at,
                                     counts=res)
        for t in group:
            yf_at[t] = arrived
        for t, rows in bars.items():
            yf_bars.setdefault(t, []).extend(rows)
            if any(r.get("close") is not None for r in rows):
                yf_ok.add(t)
        for t, rows in actions.items():
            yf_actions.setdefault(t, []).extend(rows)

    offered = [r["session_date"] for t in chunk for r in (*uw_rows.get(t, []), *yf_bars.get(t, []))]
    stored = _stored_rows(chunk, min(offered)) if offered else {}
    existing = set(stored)
    existing_actions = _stored_action_keys(chunk)
    rows_bars: list[dict] = []
    actions_rows: list[dict] = []
    by_source = {"uw": 0, "yfinance": 0}
    got_bar: set[str] = set()
    pairs: list[tuple[dict, dict]] = []
    for t in chunk:
        uw_by_date = {r["session_date"]: r for r in uw_rows.get(t, [])}
        yf_by_date = {r["session_date"]: r for r in yf_bars.get(t, [])}
        adj_by_date = {r["session_date"]: r.get("adj_close") for r in yf_bars.get(t, [])}
        for d in sorted(set(uw_by_date) | set(yf_by_date)):
            if d > latest:
                continue
            if (t, d) in existing:
                if d in uw_by_date and (t, d) in stored:
                    pairs.append((uw_by_date[d], stored[(t, d)]))
                continue
            reason = keep_session(d, latest, reference)
            if reason == "off_calendar":
                res["dropped_off_calendar"] += 1
                continue
            if reason == "off_reference":
                res["dropped_off_reference"] += 1
                continue
            row = uw_by_date.get(d) or yf_by_date.get(d)
            if row is None:
                continue
            row = dict(row)
            adj = adj_by_date.get(d)
            row["adj_close"] = adj
            row["adj_source"] = "yfinance" if adj is not None else None
            if adj is None:
                res["adj_missing"] += 1
            rows_bars.append(row)
            by_source[row["source"]] = by_source.get(row["source"], 0) + 1
            got_bar.add(t)
    for t in chunk:
        for a in yf_actions.get(t, []):
            if (t, a["ex_date"], a["action"]) in existing_actions:
                continue
            actions_rows.append(a)

    for t in chunk:
        if t in due_set and t not in got_bar and t not in uw_ok and t not in yf_ok:
            res["failed_both"] += 1

    checks = [{"dataset": CHECK_ACTIONS[0], "source": CHECK_ACTIONS[1], "key": t,
               "checked_at": yf_at[t], "run_id": rid} for t in sorted(yf_ok)]
    try:
        rawstore.atomic(lambda tx: _write_chunk(tx, rows_bars, actions_rows, checks), db=_DB)
    except Exception as exc:  # noqa: BLE001 - one bad chunk must not stop the run
        logger.warning("daily_bars chunk failed: %s", exc)
        res["chunk_failed"] += 1
        return []
    res["rows_inserted"] += len(rows_bars)
    res["rows_by_source"]["uw"] += by_source["uw"]
    res["rows_by_source"]["yfinance"] += by_source["yfinance"]
    res["actions_inserted"] += len(actions_rows)
    res["actions_checked"] += len(checks)
    return pairs


def _write_chunk(tx, bars: list[dict], actions: list[dict], checks: list[dict]) -> None:
    if bars:
        tx.write_rows("price_bars_daily", bars)
    if actions:
        tx.write_rows("price_actions", actions)
    if checks:
        tx.write_rows("price_checks", checks)


# ── attended fill ────────────────────────────────────────────────────────────

def _fill_batch(tickers: list[str], *, dl, latest: date, reference: set[date], stamp,
                rid: str, run_at: float, res: dict, mark_fill: bool, count_fill: bool = True) -> None:
    """Download ``period="max"`` for ``tickers`` and write one atomic chunk each."""
    try:
        frame = dl(list(tickers), period="max", **YF_KW)
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_bars fill failed: %s", exc)
        res["yf_failed"] += len(tickers)
        return
    fetched_at = stamp()
    bars, actions = rows_from_yf(frame, tickers, fetched_at=fetched_at, run_id=rid, run_at=run_at,
                                 counts=res)
    existing = _stored_keys(tickers)
    existing_actions = _stored_action_keys(tickers)
    rows_bars: list[dict] = []
    rows_actions: list[dict] = []
    by_source = {"uw": 0, "yfinance": 0}
    ok_tickers: list[str] = []
    for t in tickers:
        t_bars = bars.get(t, [])
        if any(r.get("close") is not None for r in t_bars):
            ok_tickers.append(t)
        for r in t_bars:
            if (t, r["session_date"]) in existing:
                continue
            reason = keep_session(r["session_date"], latest, reference)
            if reason == "off_calendar":
                res["dropped_off_calendar"] += 1
                continue
            if reason == "off_reference":
                res["dropped_off_reference"] += 1
                continue
            rows_bars.append(r)
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        for a in actions.get(t, []):
            if (t, a["ex_date"], a["action"]) in existing_actions:
                continue
            rows_actions.append(a)
    checks: list[dict] = []
    if mark_fill:
        # The marker records "tried": every ticker in a download that did not raise, so
        # §9.3 step 4 can decide the retry window (a fill that found nothing still counts).
        for t in sorted(tickers):
            checks.append({"dataset": CHECK_FILL[0], "source": CHECK_FILL[1], "key": t,
                           "checked_at": fetched_at, "run_id": rid})
            checks.append({"dataset": CHECK_ACTIONS[0], "source": CHECK_ACTIONS[1], "key": t,
                           "checked_at": fetched_at, "run_id": rid})
    try:
        rawstore.atomic(lambda tx: _write_chunk(tx, rows_bars, rows_actions, checks), db=_DB)
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_bars fill chunk failed: %s", exc)
        res["chunk_failed"] += 1
        return
    res["rows_inserted"] += len(rows_bars)
    res["rows_by_source"]["uw"] += by_source["uw"]
    res["rows_by_source"]["yfinance"] += by_source["yfinance"]
    res["actions_inserted"] += len(rows_actions)
    res["actions_checked"] += len(checks)
    if count_fill:
        res["filled_new"] = res.get("filled_new", 0) + len(ok_tickers)


def fill_history(tickers=None, *, yf_download=None, now=None, run_id=None,
                 chunk: int = CHUNK, detail: bool = False) -> dict:
    """The attended ``period="max"`` fill: SPY first, then the rest (#156 slice B)."""
    now_given = now is not None
    now = _now(now)
    run_at = now.timestamp()
    rid = run_id or ledger.new_run_id()
    dl = yf_download or _yf_download
    res = _blank_result(rid)
    res["reference_first"] = None
    res["spy_first"] = None
    res["per_ticker"] = {}
    try:
        latest = calendar.latest_complete_session(now)
    except ValueError:
        _add_error(res, "calendar")
        return res
    res["latest_session"] = latest.isoformat()
    scope = _norm(tickers) if tickers is not None else bar_scope()
    res["scope"] = len(scope)
    stamp = _stamper(now_given, run_at)
    with job_lock.acquire("daily_bars", wait=0) as got:
        if not got:
            res["locked"] = True
            return res
        try:
            ref_frame = dl(["SPY", REFERENCE_INDEX], period="max", **YF_KW)
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily_bars reference download failed: %s", exc)
            res["yf_failed"] += 2
            _add_error(res, "reference")
            return res
        ref_bars, _ = rows_from_yf(ref_frame, ["SPY", REFERENCE_INDEX],
                                   fetched_at=stamp(), run_id=rid, run_at=run_at)
        spy_dates = sorted(r["session_date"] for r in ref_bars.get("SPY", []))
        gspc_dates = sorted(r["session_date"] for r in ref_bars.get(REFERENCE_INDEX, []))
        spy_first = spy_dates[0] if spy_dates else None
        res["spy_first"] = spy_first.isoformat() if spy_first else None
        # yfinance does not raise when one symbol fails: it returns an empty frame for it. An empty
        # SPY (or no ^GSPC date before SPY's first) would drop every pre-2025 (pre-1993) row as
        # off_reference while the fill markers stop any refill, so the run stops before any write.
        if spy_first is None or not any(d < spy_first for d in gspc_dates):
            res["yf_failed"] += 1
            _add_error(res, "reference")
            return res
        reference = set(spy_dates) | {d for d in gspc_dates if d < spy_first}
        res["reference_first"] = min(reference).isoformat()

        _fill_batch(["SPY"], dl=dl, latest=latest, reference=reference, stamp=stamp,
                    rid=rid, run_at=run_at, res=res, mark_fill=True, count_fill=False)
        rest = [t for t in scope if t != "SPY"]
        for group in _chunks(rest, chunk):
            todo = [t for t in group if _needs_fill(t)]
            if todo:
                _fill_batch(todo, dl=dl, latest=latest, reference=reference, stamp=stamp,
                            rid=rid, run_at=run_at, res=res, mark_fill=True)
        if detail:
            res["per_ticker"] = _per_ticker(scope)
    return res


def _needs_fill(ticker: str) -> bool:
    """True when ``ticker`` has no fill marker, or a marker but no stored bar."""
    marks = _checks(CHECK_FILL, [ticker])
    if marks.get(ticker) is None:
        return True
    return not bool(_last_session([ticker]).get(ticker))


def _per_ticker(scope: list[str]) -> dict:
    out: dict[str, dict] = {}
    if not scope:
        return out
    rows = _read(
        f"SELECT ticker, COUNT(*) AS n, MIN(session_date) AS first FROM price_bars_daily "
        f"WHERE ticker IN ({_placeholders(len(scope))}) GROUP BY ticker", list(scope))
    for r in rows:
        first = r.get("first")
        out[r["ticker"]] = {"rows": int(r["n"]), "first": first.isoformat() if first else None}
    return out


__all__ = [
    "UW_NIGHTLY_CAP", "UW_MAX_DAYS", "CHUNK", "OVERLAP_SESSIONS", "ACTIONS_LOOKBACK_DAYS",
    "FAR_DAYS", "MAX_NEW_FILL", "FILL_RETRY_DAYS", "BENCHMARKS", "BAR_TIERS", "REFERENCE_INDEX",
    "CALENDAR_START", "CHECK_ACTIONS", "CHECK_FILL", "YF_KW",
    "bar_scope", "ensure_daily_bars", "fill_history", "daily_bars", "health_summary", "health",
    "summary_line", "run_ok", "sessions_behind", "uw_days", "rows_from_uw", "rows_from_yf",
    "keep_session", "apply_events", "overlap_stats",
]
