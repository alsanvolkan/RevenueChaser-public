"""ticker_themes — legacy keyword backfill (raw.duckdb tmp path, no network)."""
from __future__ import annotations

from RevenueChaser.core import rawstore, ticker_store
from RevenueChaser.core import ticker_themes as tt


def test_slug():
    assert tt.slug("Biotechnology") == "biotechnology"
    assert tt.slug("Cloud / SaaS") == "cloud-saas"
    assert tt.slug("  ") == ""


def test_parse_themes():
    assert tt.parse_themes('["Biotechnology"]') == ["Biotechnology"]
    assert tt.parse_themes('["A", "B"]') == ["A", "B"]
    assert tt.parse_themes("[]") == []
    assert tt.parse_themes(None) == []
    assert tt.parse_themes("not json") == []
    assert tt.parse_themes('{"a": 1}') == []
    assert tt.parse_themes(["X", " "]) == ["X"]


def test_backfill_writes_legacy_rows(rawstore_tmp_db):
    ticker_store.write_from_cards([
        {"ticker": "ETON", "name": "Eton", "themes": ["Biotechnology"]},
        {"ticker": "MULTI", "name": "Multi", "themes": ["Cloud / SaaS", "AI"]},
        {"ticker": "NONE", "name": "None", "themes": []},
    ], asof="2026-09-20")

    out = tt.backfill_legacy()
    assert out["tickers"] == 2 and out["rows"] == 3

    rows = rawstore.get_all("ticker_themes")
    got = {(r["ticker"], r["theme_id"], r["theme"]) for r in rows}
    assert ("ETON", "biotechnology", "Biotechnology") in got
    assert ("MULTI", "cloud-saas", "Cloud / SaaS") in got
    assert ("MULTI", "ai", "AI") in got
    assert all(r["theme_run_id"] == "legacy-keyword" for r in rows)
    assert all(r["level"] == "legacy" for r in rows)


def test_backfill_is_idempotent(rawstore_tmp_db):
    ticker_store.write_from_cards([{"ticker": "ETON", "themes": ["Biotechnology"]}])
    tt.backfill_legacy()
    first = rawstore.count("ticker_themes")
    tt.backfill_legacy()
    assert first == 1
    assert rawstore.count("ticker_themes") == 1


def test_backfill_empty_store_writes_nothing(rawstore_tmp_db):
    assert tt.backfill_legacy()["rows"] == 0
    assert rawstore.count("ticker_themes") == 0


def test_main_backfill(rawstore_tmp_db):
    ticker_store.write_from_cards([{"ticker": "ETON", "themes": ["Biotech"]}])
    assert tt.main(["--backfill"]) == 0
    assert rawstore.count("ticker_themes") == 1
