"""Seed the live prices store (``prices.duckdb`` in ``config.MARKET_DIR``) in unit tests, without a build (PR 10).

The boards' tests used to seed D01 (``core/tests/d01_seed.py``); since the screeners read ``rcdata.read`` they seed
this file instead. Each call rewrites the live file whole (the serving schema of ``prices/serving``, every table, the
version row), so a test can "rewrite" a series as a later build would. A ticker is its own security
(``sym:<TICKER>``) with one open ``security_symbol`` period; ``factors`` adds ``adj_factor`` periods.

The root conftest gives every test its own ``config.MARKET_DIR`` (``isolate_ops``), so a seed never leaks.
"""
from __future__ import annotations

import math
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
                         f["split_factor"] if f.get("tr_factor") is None else f["tr_factor"]])
    finally:
        con.close()


def seed_bars(ticker: str, days, fetched_at, *, close: float = 1.5, volume: float | None = 100.0,
              replace: bool = True, ohlc: dict | None = None) -> None:
    """One bar per day in ``days`` (dates or ISO texts) for ``ticker``, stamped ``fetched_at`` (a number, or a
    function of the day): open 1.0, high 2.0, low 0.5 and ``close`` (the old D01 seed's flat bar), unless ``ohlc`` maps
    a day to its own ``(o, h, l, c)``. ``volume`` None is a bar without volume."""
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
                     "fetched_at": fetched_at(day) if callable(fetched_at) else fetched_at})
    bars_of[ticker] = sorted(rows, key=lambda r: r["session_date"])
    _write()


def seed_frame(ticker: str, frame, fetched_at: float) -> None:
    """A ticker's bars from an OHLCV frame (``Open/High/Low/Close/Volume``, a date index), replacing its series."""
    ticker = ticker.strip().upper()
    _state()[0][ticker] = [{"session_date": _day(ix.date() if hasattr(ix, "date") else ix), "open": float(r["Open"]),
                      "high": float(r["High"]), "low": float(r["Low"]), "close": float(r["Close"]),
                      "volume": None if r["Volume"] is None or math.isnan(float(r["Volume"])) else float(r["Volume"]),
                      "fetched_at": fetched_at} for ix, r in frame.iterrows()]
    _write()


def seed_factor(ticker: str, split_factor: float, *, valid_from=None, valid_to=None, tr_factor=None) -> None:
    """One ``adj_factor`` period of ``ticker``: its bars in ``[valid_from, valid_to)`` times ``split_factor``."""
    _state()[1].append({"ticker": ticker.strip().upper(), "split_factor": split_factor,
                     "valid_from": _day(valid_from) if valid_from else None,
                     "valid_to": _day(valid_to) if valid_to else None, "tr_factor": tr_factor})
    _write()


def seed_actions(ticker: str, closes: dict, actions) -> None:
    """``adj_factor`` periods for ``actions`` (``(ex_date, "split" | "dividend", value)``), as the builder derives
    them from the nominal ``closes`` (``{day: close}``): every bar before an ex-date is multiplied by ``1 / ratio``
    for a split (split and total-return factor) and by ``1 - value / the close before it`` for a cash dividend (the
    total-return factor only)."""
    edges = sorted((_day(ex), kind, float(v)) for ex, kind, v in actions)
    days = sorted(_day(d) for d in closes)
    close_of = {_day(d): float(c) for d, c in closes.items()}
    for i, (ex, _kind, _v) in enumerate(edges):
        split = tr = 1.0
        for ex2, kind2, v2 in edges[i:]:
            if kind2 == "split":
                split, tr = split / v2, tr / v2
            else:
                tr *= 1.0 - v2 / close_of[max(d for d in days if d < ex2)]
        seed_factor(ticker, split, valid_from=edges[i - 1][0] if i else None, valid_to=ex, tr_factor=tr)


def drop_session(day) -> None:
    """Remove every ticker's bar of ``day``, as if the builder had not read that session yet."""
    day = _day(day)
    bars_of = _state()[0]
    for ticker in list(bars_of):
        bars_of[ticker] = [r for r in bars_of[ticker] if r["session_date"] != day]
    _write()


def drop_ticker(ticker: str) -> None:
    """Remove ``ticker``'s bars and factors, as if the store had never held it."""
    ticker = ticker.strip().upper()
    bars_of, factors = _state()
    bars_of.pop(ticker, None)
    factors[:] = [f for f in factors if f["ticker"] != ticker]
    _write()
