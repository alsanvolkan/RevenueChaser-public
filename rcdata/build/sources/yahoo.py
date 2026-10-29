"""Yahoo's daily history for the two market-wide series no other source serves: the ICE BofA MOVE index (``^MOVE``)
and CBOT wheat futures (``ZW=F``, the front month), series ids ``MOVE`` and ``WHEATF`` (the names the Sector Rotation
map uses, ``core/rotation_reference.MACRO_SERIES``; PR 9).

Read through the ``yfinance`` library (``Ticker.history(period="max", interval="1d")``), as the app's other Yahoo
reads are (the float, ``rcdata/build/prices/floats.py``): Yahoo's chart endpoint refuses the builder's own
User-Agent (HTTP 429, measured 29 Oct 2026), and asked directly with ``range=max`` it answers monthly bars. Through
the library (1.7.0, 29 Oct 2026) ``^MOVE`` gave 5,925 daily bars from 12 Nov 2002 and ``ZW=F`` 6,590 from
17 Jul 2000. One request per symbol and as-of date, the whole history each time, so a daily fetch is also the
backfill; counted under the ``yfinance`` provider (its day cap). Raw: ``raw/yahoo/history/<as-of>/<SERIES>.json``,
the bars as ``[day, open, high, low, close]``. A bar is dated by the exchange's own day; a bar of a day whose session
is not over when it was fetched is skipped (16:00 New York), and the next fetch brings it complete.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import base

SOURCE, DATASET_DIR = "yahoo", "history"
PROVIDER = "yfinance"
SYMBOLS = {"MOVE": "^MOVE", "WHEATF": "ZW=F"}
NY = ZoneInfo("America/New_York")
# shortcut: one New York cut-off for both; wheat settles at 13:20 Chicago, so its bar waits up to two hours longer
# than it must. Upgrade to a per-symbol close if a same-afternoon wheat bar ever matters.
CLOSED_HOUR = 16


def _history(symbol: str) -> list[list]:
    """The daily bars of ``symbol`` from yfinance, ``[[ISO day, open, high, low, close], ...]`` oldest first."""
    import yfinance as yf
    frame = yf.Ticker(symbol).history(period="max", interval="1d", auto_adjust=False, actions=False)
    return [[ts.date().isoformat(), *(_num(row.get(k)) for k in ("Open", "High", "Low", "Close"))]
            for ts, row in frame.iterrows()]


def fetch(ctx, as_of: date, download=None) -> list[Path]:
    from ..market_series import save_raw
    get = download or _history
    out, failed = [], 0
    for series, symbol in SYMBOLS.items():
        path = base.raw_root() / SOURCE / DATASET_DIR / as_of.isoformat() / f"{series}.json"
        if not path.exists():
            ctx.check_cap(PROVIDER)
            ctx.count(PROVIDER, calls=1)
            try:
                bars = get(symbol)
            except Exception as e:  # noqa: BLE001 - the library raises its own types; one symbol down is an issue
                bars, why = None, f"{type(e).__name__}: {e}"[:300]
            else:
                why = "yfinance answered no bars"
            if not bars:
                ctx.count(PROVIDER, failures=1)
                ctx.issue("fetch_failed", f"yahoo:{series}", why)
                failed += 1
                continue
            save_raw(ctx, path, json.dumps({"symbol": symbol, "bars": bars}).encode(), as_of,
                     f"yfinance:{symbol}", series=series, symbol=symbol)
        out.append(path)
    if failed and not out:
        raise base.SourceError(f"every yahoo symbol failed ({failed})")
    return out


def backfill(ctx, start: date, end: date) -> list[Path]:
    return fetch(ctx, end)


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse(text: str, meta: dict) -> list[dict]:
    series = meta["series"]
    fetched = datetime.fromtimestamp(float(meta["fetched_at"]), NY)
    rows, seen = [], set()
    for bar in json.loads(text).get("bars") or []:
        day = date.fromisoformat(str(bar[0])[:10])
        o, h, lo, close = (_num(v) for v in bar[1:5])
        # A bar of the fetch's own day is complete only once that day's session is over.
        if close is None or day in seen or (day == fetched.date() and fetched.hour < CLOSED_HOUR):
            continue
        seen.add(day)
        rows.append({"series_id": series, "obs_date": day, "value": close, "open": o, "high": h, "low": lo,
                     "close": close, "frequency": "daily"})
    return rows
