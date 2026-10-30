"""The bridge views of ``spine.duckdb`` (D4): the old ``raw.duckdb`` names with the old columns in the old order, so a
reader's SQL runs unchanged against the spine."""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build.spine import views

from .test_spine_security import built, run_all  # noqa: F401 - the built spine of the 10 names

pytestmark = pytest.mark.usefixtures("built")


def _cols(table: str) -> list[str]:
    with serving.open_read("spine") as con:
        return [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]


def _manifest_cols(table: str) -> list[str]:
    return [c.split()[0] for c in rawstore.MANIFEST[table]["columns"] if not c.startswith("PRIMARY KEY")]


def _q(sql: str, *args) -> list[tuple]:
    with serving.open_read("spine") as con:
        return con.execute(sql, list(args)).fetchall()


@pytest.mark.parametrize("table", ["equity_universe", "security_master"])
def test_the_views_keep_the_old_columns_in_the_old_order(table):
    assert _cols(table) == _manifest_cols(table)


def test_index_constituents_and_index_tickers_keep_their_columns():
    assert _cols("index_constituents") == _manifest_cols("index_constituents")
    assert _cols("index_tickers") == ["slug", "ticker", "name", "sector"]


def test_they_are_views_not_tables_so_the_swap_checks_count_only_the_served_tables():
    from RevenueChaser.rcdata.build import base as build_base
    with serving.open_read("spine") as con:
        names = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()}
        tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert set(views.VIEWS) <= names and not set(views.VIEWS) & tables
    # No helper table of the build is left in the file: only the served tables and the version row. The served
    # tables are every spine dataset's own (``spine/security``'s four and ``funds/etf_holdings``' one), not one
    # module's list: a dataset that serves a table of its own belongs in the file as much as the build's do.
    served = {t for d in build_base.kind_datasets("spine") for t in d.tables}
    assert tables == served | {serving.VERSION_TABLE}


def test_equity_universe_is_the_listed_rows_and_the_old_universe_query_runs():
    tickers = {r[0] for r in _q("SELECT ticker FROM equity_universe")}
    assert {"MSFT", "HEI", "HEI-A", "WFC-PC", "SPY"} <= tickers and not {"OTCX", "NOEX"} & tickers
    # core/universe.py's own query, unchanged: no market cap in the spine, so the $300M slice is empty, never wrong.
    assert _q("SELECT ticker FROM equity_universe WHERE COALESCE(market_cap, 0) >= ? ORDER BY market_cap DESC",
              300_000_000.0) == []
    assert _q("SELECT sector, industry FROM equity_universe WHERE ticker = 'MSFT'") == [
        ("Technology", "Software & Services")]


def test_security_master_carries_each_fields_source():
    row = _q("SELECT security_type, type_source, cusip, cusip_source, isin, figi, figi_source, sector_source "
             "FROM security_master WHERE ticker = 'HEI-A'")[0]
    assert row == ("common", "openfigi", "422806208", "wrds/sec_idhist", "US4228062083", "BBG000BL3Y00",
                   "openfigi/map", "wrds/co_hgic")
    assert _q("SELECT security_type FROM security_master WHERE ticker = 'SPY'") == [("etp",)]


def test_index_constituents_are_the_current_fund_members_plus_listed_nasdaq():
    rows = set(_q("SELECT slug, ticker FROM index_constituents"))
    assert {("sp500", "MSFT"), ("sp500", "NVR"), ("sp400", "HEI-A"), ("ndx", "MSFT"), ("nasdaq", "MSFT"),
            ("nasdaq", "VYLR")} <= rows
    assert not {s for s, _ in rows} - {"sp500", "sp400", "sp600", "ndx", "nasdaq"}
    assert ("sp400", "HEI") in rows                     # the fund files' membership, not WRDS's (which ended)
    assert _q("SELECT name, sector FROM index_tickers WHERE slug = 'sp500' AND ticker = 'ETN'") == [
        ("Eaton Corp plc", "Industrials")]
