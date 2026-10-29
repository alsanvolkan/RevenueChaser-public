"""Test helpers: stored daily bars for the Rotation tests and a pinned session clock (M-ROT-2, #374).

Since M-ROT-2 the RRG, the backfill and (M-ROT-3) the holdings RS and Money Flow read the stored daily bars; since PR 9
(G3) those are the prices store's (``prices.duckdb``, ``rcdata/tests/prices_store``), not D01's. A test seeds them the
way the prices build leaves them, and pins ``core.calendar``'s clock seam to place "the latest complete session". The
module was named ``d01_seed`` until review S4 of PR 270.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

from RevenueChaser.core import calendar
from RevenueChaser.rcdata.tests import prices_store

FIRST = date(2025, 3, 3)


def sessions(start: date, end: date) -> list[date]:
    """Trading sessions in ``[start, end]``, oldest first."""
    out, d = [], start
    while d <= end:
        if calendar.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def seed(ticker: str, end: date, start: date = FIRST, closes=None, volume: float = 1_000_000.0) -> list[date]:
    """Stored bars of ``ticker`` on every session in ``[start, end]``. ``closes`` is a function of the bar's index, or
    ``None`` for a deterministic path whose drift is seeded by the ticker (so ETFs land in different quadrants).
    Returns the sessions written."""
    days = sessions(start, end)
    seed_ = int(hashlib.sha256(ticker.encode()).hexdigest()[:8], 16)
    drift = ((seed_ % 21) - 10) / 2000.0
    price, rows = 100.0, []
    for i, d in enumerate(days):
        if closes is None:
            price *= 1.0 + drift + (((seed_ >> (i % 16)) & 3) - 1.5) / 400.0
            close = round(price, 4)
        else:
            close = float(closes(i))
        rows.append({"ticker": ticker, "session_date": d, "open": close, "high": close, "low": close, "close": close,
                     "volume": volume + i, "fetched_at": 0.0})
    prices_store.put_bars(rows, replace=False, overwrite=True)
    return days


def pin_clock(monkeypatch, when: datetime) -> None:
    """Pin ``core.calendar``'s clock seam to the aware datetime ``when``."""
    utc = when.astimezone(timezone.utc)
    monkeypatch.setattr(calendar, "_now", lambda: utc)


def ny(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=calendar.NY)
