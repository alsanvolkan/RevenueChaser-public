"""Daily closes for the Co-movement board and the Relatedness peer market (PR 8, G3: the prices store).

Both read the prices store (``prices.duckdb``, PR 7): ``rcdata.read.ticker_bars(basis="total_return")``, the split-
and dividend-adjusted closes of ``price_bar_daily`` with ``adj_factor`` applied on read, over the last
``config.OHLC_PERIOD`` of sessions (``history_start``: two years back from the store's newest session).

Nothing here downloads. The prices build (``rcdata/build/prices``, the builder's queue) is the store's one writer, so
a Co-movement Fetch calls no provider for bars (``fetch_bulk`` only reports the store's coverage) and a Refresh stays
zero-network. Until the history run (Run H, after PR 21) the store holds the bars from the PR 7 deploy day on, so the
windows find few complete names at first: that is the data, not a fault here.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import pandas as pd

from RevenueChaser.core.universe import DEFAULT_MC_MIN, classification_universe
from RevenueChaser.rcdata import read

from . import config

# One benchmark at a time is enough (SPY/DIA/QQQ are collinear), but the prices store holds all three
# (``rcdata/build/prices.BENCHMARKS``), so switching the Remove-market control stays offline.
BENCHMARK_TICKERS = ("SPY", "DIA", "QQQ")


def _names(tickers) -> list[str]:
    """Upper-cased, stripped, de-duplicated: the names the store is asked for."""
    if isinstance(tickers, str):
        tickers = [tickers]
    return list(dict.fromkeys(str(t).strip().upper() for t in (tickers or []) if t and str(t).strip()))


def _as_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _period_years(period: str | None = None) -> int:
    """The years of ``config.OHLC_PERIOD`` (``"2y"``)."""
    period = config.OHLC_PERIOD if period is None else period
    m = re.fullmatch(r"\s*(\d+)\s*y\s*", str(period))
    if not m:
        raise ValueError(f"OHLC_PERIOD must be '<n>y', got {period!r}")
    return int(m.group(1))


def history_start() -> date | None:
    """The first session Co-movement reads: the store's newest session minus ``config.OHLC_PERIOD`` (2y). One anchor
    for every read, so a name's whole history, the window and the flat check all see the same span. ``None`` when the
    store holds no bar."""
    d = read.newest_session()
    if d is None:
        return None
    return (pd.Timestamp(_as_date(d)) - pd.DateOffset(years=_period_years())).date()


def universe_tickers(mc_min: float = DEFAULT_MC_MIN, *, operating_only: bool = False) -> list[str]:
    """Liquid slice of the shared universe (highest market cap first).

    Delegates to the one shared selector so every classification board reads the
    same ticker set (see core/universe.py). ``operating_only`` is the board's type
    filter (PR 2; off by default: Co-movement correlates every A+B type).
    """
    return classification_universe(mc_min, operating_only=operating_only)


def fetch_bulk(tickers: list[str] | None = None, no_fetch: bool = False) -> dict:
    """The store's coverage of ``tickers`` plus the benchmarks (``BENCHMARK_TICKERS``); never downloads.

    The prices build is the store's one writer, so a Fetch calls no provider for the bars it holds, nor for the ones
    it lacks: ``cached`` counts the names with a bar in the span Co-movement reads (``history_start``), ``pending``
    the others (they show as no prices until the prices build stores them). ``fetched`` and ``failed`` stay 0: the
    keys are the ones the board had before the D01 switch.
    """
    if tickers is None:
        tickers = universe_tickers()
    fetch_list = _names([*tickers, *BENCHMARK_TICKERS])
    lo = history_start()
    held: set[str] = set()
    if lo is not None and fetch_list:
        held = {t for t, d in read.last_sessions(fetch_list).items() if _as_date(d) >= lo}
    cached = sum(1 for t in fetch_list if t in held)
    return {"requested": len(fetch_list), "cached": cached, "pending": len(fetch_list) - cached,
            "fetched": 0, "failed": 0, "no_fetch": bool(no_fetch)}


def last_written(tickers) -> float:
    """When the store last changed under ``tickers``: the newest ``fetched_at`` of their bars and actions (0.0 when
    none, ``rcdata.read.changed_at``).

    A new dividend or split revises the adjusted history without moving the last date, the history run adds older
    bars and a re-read session replaces one, so a stored run older than this is not served (``engine._stored_run``)."""
    return read.changed_at(_names(tickers))


def sessions_held(ticker: str = "SPY") -> int:
    """Sessions of ``ticker`` with a close in the span Co-movement reads (from ``history_start``); 0 when none.

    The Settings window rule (``core/board_settings.stored_sessions``) counts SPY here."""
    lo = history_start()
    if lo is None:
        return 0
    return len(read.bar_sessions([ticker], start=lo))


def price_frame(tickers: list[str], start: str | None = None) -> pd.DataFrame:
    """Adjusted close matrix (sessions × tickers) from the prices store (``rcdata.read.ticker_bars``, total return).

    The rows are the sessions from ``history_start`` on; ``start`` (``YYYY-MM-DD``) keeps only that session and
    later, read by SQL (K8a): a store-only run reads its window instead of every stored bar (#379 COM-3; the
    Relatedness peer market reuses it, #388). Only names with a bar appear; a NULL or NaN close is no bar.
    """
    names = _names(tickers)
    if not names:
        return pd.DataFrame()
    lo = history_start()
    if lo is None:
        return pd.DataFrame()
    begin = lo if start is None else max(lo, _as_date(start))
    bars = read.ticker_bars(names, start=begin, basis="total_return")
    if bars.empty:
        return pd.DataFrame()
    bars = bars[pd.to_numeric(bars["close"], errors="coerce").notna()]
    if bars.empty:
        return pd.DataFrame()
    frame = bars.pivot(index="session_date", columns="ticker", values="close").astype(float).sort_index()
    frame.index = pd.DatetimeIndex(frame.index, name="ts")
    frame.columns.name = "ticker"
    return frame


def window_start(tickers: list[str], sessions: int) -> str | None:
    """The ``start`` for ``price_frame(tickers, start=...)`` that keeps exactly the last ``sessions`` rows of the full
    frame (``YYYY-MM-DD``: the ``sessions``-th latest session on which any of them has a bar), or None when the
    frame holds fewer sessions than that, so the caller reads everything (the Relatedness peer market, #388)."""
    names = _names(tickers)
    lo = history_start() if names and sessions >= 1 else None
    if lo is None:
        return None
    days = read.bar_sessions(names, start=lo)
    return days[-int(sessions)].isoformat() if len(days) >= int(sessions) else None


def history_facts(tickers: list[str], flat_run: int = config.FLAT_RUN) -> dict:
    """What a windowed read cannot see, over the whole span ``price_frame`` reads (#379 COM-3), from one SQL read
    (``rcdata.read.bar_facts``): each ticker's ``first`` and ``last`` session with a bar, the ``sessions`` on which
    any of them has one (``price_frame``'s full index), and the ``flat`` candidates, the only tickers
    ``returns.flat_print_names`` can flag on the adjusted closes; the caller reads those whole and judges them with
    that same function.
    """
    names = _names(tickers)
    lo = history_start() if names else None
    if lo is None:
        return {"first": {}, "last": {}, "sessions": [], "flat": []}
    return read.bar_facts(names, start=lo, flat_run=flat_run)
