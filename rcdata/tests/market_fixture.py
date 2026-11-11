"""A live ``market.duckdb`` for tests that read the builder's market series.

Written for #643 PR 5, where Welcome's four tiles move off the old ``index_levels_daily``, ``vol_index_daily`` and
``macro_series`` tables. The shape is the builder's own (``rcdata/build/market_series.py`` ``SCHEMA``), so a test
seeds the same columns the real file has. Cboe writes one ``value`` per session and no OHLC, which is why
``write_series`` leaves ``close`` NULL unless the caller gives one.
"""
from __future__ import annotations

from datetime import date

import duckdb


def rows(series_id: str, sessions: list[date], values: list[float], source: str = "test",
         ohlc: bool = False) -> list[dict]:
    """``market_series`` rows for one series: one per session, in the sessions' order."""
    if len(sessions) != len(values):
        raise ValueError(f"{series_id}: {len(sessions)} sessions and {len(values)} values")
    return [{"series_id": series_id, "obs_date": d, "source": source,
             "value": None if ohlc else float(v), "open": None, "high": None, "low": None,
             "close": float(v) if ohlc else None, "frequency": "daily", "session_date": d,
             "source_as_of": d, "fetched_at": 1.0, "run_id": "test"}
            for d, v in zip(sessions, values)]


def write_series(series_rows: list[dict], version: int = 1) -> None:
    """A live ``market.duckdb`` holding ``series_rows`` in ``market_series`` (columns it omits are NULL)."""
    from RevenueChaser.rcdata import serving
    from RevenueChaser.rcdata.build.market_series import SCHEMA, TABLE
    path = serving.live_path("market")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute(f"CREATE TABLE IF NOT EXISTS {serving.VERSION_TABLE} (kind TEXT, version INTEGER, "
                    "built_at DOUBLE, build_id TEXT)")
        # Called again to add rows (a later session, a revision), so the version row is written once only.
        if not con.execute(f"SELECT 1 FROM {serving.VERSION_TABLE} WHERE kind = 'market'").fetchone():
            con.execute(f"INSERT INTO {serving.VERSION_TABLE} VALUES ('market', ?, 0, 'test')", [version])
        cols = ", ".join(f'"{c}" {t}' for c, t in SCHEMA)
        con.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} ({cols})")
        names = [c for c, _ in SCHEMA]
        for r in series_rows:
            con.execute(f"INSERT INTO {TABLE} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                        [r.get(c) for c in names])
    finally:
        con.close()
