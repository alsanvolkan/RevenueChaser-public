"""The daily bars of the new prices store (``prices.duckdb``, ``rcdata.read``) in the frame the boards read (PR 10).

Swing and Fundamentals (and the forecast ledger's checks) were written against D01, ``core.prices.daily_bars``: a
pandas frame with ``ticker, session_date, open, high, low, close, volume, source`` per bar, on one of two bases. This
module gives the same frame from ``rcdata.read.bars_by_ticker``, so the boards' code is unchanged. The basis names stay
the boards' own, which are **not** ``rcdata``'s:

| here (the boards' name) | ``rcdata.read.bars`` basis | what it is |
|---|---|---|
| ``nominal`` | ``split`` | every split applied to today's share basis, dividends not (D01's "nominal") |
| ``adjusted`` | ``total_return`` | splits and cash dividends applied (D01's "adjusted", yfinance's ``auto_adjust``) |

Both come from one read: each nominal row carries its ``split_factor`` and ``tr_factor``. Volume is divided by the
split factor on both bases (today's shares). Store only, never a network call; a missing prices store is no bars.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

COLUMNS = ["ticker", "session_date", "open", "high", "low", "close", "volume", "source"]
BASES = ("nominal", "adjusted")
_FACTOR = {"nominal": "split_factor", "adjusted": "tr_factor"}
_PRICES = ("open", "high", "low", "close")


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


def _day(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _num(value) -> float | None:
    return None if value is None else float(value)


def daily_bars_bases(tickers, start=None, end=None, bases: tuple[str, ...] = BASES) -> dict[str, pd.DataFrame]:
    """``{basis: frame}`` for ``tickers`` with ``start <= session_date <= end`` (either bound optional), sorted by
    ticker then session, from one read of the prices store."""
    for basis in bases:
        if basis not in BASES:
            raise ValueError(f"unknown basis {basis!r}")
    from RevenueChaser.rcdata import read
    rows = read.bars_by_ticker(tickers, start=_day(start), end=_day(end))
    if not rows:
        return {basis: _empty() for basis in bases}
    out = {}
    for basis in bases:
        factor = _FACTOR[basis]
        frame = pd.DataFrame({
            "ticker": [r["ticker"] for r in rows],
            "session_date": pd.to_datetime([r["session_date"] for r in rows]),
            **{f: [None if r[f] is None else float(r[f]) * float(r[factor]) for r in rows] for f in _PRICES},
            "volume": [None if r["volume"] is None else float(r["volume"]) / float(r["split_factor"]) for r in rows],
            "source": [r["source"] for r in rows],
        })
        out[basis] = frame[COLUMNS].sort_values(["ticker", "session_date"]).reset_index(drop=True)
    return out


def daily_bars(tickers, basis: str = "nominal", start=None, end=None) -> pd.DataFrame:
    """``daily_bars_bases`` for one basis."""
    if basis not in BASES:
        raise ValueError(f"unknown basis {basis!r}")
    return daily_bars_bases(tickers, start, end, bases=(basis,))[basis]


def last_bars(tickers) -> dict[str, tuple[str, float | None]]:
    """``{ticker: (newest session 'YYYY-MM-DD', fetched_at of that bar)}``: the shape the ledger's checks read."""
    from RevenueChaser.rcdata import read
    return {t: (r["last_session"].isoformat(), _num(r["last_fetched_at"]))
            for t, r in read.last_bars(tickers).items() if r.get("last_session") is not None}


def newest_session() -> date | None:
    """The newest session the prices store holds a bar for, or None (no bars, no file)."""
    from RevenueChaser.rcdata import read
    return _day(read.newest_session())


def tickers_on(day) -> list[str]:
    """The tickers with a usable close on ``day`` in the prices store (``rcdata.read.tickers_on``)."""
    from RevenueChaser.rcdata import read
    return read.tickers_on(_day(day))
