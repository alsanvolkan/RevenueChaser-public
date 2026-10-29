"""``peer_group`` (build plan PR 12): each Tier A company's ``K`` nearest companies on the GICS ladder (sub-industry,
then industry, industry group, sector), the closest annual revenue first within a rung."""
from __future__ import annotations

import duckdb

from RevenueChaser.rcdata.build.fundamentals import tables

from . import fund_fixture as fx

# (cik, tickers, gsector, ggroup, gind, gsubind, annual revenue or None)
COMPANIES = [
    ("1", ["AAA", "AAA-B"], "45", "4510", "451030", "45103020", 100.0),
    ("2", ["BBB"], "45", "4510", "451030", "45103020", 1_000.0),
    ("3", ["CCC"], "45", "4510", "451030", "45103020", 120.0),
    ("8", ["HHH"], "45", "4510", "451030", "45103020", None),           # no revenue: last in its rung
    ("4", ["DDD"], "45", "4510", "451030", "45103010", 100.0),          # same industry
    ("5", ["EEE"], "45", "4510", "451020", "45102010", 100.0),          # same industry group
    ("6", ["FFF"], "45", "4520", "452010", "45201010", 100.0),          # same sector
    ("7", ["GGG"], "20", "2010", "201010", "20101010", 100.0),          # another sector
]


def _con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    tables.create(con, "fundamental_company")
    tables.create(con, "fundamental_fact")
    for cik, tickers, gsector, ggroup, gind, gsubind, revenue in COMPANIES:
        for t in tickers:
            con.execute("INSERT INTO fundamental_company (ticker, cik, gsector, ggroup, gind, gsubind) "
                        "VALUES (?, ?, ?, ?, ?, ?)", [t, cik, gsector, ggroup, gind, gsubind])
        if revenue is not None:
            for year, value in ((2024, revenue / 2), (2025, revenue)):          # the newest year counts
                con.execute("INSERT INTO fundamental_fact (cik, field, period_type, fiscal_year, period_end, value, "
                            "accn, filed, derived, preliminary, source) VALUES (?, 'revenue', 'annual', ?, ?, ?, "
                            "'a', DATE '2026-01-01', false, false, 'sec/companyfacts')",
                            [cik, year, f"{year}-12-31", value])
    return con


def test_the_ladder_fills_from_the_sub_industry_out_by_revenue():
    con = _con()
    assert tables._peers(con, k=5) > 0
    rows = con.execute("SELECT rank, peer_ticker, level, peer_revenue FROM peer_group WHERE cik = '1' "
                       "ORDER BY rank").fetchall()
    assert rows == [(1, "CCC", "gsubind", 120.0), (2, "BBB", "gsubind", 1_000.0), (3, "HHH", "gsubind", None),
                    (4, "DDD", "gind", 100.0), (5, "EEE", "ggroup", 100.0)]
    assert con.execute("SELECT DISTINCT ticker, revenue FROM peer_group WHERE cik = '1'").fetchall() == [
        ("AAA", 100.0)]                                                  # the company's shortest ticker
    assert con.execute("SELECT count(*) FROM peer_group WHERE cik = '7'").fetchone() == (0,)   # alone in its sector


def test_the_build_serves_it():
    fx.spine()
    fx.run(tables.NAME, "2026-10-28")
    assert fx.q("SELECT ticker, peer_ticker, rank, level FROM peer_group WHERE cik = '789019'") == [
        ("MSFT", "ORCL", 1, "gsubind")]
