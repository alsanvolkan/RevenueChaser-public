"""Write atomicity — a failed refresh must never destroy the previous data.

DuckDB autocommits every statement, so the DELETE half of a "replace" write (or
a ``clear()`` followed by a separate ``write_rows``) used to commit on its own:
a batch that failed to insert left the table EMPTIED. The store's docstring
promises "no silent data loss"; the exception propagating is only half of that.

Every test here seeds good rows, attempts a refresh that fails partway, and
asserts the good rows are still exactly as they were.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore

# a value DuckDB cannot cast into the DOUBLE/INTEGER column it lands in, so the
# failure happens INSIDE executemany — after the DELETE, mid-batch
BAD = "not-a-number"


def _ohlc(ts: str, close: float, bad: bool = False):
    return {"ticker": "AAPL", "ts": ts, "open": BAD if bad else 1.0, "high": 1.0,
            "low": 1.0, "close": close, "volume": 1.0, "cached_at": 1.0}


class TestReplaceMode:
    def test_failed_replace_keeps_the_previous_rows(self, rawstore_tmp_db):
        good = [_ohlc(f"2026-01-0{i}", float(i)) for i in range(1, 6)]
        rawstore.write_rows("swing_ohlc", good)
        assert rawstore.count("swing_ohlc") == 5

        with pytest.raises(Exception):
            rawstore.write_rows("swing_ohlc", [_ohlc("2026-02-01", 9.0),
                                               _ohlc("2026-02-02", 9.0, bad=True)])

        rows = rawstore.query("SELECT ts, close FROM swing_ohlc ORDER BY ts")
        assert [r["ts"] for r in rows] == [f"2026-01-0{i}" for i in range(1, 6)]
        assert [r["close"] for r in rows] == [1.0, 2.0, 3.0, 4.0, 5.0]

    def test_successful_replace_still_swaps_the_group(self, rawstore_tmp_db):
        rawstore.write_rows("swing_ohlc", [_ohlc("2026-01-01", 1.0)])
        rawstore.write_rows("swing_ohlc", [_ohlc("2026-02-01", 2.0)])
        rows = rawstore.query("SELECT ts FROM swing_ohlc")
        assert [r["ts"] for r in rows] == ["2026-02-01"]

    def test_replace_only_clears_its_own_group(self, rawstore_tmp_db):
        rawstore.write_rows("index_constituents", [{"slug": "sp500", "ticker": "AAPL"}])
        rawstore.write_rows("index_constituents", [{"slug": "nasdaq100", "ticker": "MSFT"}])
        rawstore.write_rows("index_constituents", [{"slug": "sp500", "ticker": "NVDA"}])
        rows = rawstore.query("SELECT slug, ticker FROM index_constituents ORDER BY slug")
        assert [(r["slug"], r["ticker"]) for r in rows] == [("nasdaq100", "MSFT"), ("sp500", "NVDA")]


class TestUpsertMode:
    def test_failed_upsert_keeps_the_previous_rows(self, rawstore_tmp_db):
        rawstore.write_rows("ticker_payload_cache", [{"ticker": "AAPL", "payload": {"x": 1},
                                              "fetched_at": 1.0, "fetched_at_iso": "a"}])
        with pytest.raises(Exception):
            rawstore.write_rows("ticker_payload_cache", [
                {"ticker": "AAPL", "payload": {"x": 2}, "fetched_at": 2.0, "fetched_at_iso": "b"},
                {"ticker": "MSFT", "payload": {"x": 3}, "fetched_at": BAD, "fetched_at_iso": "c"},
            ])
        rows = rawstore.get_all("ticker_payload_cache")
        assert len(rows) == 1
        assert rows[0]["payload"] == {"x": 1} and rows[0]["fetched_at"] == 1.0


class TestReplaceAll:
    def test_replace_all_removes_keys_absent_from_the_new_set(self, rawstore_tmp_db):
        rawstore.replace_all("watchlist", [{"ticker": "AAPL"}, {"ticker": "MSFT"}])
        rawstore.replace_all("watchlist", [{"ticker": "NVDA"}])
        assert rawstore.load_watchlist() == {"NVDA"}

    def test_failed_replace_all_keeps_the_previous_rows(self, rawstore_tmp_db):
        rawstore.replace_all("ticker_scores", [
            {"ticker": "AAPL", "name": "Apple", "market_cap": 1.0, "total_points": 3},
            {"ticker": "MSFT", "name": "Microsoft", "market_cap": 2.0, "total_points": 4},
        ])
        with pytest.raises(Exception):
            rawstore.replace_all("ticker_scores", [
                {"ticker": "NVDA", "name": "Nvidia", "market_cap": 3.0, "total_points": 5},
                {"ticker": "AMD", "name": "AMD", "market_cap": BAD, "total_points": 6},
            ])
        assert {r["ticker"] for r in rawstore.get_all("ticker_scores")} == {"AAPL", "MSFT"}

    def test_replace_all_with_no_rows_empties_the_table_when_asked(self, rawstore_tmp_db):
        # an empty batch is refused by default (#284, TestReplaceAllEmptyGuard); empty is opt-in
        rawstore.replace_all("watchlist", [{"ticker": "AAPL"}])
        assert rawstore.replace_all("watchlist", [], allow_empty=True) == 0
        assert rawstore.load_watchlist() == set()


class TestTickerStoreFullRefresh:
    """``write_from_cards`` is the single writer for a table other boards read;
    a failed scan must not leave them looking at an empty store."""

    def test_failed_write_from_cards_keeps_the_previous_scan(self, rawstore_tmp_db):
        from RevenueChaser.core import ticker_store
        ticker_store.write_from_cards(
            [{"ticker": "ETON", "name": "Eton", "market_cap": 1.8e9, "total_points": 9,
              "themes": ["Biotech"], "signals": []}], asof="2026-08-22")
        assert ticker_store.count() == 1

        with pytest.raises(Exception):
            ticker_store.write_from_cards([
                {"ticker": "AAPL", "name": "Apple", "market_cap": 1.0, "total_points": 1},
                {"ticker": "MSFT", "name": "Microsoft", "market_cap": BAD, "total_points": 2},
            ], asof="2026-08-23")

        assert ticker_store.count() == 1
        assert ticker_store.get("ETON")["themes"] == ["Biotech"]
        assert ticker_store.asof() == "2026-08-22"

    def test_write_from_cards_drops_tickers_absent_from_the_new_scan(self, rawstore_tmp_db):
        from RevenueChaser.core import ticker_store
        ticker_store.write_from_cards([{"ticker": "AAPL", "name": "Apple"},
                                       {"ticker": "MSFT", "name": "Microsoft"}])
        ticker_store.write_from_cards([{"ticker": "AAPL", "name": "Apple"}])
        assert [r["ticker"] for r in ticker_store.list_tickers()] == ["AAPL"]


class TestMultiTableTransaction:
    def test_transaction_commits_every_table_together(self, rawstore_tmp_db):
        with rawstore.transaction() as tx:
            tx.write_rows("sec13f_submissions", [{"accession": "acc1", "cik": "1", "period": "2026-03-31",
                                                  "filing_date": "01-MAY-2026", "type": "13F-HR"}])
            tx.write_rows("sec13f_ingested_zips", [{"filename": "z.zip", "ingested_at": "now"}])
        assert rawstore.count("sec13f_submissions") == 1
        assert rawstore.count("sec13f_ingested_zips") == 1

    def test_transaction_rolls_every_table_back_on_failure(self, rawstore_tmp_db):
        with pytest.raises(Exception):
            with rawstore.transaction() as tx:
                tx.write_rows("sec13f_submissions", [{"accession": "acc1", "cik": "1", "period": "p",
                                                      "filing_date": "d", "type": "13F-HR"}])
                raise RuntimeError("interrupted")
        assert rawstore.count("sec13f_submissions") == 0

    def test_connection_is_reusable_after_a_rolled_back_transaction(self, rawstore_tmp_db):
        with pytest.raises(RuntimeError):
            with rawstore.transaction() as tx:
                tx.write_rows("watchlist", [{"ticker": "AAPL"}])
                raise RuntimeError("boom")
        rawstore.write_rows("watchlist", [{"ticker": "MSFT"}])
        assert rawstore.load_watchlist() == {"MSFT"}
