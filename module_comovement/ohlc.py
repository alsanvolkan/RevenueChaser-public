"""Daily closes for the Co-movement board and the Relatedness peer market (#379 D01 switch).

Both read D01: ``core.prices.daily_bars(basis="adjusted")``, the split/dividend-adjusted closes of
``price_bars_daily`` plus ``price_actions`` (prices.duckdb), through its windowed read (K8a), over the last
``config.OHLC_PERIOD`` of sessions (``history_start``: the two years the old ``swing_ohlc`` cache held).

Nothing here downloads. Daily Bars (``core/prices.ensure_daily_bars``, RC-Daily-Bars) is D01's one writer, so a
Co-movement Fetch calls no provider for bars and writes no ``swing_ohlc`` row (``fetch_bulk`` only reports D01's
coverage), and a Refresh stays zero-network.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import pandas as pd

from RevenueChaser.core import prices, rawstore
from RevenueChaser.core.universe import DEFAULT_MC_MIN, classification_universe

from . import config

# One benchmark at a time is enough (SPY/DIA/QQQ are collinear), but D01 holds all three (core/prices.BENCHMARKS),
# so switching the Remove-market control stays offline.
BENCHMARK_TICKERS = ("SPY", "DIA", "QQQ")
# The file of the D01 tables (prices.duckdb since #156), named the way core/prices.py names it.
_DB = rawstore.MANIFEST["price_bars_daily"].get("db", "raw")


def _names(tickers) -> list[str]:
    """Upper-cased, stripped, de-duplicated: the names ``daily_bars`` reads."""
    if isinstance(tickers, str):
        tickers = [tickers]
    return list(dict.fromkeys(str(t).strip().upper() for t in (tickers or []) if t and str(t).strip()))


def _marks(n: int) -> str:
    return ", ".join("?" for _ in range(n))


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
    """The first session Co-movement reads: D01's newest session minus ``config.OHLC_PERIOD`` (2y), the history the
    old ``swing_ohlc`` cache held. One anchor for every read, so a name's whole history, the window and the flat
    check all see the same span. ``None`` when D01 holds no bar."""
    rows = rawstore.query("SELECT max(session_date) AS d FROM price_bars_daily", db=_DB)
    d = rows[0].get("d") if rows else None
    if d is None:
        return None
    return (pd.Timestamp(_as_date(d)) - pd.DateOffset(years=_period_years())).date()


def universe_tickers(mc_min: float = DEFAULT_MC_MIN) -> list[str]:
    """Liquid slice of the shared universe (highest market cap first).

    Delegates to the one shared selector so every classification board reads the
    same ticker set (see core/universe.py).
    """
    return classification_universe(mc_min)


def fetch_bulk(tickers: list[str] | None = None, no_fetch: bool = False) -> dict:
    """D01's coverage of ``tickers`` plus the benchmarks (``BENCHMARK_TICKERS``); never downloads (#379 D01 switch).

    Daily Bars is the one D01 writer, so a Fetch calls no provider for the bars D01 holds, nor for the ones it lacks:
    ``cached`` counts the names with a D01 bar in the span Co-movement reads (``history_start``), ``pending`` the
    others (they show as no prices until Daily Bars stores them). ``fetched`` and ``failed`` stay 0: the keys are the
    ones the board had before the switch.
    """
    if tickers is None:
        tickers = universe_tickers()
    fetch_list = _names([*tickers, *BENCHMARK_TICKERS])
    held: set[str] = set()
    lo = history_start()
    if lo is not None and fetch_list:
        rows = rawstore.query(
            f"SELECT DISTINCT ticker FROM price_bars_daily WHERE ticker IN ({_marks(len(fetch_list))}) "
            "AND session_date >= ? AND close IS NOT NULL AND NOT isnan(close)", [*fetch_list, lo], db=_DB)
        held = {r["ticker"] for r in rows}
    cached = sum(1 for t in fetch_list if t in held)
    return {"requested": len(fetch_list), "cached": cached, "pending": len(fetch_list) - cached,
            "fetched": 0, "failed": 0, "no_fetch": bool(no_fetch)}


def last_written(tickers) -> float:
    """When D01 last changed under ``tickers``: the newest ``fetched_at`` of their bars and actions (0.0 when none).

    A new dividend or split row revises the adjusted history without moving the last date, and a fill adds older
    bars, so a stored run older than this is not served (``engine._stored_run``)."""
    names = _names(tickers)
    if not names:
        return 0.0
    marks = _marks(len(names))
    rows = rawstore.query(
        f"SELECT greatest(COALESCE((SELECT max(fetched_at) FROM price_bars_daily WHERE ticker IN ({marks})), 0), "
        f"COALESCE((SELECT max(fetched_at) FROM price_actions WHERE ticker IN ({marks})), 0)) AS t",
        [*names, *names], db=_DB)
    return float((rows[0].get("t") if rows else None) or 0.0)


def sessions_held(ticker: str = "SPY") -> int:
    """Sessions of ``ticker`` with a close in the span Co-movement reads (D01, from ``history_start``); 0 when none.

    The Settings window rule (``core/board_settings.stored_sessions``) counts SPY here since the D01 switch."""
    lo = history_start()
    if lo is None:
        return 0
    rows = rawstore.query(
        "SELECT count(*) AS n FROM price_bars_daily WHERE ticker = ? AND session_date >= ? "
        "AND close IS NOT NULL AND NOT isnan(close)", [str(ticker).strip().upper(), lo], db=_DB)
    return int(rows[0]["n"]) if rows else 0


def price_frame(tickers: list[str], start: str | None = None) -> pd.DataFrame:
    """Adjusted close matrix (sessions × tickers) from D01 (``core.prices.daily_bars(basis="adjusted")``).

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
    bars = prices.daily_bars(names, basis="adjusted", start=begin)
    if bars.empty:
        return pd.DataFrame()
    bars = bars[pd.to_numeric(bars["close"], errors="coerce").notna()]
    if bars.empty:
        return pd.DataFrame()
    frame = bars.pivot(index="session_date", columns="ticker", values="close").astype(float).sort_index()
    frame.index.name = "ts"
    frame.columns.name = "ticker"
    return frame


