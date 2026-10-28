"""The bridge (D4): views in ``spine.duckdb`` under the old ``raw.duckdb`` table names, with the old columns.

A reader written against ``equity_universe``, ``security_master``, ``index_constituents`` or ``index_tickers`` can
point at the spine and run its SQL unchanged. Each view keeps the old column list and order; a column the spine has
no source for is NULL of its old type (market cap, price and the quote are market data, not identity; SIC is not
fetched by the spine), never a made-up value.

- ``equity_universe``: the listed rows (Tier A and B), the old table's membership rule (#264); the type filter stays
  with the reader (``core/universe.py``), as before.
- ``security_master``: every SEC-file row, with each field's source.
- ``index_constituents``: the current fund-file members (``sp500``, ``sp400``, ``sp600``, ``ndx``) and ``nasdaq``, the
  listed Nasdaq rows (the old slug meant "listed on Nasdaq"). ``dow`` has no source in the spine and is absent.
- ``index_tickers``: as ``core/rawstore.py`` defines it, over the two views above.
"""
from __future__ import annotations

import duckdb

VIEWS: dict[str, str] = {
    "equity_universe": (
        "SELECT ticker, CAST(NULL AS DOUBLE) AS market_cap, sector, CAST(NULL AS DOUBLE) AS price, name, industry, "
        "CAST(NULL AS TEXT) AS industry_group, CAST(NULL AS TEXT) AS sub_industry, CAST(NULL AS TEXT) AS quote, "
        "CAST(NULL AS TEXT) AS next_earnings_date FROM security WHERE tier IN ('A', 'B')"),
    "security_master": (
        "SELECT ticker, cik, source AS cik_source, exchange, name, source AS name_source, security_type, type_source, "
        "CAST(source_as_of AS TEXT) AS type_asof, sector, gics_source AS sector_source, "
        "CAST(NULL AS TEXT) AS industry_group, CAST(NULL AS TEXT) AS group_source, industry, "
        "gics_source AS industry_source, CAST(NULL AS TEXT) AS sub_industry, CAST(NULL AS TEXT) AS sub_source, "
        "CAST(gics_from AS TEXT) AS gics_asof, CAST(source_as_of AS TEXT) AS updated_at, figi, composite_figi, "
        "share_class_figi, exch_code, security_description, figi_source, CAST(NULL AS TEXT) AS sic, "
        "CAST(NULL AS TEXT) AS sic_description, CAST(NULL AS TEXT) AS sic_group2, CAST(NULL AS TEXT) AS sic_source, "
        "cusip, cusip_source, isin, isin_source FROM security"),
    "index_constituents": (
        "SELECT index_slug AS slug, ticker FROM index_membership "
        "WHERE source = 'funds/index_holdings' AND valid_to IS NULL AND ticker IS NOT NULL "
        "UNION ALL SELECT 'nasdaq' AS slug, ticker FROM security WHERE exchange = 'Nasdaq' AND tier IN ('A', 'B')"),
    "index_tickers": (
        "SELECT c.slug, c.ticker, u.name, u.sector FROM index_constituents c "
        "LEFT JOIN equity_universe u ON u.ticker = c.ticker"),
}


def create(con: duckdb.DuckDBPyConnection) -> None:
    """Create every view, in order (``index_tickers`` reads the two before it)."""
    for name, sql in VIEWS.items():
        con.execute(f"CREATE VIEW {name} AS {sql}")
