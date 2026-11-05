"""Settings reads the daily bars where the builder writes them: the market store's ``prices.duckdb``.

Since build plan PR 7 the bars live in ``RC_MARKET_DIR/prices.duckdb`` (``price_bar_daily``, with the old
``price_bars_daily`` shape served as a view). The store's own ``prices.duckdb`` under ``RC_STORE_DIR`` is no longer
filled there, so a page that read only it showed an empty price history beside a full one: the live check of
1 Nov 2026 found a 798 KB schema stub in the store and 5.65M bars through 2026-10-30 in the market file.

These tests build a small market file by hand. Its ``price_bars_daily`` is a plain table standing in for the
builder's bridge view (``rcdata/build/prices/serving.py``): same name and the columns these reads use.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb

from RevenueChaser.core import rawstore, universe
from RevenueChaser.module_settings import inventory
from RevenueChaser.rcdata import serving

# A Thursday, 08:00 in New York: the latest complete session is Wednesday 30 Sep 2026.
FIXED = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
SESSION = date(2026, 9, 30)


def _market_prices(bars: list[tuple[str, date, str]], built_at=datetime(2026, 10, 1, 4, 57)) -> None:
    """``bars`` as ``(ticker, session, source)``; one security id per ticker, and an index row that is never a bar."""
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE price_bar_daily (security_id VARCHAR, session_date DATE, close DOUBLE, "
                    "source VARCHAR)")
        con.execute("CREATE TABLE price_bars_daily (ticker VARCHAR, session_date DATE, close DOUBLE, "
                    "source VARCHAR)")
        con.execute("CREATE TABLE serving_schema_version (version INTEGER, built_at TIMESTAMP)")
        con.execute("INSERT INTO serving_schema_version VALUES (1, ?)", [built_at])
        for ticker, day, source in bars:
            con.execute("INSERT INTO price_bar_daily VALUES (?, ?, 1.0, ?)", [f"sym:{ticker}", day, source])
            con.execute("INSERT INTO price_bars_daily VALUES (?, ?, 1.0, ?)", [ticker, day, source])
        con.execute("INSERT INTO price_bar_daily VALUES ('index:SPX', ?, 5000.0, 'cboe')", [SESSION])
    finally:
        con.close()


def _price_row(payload: dict) -> dict:
    return next(r for r in payload["rows"] if r["key"] == "price_bars")


def _seed_universe() -> None:
    rawstore.write_rows("equity_universe", [
        {"ticker": "AAPL", "market_cap": 3.0e12, "name": "Apple"},
        {"ticker": "MU", "market_cap": 1.0e11, "name": "Micron Technology"},
    ])


# ── the "Daily price bars" row ────────────────────────────────────────────────────────────────────────────────────

def test_the_row_reads_the_market_file_beside_an_empty_store_file(rawstore_tmp_db):
    _seed_universe()
    assert rawstore.count("price_bars_daily") == 0, "the store's own file is the empty stub"
    _market_prices([("AAPL", date(2026, 9, 29), "alpaca"), ("AAPL", SESSION, "alpaca"),
                    ("MU", SESSION, "alpaca")])
    row = _price_row(inventory.collect(now=FIXED))
    assert row["rows"] == 3, "index rows are levels, not bars"
    assert row["last_date"] == "2026-09-30"
    assert row["holes"] == 0 and row["hole_label"] == "sessions behind latest"
    assert "market store" in row["note"] and "no bars yet" not in row["note"]
    assert "newest 2026-09-30 vs latest complete 2026-09-30" in row["note"]
    assert "Alpaca 2" in row["note"]
    assert row["fresh"]["text"] == "2026-09-30", "the Freshness cell dates the market file, not the stub"
    assert row["db"] == "market" and row["source"] == "alpaca"


def test_a_market_file_behind_the_calendar_counts_the_sessions_it_misses(rawstore_tmp_db):
    _market_prices([("AAPL", date(2026, 9, 28), "alpaca")])
    row = _price_row(inventory.collect(now=FIXED))
    assert row["last_date"] == "2026-09-28"
    assert row["holes"] == 2, "29 and 30 Sep are complete sessions the file does not hold"


def test_a_built_market_file_with_no_bar_is_empty_not_missing(rawstore_tmp_db):
    _market_prices([])
    row = _price_row(inventory.collect(now=FIXED))
    assert row["rows"] == 0
    assert "market store" in row["note"] and "holds no bar" in row["note"]


def test_an_unreadable_market_file_is_said_and_never_read_as_empty(rawstore_tmp_db):
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a duckdb file")
    payload = inventory.collect(now=FIXED)
    row = _price_row(payload)
    assert row["rows"] is None
    assert "could not be read" in row["note"] and "no bars yet" not in row["note"]
    assert row["fresh"]["state"] == "failed"
    assert any(e.get("table") == "price_bar_daily" for e in payload["errors"])


def test_without_a_market_file_the_row_says_where_it_looked(rawstore_tmp_db):
    row = _price_row(inventory.collect(now=FIXED))
    assert "no bars yet" in row["note"]
    assert "market prices file is not built" in row["note"]


# ── the ticker drill ──────────────────────────────────────────────────────────────────────────────────────────────

def test_the_drill_counts_a_names_bars_in_the_market_file(rawstore_tmp_db):
    _market_prices([("NVDA", date(2026, 9, 29), "alpaca"), ("NVDA", SESSION, "alpaca"),
                    ("AAPL", SESSION, "alpaca")])
    presence = inventory.ticker_detail("NVDA")["presence"]
    assert presence["price_bars_daily"] == {"bars": 2, "newest": "2026-09-30"}


# ── identity coverage ─────────────────────────────────────────────────────────────────────────────────────────────

def test_identity_coverage_counts_bars_in_the_market_file(rawstore_tmp_db):
    _seed_universe()
    _market_prices([("AAPL", SESSION, "alpaca"), ("MU", date(2026, 9, 29), "alpaca")])
    cov = inventory.identity_coverage(universe.classification_universe(), FIXED)
    bars = next(r for r in cov["inputs"] if r["key"] == "bars")
    assert (bars["covered"], bars["state"]) == (1, ""), "MU's newest bar is behind the latest complete session"
