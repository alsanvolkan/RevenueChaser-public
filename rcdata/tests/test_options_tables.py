"""The ``options`` serving kind (G8): its version, its tables built from Parquet, and the window of ``option_eod``."""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.options import tables


def test_the_options_kind_is_served_at_version_3():
    """2: the calendar fields on ``option_screener_daily``, ``option_far_print`` and ``earnings_tape`` (#644).
    3: ``option_far_contract``, the far-dated contracts and their next-session open interest (#746)."""
    assert serving.SERVING_SCHEMA_VERSION["options"] == 3
    assert {"option_far_print", "earnings_tape", "option_far_contract"} <= set(tables.TABLES)
    assert tables.TABLES["option_far_contract"].key == ("session_date", "option_symbol")
    assert tables.TABLES["option_far_contract"].window is None, "every session is served"
    assert {"expected_move_perc", "street_mean_est", "pre_earnings_close", "post_earnings_close", "report_time",
            "full_name", "marketcap", "realized_volatility", "stock_volume"} <= set(
        tables.TABLES["option_screener_daily"].columns)
    assert "options" in serving.KINDS


def test_every_options_table_carries_its_key_and_the_provenance_columns():
    for t in tables.TABLES.values():
        cols = [c for c, _ in t.schema]
        assert len(cols) == len(set(cols)), t.name
        assert set(t.key) <= set(cols), t.name
        assert {"source", "source_as_of", "fetched_at", "run_id", "observed"} <= set(cols), t.name
        assert t.sources, t.name


def _eod_rows(session: date, n: int) -> list[dict]:
    return [{"session_date": session, "ticker": "AAPL", "option_symbol": f"AAPL261120C{i:08d}",
             "expiry": date(2026, 11, 20), "strike": float(i), "option_type": "call", "source": "uw",
             "source_as_of": session, "fetched_at": 1.0, "run_id": "r", "observed": True} for i in range(n)]


def _sessions(n: int) -> list[date]:
    first, out = date(2026, 6, 1), []
    while len(out) < n:
        if first.weekday() < 5:
            out.append(first)
        first += timedelta(days=1)
    return out


def _build(names) -> dict[str, int]:
    with serving.build("options") as b:
        counts = tables.build_tables(b.con, names)
        b.swap(counts)
    return counts


def test_option_eod_keeps_the_last_30_sessions_and_one_row_per_contract_and_date():
    t = tables.TABLES["option_eod"]
    days = _sessions(32)
    for d in days:
        rows = _eod_rows(d, 3)
        path = tables.partition_path("uw/option_list", t.name, d)
        base.upsert_partition(rows + rows[:1], t.schema, list(t.key), path)
    counts = _build(["option_eod"])
    assert counts == {"option_eod": 30 * 3}
    with serving.open_read("options") as con:
        first, last, n = con.execute("SELECT min(session_date), max(session_date), count(DISTINCT session_date) "
                                     "FROM option_eod").fetchone()
        dup = con.execute("SELECT count(*) FROM (SELECT session_date, option_symbol FROM option_eod "
                          "GROUP BY ALL HAVING count(*) > 1)").fetchone()[0]
    assert (first, last, n) == (days[2], days[-1], 30) and dup == 0


def test_a_window_may_slide_past_a_short_new_day_but_never_lose_rows_inside_it():
    """The shrink check (B2) compares the rows both files are meant to hold: a session that leaves the window is not
    a loss, a session inside it that lost rows is."""
    t = tables.TABLES["option_eod"]
    days = _sessions(31)
    for d in days[:30]:
        path = tables.partition_path("uw/option_list", t.name, d)
        base.upsert_partition(_eod_rows(d, 10), t.schema, list(t.key), path)
    _build(["option_eod"])
    # The next session's first run typed only 2 names' worth: 292 rows against 300 live, but nothing inside the new
    # window is missing.
    base.upsert_partition(_eod_rows(days[30], 2), t.schema, list(t.key),
                          tables.partition_path("uw/option_list", t.name, days[30]))
    assert _build(["option_eod"]) == {"option_eod": 292}
    # A partition inside the window that lost rows is a shrink, and is refused.
    part = tables.partition_path("uw/option_list", t.name, days[10])
    part.unlink()
    base.upsert_partition(_eod_rows(days[10], 4), t.schema, list(t.key), part)
    with pytest.raises(serving.SwapRefused, match="shrinks"):
        _build(["option_eod"])


