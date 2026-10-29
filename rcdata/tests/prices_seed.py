"""Seed the live prices store (``prices.duckdb`` in ``config.MARKET_DIR``) in unit tests, without a build (PR 10).

The boards' tests used to seed D01 (``core/tests/d01_seed.py``); since the screeners read ``rcdata.read`` they seed
this file instead. Each call rewrites the live file whole (the serving schema of ``prices/serving``, every table, the
version row), so a test can "rewrite" a series as a later build would. A ticker is its own security
(``sym:<TICKER>``) with one open ``security_symbol`` period; ``factors`` adds ``adj_factor`` periods.

The root conftest gives every test its own ``config.MARKET_DIR`` (``isolate_ops``), so a seed never leaks.
"""
from __future__ import annotations

from datetime import date

import duckdb

# The seeded series per live file: every test has its own ``MARKET_DIR``, so its own entry here.
_STATE: dict[str, tuple[dict[str, list[dict]], list[dict]]] = {}


def _state() -> tuple[dict[str, list[dict]], list[dict]]:
    from RevenueChaser.rcdata import serving
    return _STATE.setdefault(str(serving.live_path("prices")), ({}, []))


def _day(d) -> date:
    return d if isinstance(d, date) else date.fromisoformat(str(d)[:10])


def _write() -> None:
    from RevenueChaser.rcdata import serving
    bars_of, factors = _state()
    from RevenueChaser.rcdata.build.prices.serving import SCHEMAS
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute(f"CREATE TABLE {serving.VERSION_TABLE} (kind TEXT, version INTEGER, built_at DOUBLE, "
                    "build_id TEXT)")
        con.execute(f"INSERT INTO {serving.VERSION_TABLE} VALUES ('prices', 1, 0, 'test')")
        for table, schema in SCHEMAS.items():
            con.execute(f"CREATE TABLE {table} ({', '.join(f'{chr(34)}{c}{chr(34)} {t}' for c, t in schema)})")
        for ticker, rows in bars_of.items():
            sid = f"sym:{ticker}"
            con.execute("INSERT INTO security_symbol VALUES (?, ?, NULL, ?, NULL, 'test')",
                        [sid, ticker, min(r["session_date"] for r in rows) if rows else None])
            for r in rows:
                con.execute("INSERT INTO price_bar_daily (security_id, session_date, symbol, open, high, low, close, "
                            "volume, source, source_as_of, fetched_at, run_id) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'alpaca/bars', ?, ?, 'seed')",
                            [sid, r["session_date"], ticker, r["open"], r["high"], r["low"], r["close"],
                             r["volume"], r["session_date"], r["fetched_at"]])
        for f in factors:
            con.execute("INSERT INTO adj_factor VALUES (?, ?, ?, ?, ?, 1)",
                        [f"sym:{f['ticker']}", f.get("valid_from"), f.get("valid_to"), f["split_factor"],
                         f.get("tr_factor", f["split_factor"])])
    finally:
        con.close()


def seed_bars(ticker: str, days, fetched_at: float, *, close: float = 1.5, volume: float = 100.0,
              replace: bool = True, ohlc: dict | None = None) -> None:
    """One bar per day in ``days`` (dates or ISO texts) for ``ticker``, stamped ``fetched_at``: open 1.0, high 2.0,
    low 0.5 and ``close`` (the old D01 seed's flat bar), unless ``ohlc`` maps a day to its own ``(o, h, l, c)``."""
    ticker = ticker.strip().upper()
    bars_of = _state()[0]
    rows = [] if replace else list(bars_of.get(ticker, []))
    known = {r["session_date"] for r in rows}
    for d in days:
        day = _day(d)
        if day in known:
            continue                                   # first write of a key wins, as the old append did
        o, h, lo, c = (ohlc or {}).get(day, (1.0, 2.0, 0.5, close))
        rows.append({"session_date": day, "open": o, "high": h, "low": lo, "close": c, "volume": volume,
                     "fetched_at": fetched_at})
    bars_of[ticker] = sorted(rows, key=lambda r: r["session_date"])
    _write()


def seed_frame(ticker: str, frame, fetched_at: float) -> None:
    """A ticker's bars from an OHLCV frame (``Open/High/Low/Close/Volume``, a date index), replacing its series."""
    ticker = ticker.strip().upper()
    _state()[0][ticker] = [{"session_date": _day(ix.date() if hasattr(ix, "date") else ix), "open": float(r["Open"]),
                      "high": float(r["High"]), "low": float(r["Low"]), "close": float(r["Close"]),
                      "volume": float(r["Volume"]), "fetched_at": fetched_at} for ix, r in frame.iterrows()]
    _write()


def seed_factor(ticker: str, split_factor: float, *, valid_from=None, valid_to=None, tr_factor=None) -> None:
    """One ``adj_factor`` period of ``ticker``: its bars in ``[valid_from, valid_to)`` times ``split_factor``."""
    _state()[1].append({"ticker": ticker.strip().upper(), "split_factor": split_factor,
                     "valid_from": _day(valid_from) if valid_from else None,
                     "valid_to": _day(valid_to) if valid_to else None, "tr_factor": tr_factor})
    _write()