def _bars_sql(tickers: list[str], lo: date) -> tuple[str, list]:
    """``WITH bars``: the D01 bars ``price_frame`` keeps (from ``lo`` on, a close that is not NULL or NaN)."""
    return ("WITH bars AS (SELECT ticker, session_date AS d, close FROM price_bars_daily "
            f"WHERE ticker IN ({_marks(len(tickers))}) AND session_date >= ? "
            "AND close IS NOT NULL AND NOT isnan(close))"), [*tickers, lo]


def window_start(tickers: list[str], sessions: int) -> str | None:
    """The ``start`` for ``price_frame(tickers, start=...)`` that keeps exactly the last ``sessions`` rows of the full
    frame (``YYYY-MM-DD``: the ``sessions``-th latest session on which any of them has a bar), or None when the
    frame holds fewer sessions than that, so the caller reads everything (the Relatedness peer market, #388)."""
    names = _names(tickers)
    lo = history_start() if names and sessions >= 1 else None
    if lo is None:
        return None
    bars, params = _bars_sql(names, lo)
    rows = rawstore.query(bars + " SELECT CAST(d AS VARCHAR) AS d FROM (SELECT DISTINCT d FROM bars) "
                                 f"ORDER BY d DESC LIMIT 1 OFFSET {int(sessions) - 1}", params, db=_DB)
    return str(rows[0]["d"]) if rows else None


def history_facts(tickers: list[str], flat_run: int = config.FLAT_RUN) -> dict:
    """What a windowed read cannot see, from SQL over the whole span ``price_frame`` reads (#379 COM-3, D01 switch).

    - ``first`` / ``last``: each ticker's first and last session with a bar (``YYYY-MM-DD``);
    - ``sessions``: the sorted sessions on which any of them has one (``price_frame``'s full index);
    - ``flat``: the only tickers ``returns.flat_print_names`` can flag on the adjusted closes. SQL sees the stored
      closes, so a step counts as unchanged when the stored close is equal OR an action (split, dividend, capital
      gain) has its ex-date inside the step (the adjusted closes can be equal there while the stored ones are not);
      a name with ``flat_run`` such steps in a row is a candidate. A name with a split in the span is always one: a
      split divides a bar by whether that bar was fetched before it, which stored closes cannot show. The caller
      reads the candidates whole and judges them with that same function.
    """
    names = _names(tickers)
    lo = history_start() if names else None
    if lo is None:
        return {"first": {}, "last": {}, "sessions": [], "flat": []}
    bars, params = _bars_sql(names, lo)
    marks = _marks(len(names))
    run = max(2, int(flat_run))
    rows = rawstore.query(
        bars + f", ex AS (SELECT DISTINCT ticker, ex_date FROM price_actions WHERE ticker IN ({marks}) "
        "AND ex_date > ?), "
        "steps AS (SELECT ticker, d, close, lag(close) OVER w AS pc, lag(d) OVER w AS pd FROM bars "
        "WINDOW w AS (PARTITION BY ticker ORDER BY d)), "
        "moves AS (SELECT s.ticker, s.d, CASE WHEN s.pc IS NULL THEN NULL WHEN s.close = s.pc THEN 0 "
        "WHEN EXISTS (SELECT 1 FROM ex WHERE ex.ticker = s.ticker AND ex.ex_date > s.pd AND ex.ex_date <= s.d) "
        "THEN 0 ELSE 1 END AS moved FROM steps s), "
        "runs AS (SELECT ticker, d, count(moved) OVER r = ? AND sum(moved) OVER r = 0 AS flat FROM moves "
        f"WINDOW r AS (PARTITION BY ticker ORDER BY d ROWS BETWEEN {run - 1} PRECEDING AND CURRENT ROW)) "
        "SELECT ticker, CAST(min(d) AS VARCHAR) AS first, CAST(max(d) AS VARCHAR) AS last, "
        "bool_or(COALESCE(flat, false)) AS flat FROM runs GROUP BY ticker",
        [*params, *names, lo, run], db=_DB)
    split = {r["ticker"] for r in rawstore.query(
        f"SELECT DISTINCT ticker FROM price_actions WHERE ticker IN ({marks}) AND action = 'split' AND ex_date > ?",
        [*names, lo], db=_DB)}
    sessions = rawstore.query(bars + " SELECT DISTINCT CAST(d AS VARCHAR) AS d FROM bars ORDER BY d", params,
                              db=_DB)
    return {"first": {r["ticker"]: r["first"] for r in rows},
            "last": {r["ticker"]: r["last"] for r in rows},
            "sessions": [r["d"] for r in sessions],
            "flat": sorted(r["ticker"] for r in rows if r["flat"] or r["ticker"] in split)}