def test_a_market_build_without_a_window_still_refuses_any_shrink():
    with serving.build("market") as b:
        b.con.execute("CREATE TABLE t AS SELECT range AS i FROM range(10)")
        b.swap({"t": 10})
    with pytest.raises(serving.SwapRefused, match="shrinks"):
        with serving.build("market") as b:
            b.con.execute("CREATE TABLE t AS SELECT range AS i FROM range(9)")
            b.swap({"t": 9})


def test_a_table_whose_parquet_never_ran_is_built_empty():
    counts = _build(list(tables.TABLES))
    assert counts == {name: 0 for name in tables.TABLES}
    con = duckdb.connect(str(serving.live_path("options")), read_only=True)
    try:
        keys = {r[0] for r in con.execute("SELECT table_name FROM duckdb_constraints() "
                                          "WHERE constraint_type = 'PRIMARY KEY'").fetchall()}
    finally:
        con.close()
    assert keys == set(tables.TABLES)


# ── Parquet written before a column was added (10 Nov 2026 whole build: "realized_volatility" not found) ─────────

_NEW_SCREENER_COLUMNS = {"realized_volatility", "stock_volume", "expected_move_perc", "street_mean_est",
                         "pre_earnings_close", "post_earnings_close", "full_name", "report_time"}


def _screener_row(session: date, ticker: str, fetched_at: float, **extra) -> dict:
    return {"session_date": session, "ticker": ticker, "close": 10.0, "source": "uw", "source_as_of": session,
            "fetched_at": fetched_at, "run_id": "r", "observed": True, **extra}


def _old_screener_partition(session: date, tickers=("AAPL",)):
    """A screener partition in the shape options 1 wrote: none of the columns options 2 added."""
    t = tables.TABLES["option_screener_daily"]
    old = [(c, typ) for c, typ in t.schema if c not in _NEW_SCREENER_COLUMNS]
    path = tables.partition_path(tables.SCREENER_SOURCE, t.name, session)
    base.upsert_partition([_screener_row(session, k, 1.0) for k in tickers], old, list(t.key), path)
    return t, path


def test_a_build_over_only_old_screener_parquet_reads_the_new_columns_as_null():
    _old_screener_partition(date(2026, 11, 9), ("AAPL", "MSFT"))
    assert _build(["option_screener_daily"]) == {"option_screener_daily": 2}
    with serving.open_read("options") as con:
        got = con.execute("SELECT count(*), count(realized_volatility), count(stock_volume), "
                          "count(expected_move_perc), min(close) FROM option_screener_daily").fetchone()
    assert got == (2, 0, 0, 0, 10.0)


def test_an_upsert_into_an_old_screener_partition_keeps_its_rows_and_adds_the_new_columns():
    session = date(2026, 11, 9)
    t, path = _old_screener_partition(session)
    base.upsert_partition([_screener_row(session, "MSFT", 2.0, realized_volatility=0.25, stock_volume=1000.0)],
                          t.schema, list(t.key), path)
    con = duckdb.connect()
    try:
        src = f"read_parquet({base.sql_path(path)})"
        rows = con.execute(f"SELECT ticker, realized_volatility, stock_volume FROM {src} ORDER BY ticker").fetchall()
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    finally:
        con.close()
    assert rows == [("AAPL", None, None), ("MSFT", 0.25, 1000.0)]
    assert set(t.columns) <= cols  # DuckDB adds the hive "date" column of the partition path


def test_the_check_against_a_live_file_built_before_options_2_compares_the_columns_both_hold():
    """The live file of 10 Nov still had the options 1 screener table: the check must not ask it for a new column."""
    _old_screener_partition(date(2026, 11, 9), ("AAPL", "MSFT"))
    _build(["option_screener_daily"])
    con = duckdb.connect(str(serving.live_path("options")))
    try:
        for c in ("realized_volatility", "stock_volume"):
            con.execute(f'ALTER TABLE option_screener_daily DROP COLUMN "{c}"')
    finally:
        con.close()
    t = tables.TABLES["option_screener_daily"]
    with serving.build("options") as b:
        tables.build_tables(b.con, [t.name])
        assert tables.compare_by_day(b.con, t) == (0, 0)
