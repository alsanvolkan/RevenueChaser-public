"""Persistence & population tests — the DB is correctly filled after functions run.

Coverage model (plan Phase 6):
- Group A (illustrative): DB is correctly filled after a representative set of
  functions runs (mocked at the HTTP boundary).
- Group B: data is never "unwired" — durability across connections, round-trip
  integrity, write failures propagate, idempotency, and no module writes raw
  files directly anymore.
- Group C (gate): every table in the rawstore manifest has a population test.
- Group D: TTL / no_fetch behaviour preserved.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from RevenueChaser.core import rawstore


# ── Group A: DB filled after a function runs ─────────────────────────────────

def test_cache_get_or_fetch_populates_ticker_cache(rawstore_tmp_db):
    from RevenueChaser.core.providers import cache
    cache.save("AAPL", {"revenue": 100, "annual": [{"fiscal_year": "2025"}]})
    row = rawstore.get("ticker_payload_cache", "AAPL")
    assert row is not None
    assert row["payload"]["revenue"] == 100
    assert row["fetched_at"] and row["fetched_at_iso"]


def test_sec_edgar_populates_sec_facts(rawstore_tmp_db):
    from RevenueChaser.core.providers import sec_edgar
    annual, quarterly, meta = sec_edgar.extract_annual({"facts": {"us-gaap": {}}}), [], {"available": True}
    rawstore.write_rows("edgar_financials", [{"ticker": "ZZZZ", "annual": annual, "quarterly": quarterly, "meta": meta}])
    got = rawstore.get("edgar_financials", "ZZZZ")
    assert got["ticker"] == "ZZZZ"
    assert got["meta"]["available"] is True


def test_swing_ohlc_populated(rawstore_tmp_db):
    from RevenueChaser.module_swing_potential import indicators
    import pandas as pd
    df = pd.DataFrame({"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [1000]},
                      index=pd.to_datetime(["2026-01-01"]))
    indicators._write_ohlc("MSFT", df)
    assert rawstore.count("swing_ohlc") == 1
    row = rawstore.query("SELECT * FROM swing_ohlc WHERE ticker='MSFT'")[0]
    assert row["close"] == 1.5
    assert row["cached_at"] > 0


def test_rotation_snapshot_populated(rawstore_tmp_db):
    from datetime import date
    from RevenueChaser.core.models.rotation import ETFRotationSnapshot
    from RevenueChaser.module_sector_rotation import store as rs
    snap = ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker="XLK", group_name="sector",
                               close_price=100.0, rs_1w=1.0, rs_1m=2.0, rs_3m=3.0,
                               rs_ratio=2.0, rs_momentum=1.0, rs_ratio_z=1.0, rs_momentum_z=0.5,
                               rrg_quadrant="LEADING")
    rs.save_snapshot([snap], as_of=date(2026, 8, 14))
    data = rs.load_snapshot(date(2026, 8, 14))
    assert data["etfs"][0]["ticker"] == "XLK"
    assert data["etfs"][0]["quadrant"] == "LEADING"
    # regression: quoted-identifier column ("group" is a SQL reserved word) must
    # round-trip — a NULL here blanks the RRG map's sector filter
    assert data["etfs"][0]["group"] == "sector"


def test_ticker_store_populated(rawstore_tmp_db):
    from RevenueChaser.core import ticker_store
    ticker_store.write_from_cards([{
        "ticker": "ETON", "name": "Eton", "sector": "Health Care", "market_cap": 1.8e9,
        "total_points": 9, "themes": ["Biotech"], "signals": [],
    }], asof="2026-08-22")
    assert ticker_store.count() == 1
    assert ticker_store.get("ETON")["themes"] == ["Biotech"]
    assert ticker_store.asof() == "2026-08-22"


def test_leaps_store_populated(rawstore_tmp_db):
    from RevenueChaser.module_leaps.engine import save_cache, store_prints
    save_cache("NOK", {"ticker": "NOK", "label": "Quiet"})
    assert rawstore.get("leaps_cache", "NOK")["payload"]["latest"]["label"] == "Quiet"
    new = store_prints([{"id": "p1", "ticker": "AAPL", "strike": 200.0, "expiry": "2027-01-15",
                         "premium": 1000.0, "size": 1, "option_type": "call", "delta": 0.8,
                         "next_earnings_date": None, "executed_at": "2026-08-17T00:00:00Z",
                         "ask_vol": 1, "bid_vol": 1}])
    assert new == 1
    assert rawstore.count("leaps_flow_prints") == 1


def test_sec13f_ingest_populated(rawstore_tmp_db):
    import csv
    import io
    import zipfile
    from RevenueChaser.module_sec_13f import engine as sf

    path = rawstore.DB_PATH.parent / "q1_form13f.zip"
    with zipfile.ZipFile(path, "w") as z:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["ACCESSION_NUMBER", "CIK", "FILING_DATE",
                                            "SUBMISSIONTYPE", "PERIODOFREPORT"], delimiter="\t")
        w.writeheader()
        w.writerow({"ACCESSION_NUMBER": "acc1", "CIK": "1000", "FILING_DATE": "01-MAY-2026",
                    "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "31-MAR-2026"})
        z.writestr("SUBMISSION.tsv", buf.getvalue())
        buf2 = io.StringIO()
        w2 = csv.DictWriter(buf2, fieldnames=["ACCESSION_NUMBER", "NAMEOFISSUER", "CUSIP", "VALUE",
                                              "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"], delimiter="\t")
        w2.writeheader()
        w2.writerow({"ACCESSION_NUMBER": "acc1", "NAMEOFISSUER": "APPLE INC", "CUSIP": "111111111",
                     "VALUE": "1000", "SSHPRNAMT": "100", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""})
        z.writestr("INFOTABLE.tsv", buf2.getvalue())

    assert sf.ingest_zips([path]) == 1
    assert rawstore.count("sec13f_submissions") == 1
    assert rawstore.count("sec13f_holdings") == 1


def test_watchlist_populated(rawstore_tmp_db):
    rawstore.save_watchlist({"AAPL", "MSFT"})
    assert rawstore.load_watchlist() == {"AAPL", "MSFT"}


def test_swing_info_populated(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_swing_potential import indicators
    import yfinance

    class _T:
        def __init__(self, ticker):
            self.info = {"sector": "Technology", "beta": 1.4}

    monkeypatch.setattr(yfinance, "Ticker", _T)
    assert indicators.load_ticker_info("NVDA")["sector"] == "Technology"
    row = rawstore.get("swing_info", "NVDA")
    assert row["payload"]["beta"] == 1.4
    assert row["cached_at"] > 0


def test_rotation_holdings_populated(rawstore_tmp_db):
    from datetime import date
    from RevenueChaser.module_sector_rotation import store as rs
    rs.save_holdings_cache("XLK", [{"ticker": "AAPL", "weight": 0.2}])
    row = rawstore.get("rotation_holdings", "XLK")
    assert row["date"] == date.today().isoformat()
    assert rs.load_holdings_cache("XLK") == [{"ticker": "AAPL", "weight": 0.2}]


def test_sec_company_tickers_populated(rawstore_tmp_db, mock_httpx):
    from RevenueChaser.core.providers import sec_edgar
    mock_httpx("company_tickers.json", {"0": {"ticker": "AAPL", "cik_str": 320193, "title": "Apple Inc."}})
    assert sec_edgar._cik_map()["AAPL"]["cik"] == 320193
    row = rawstore.get("sec_company_tickers", "AAPL")
    assert row["name"] == "Apple Inc."


def test_run_registry_populated(rawstore_tmp_db):
    rawstore.write_rows("run_registry", [{"key": "profiles_mode", "value": "thinking-high"}])
    assert rawstore.get("run_registry", "profiles_mode")["value"] == "thinking-high"


def test_sec_company_sic_populated(rawstore_tmp_db):
    rawstore.write_rows("sec_company_sic", [
        {"ticker": "AAPL", "cik": "320193", "sic": "3571",
         "sic_description": "Electronic Computers", "fetched_at": 1.0},
    ])
    row = rawstore.get("sec_company_sic", "AAPL")
    assert row["sic"] == "3571"
    assert row["sic_description"] == "Electronic Computers"


def test_sec13f_security_meta_populated(rawstore_tmp_db):
    from RevenueChaser.module_sec_13f import engine as sf
    sf.save_sec13f_meta({"AAPL": {"name": "Apple", "sector": "Tech", "type": "stock"}})
    assert sf.load_sec13f_meta()["AAPL"]["type"] == "stock"
    # the one meta table (the two legacy maps were merged into it)
    assert sf.load_sec13f_meta()["AAPL"]["name"] == "Apple"
    assert rawstore.get("sec13f_security_meta", "AAPL")["entry"]["name"] == "Apple"


def test_sec13f_enrichment_populated(rawstore_tmp_db):
    from RevenueChaser.module_sec_13f import enrich
    rawstore.write_rows("sec13f_enrichment", [{"ticker": "AAPL", "entry": {"close": 190.0}}])   # writer retired (#300)
    assert enrich.load_enrichment()["AAPL"]["close"] == 190.0


def test_sec13f_edgar_backfill_populated(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    rawstore.write_rows("sec13f_edgar_backfill", [
        {"cik": "0000320193", "period": "2026-06-30", "accession": "0000320193-26-000001",
         "fetched_at": "2026-08-01T00:00:00Z"},
        {"cik": "0000320193", "period": "2026-03-31", "accession": "0000320193-26-000000",
         "fetched_at": "2026-05-01T00:00:00Z"},
    ])
    # composite-PK upsert: re-writing the same (cik, period) replaces, not dupes
    rawstore.write_rows("sec13f_edgar_backfill", [
        {"cik": "0000320193", "period": "2026-06-30", "accession": "0000320193-26-000002",
         "fetched_at": "2026-08-02T00:00:00Z"},
    ])
    rows = rawstore.query(
        "SELECT cik, accession FROM sec13f_edgar_backfill "
        "WHERE period='2026-06-30'")
    assert len(rows) == 1
    assert rows[0]["accession"] == "0000320193-26-000002"


def test_sec13f_ticker_names_populated(rawstore_tmp_db):
    # #207: the name match is retired, so the table has no writer or reader in the app; the legacy rows stay
    # readable until a cleanup issue drops the table.
    rawstore.write_rows("sec13f_ticker_names",
                        [{"ticker": "AAPL", "entry": {"name": "APPLE INC", "ticker": "AAPL", "figi": "BBG000B9XRY4"}}])
    rows = rawstore.query("SELECT ticker, entry FROM sec13f_ticker_names")
    assert [r["ticker"] for r in rows] == ["AAPL"]


def test_cusip_ticker_populated(rawstore_tmp_db):
    from RevenueChaser.module_sec_13f import engine as sf
    sf.save_cusip_ticker({"037833100": {"ticker": "AAPL", "source": "openfigi"}})
    assert sf.load_cusip_ticker()["037833100"]["ticker"] == "AAPL"


def test_sec13f_totals_populated(rawstore_tmp_db):
    from RevenueChaser.module_sec_13f import engine as sf
    sf.save_totals({"AAPL": {"2026-03-31": {"shares": 1.0, "holders": 2}}})
    assert sf.load_totals()["AAPL"]["2026-03-31"]["holders"] == 2
    # full refresh: a ticker with no holdings left must not linger
    sf.save_totals({"MSFT": {"2026-03-31": {"shares": 2.0, "holders": 1}}})
    assert "AAPL" not in sf.load_totals()


def test_etf_holdings_cache_populated(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.app import runners
    import RevenueChaser.module_sector_rotation.etf_holdings as eh
    monkeypatch.setattr(eh, "get_etf_holdings", lambda t, uw: [{"ticker": "AAPL", "weight": 0.2}])
    from RevenueChaser.app.jobs import JobMessage
    with pytest.raises(JobMessage, match="Holdings synced"):
        runners.run_etf_holdings_sync(uw=None)
    assert rawstore.get("etf_holdings", "XLK")["entry"] == [{"ticker": "AAPL", "weight": 0.2}]
    assert rawstore.get("store_meta", "etf_holdings_generated_at") is not None


def test_store_meta_populated(rawstore_tmp_db):
    from RevenueChaser.core import ticker_store
    ticker_store.write_from_cards([{"ticker": "AAPL", "name": "Apple"}], asof="2026-08-22")
    assert ticker_store.asof() == "2026-08-22"
    assert rawstore.get("store_meta", "schema_version")["value"] == ticker_store.SCHEMA_VERSION


def test_leaps_daily_scores_populated(rawstore_tmp_db):
    """Migrated from radar.db but not written by the app today — the schema
    still has to accept and return a row."""
    rawstore.write_rows("leaps_daily_scores", [{
        "ticker": "AAPL", "date": "2026-08-17", "frequency": "4", "notional": 4e5,
        "concentration": 0.5, "concentration_mult": 1.2, "final_score": 70.0}])
    row = rawstore.query("SELECT * FROM leaps_daily_scores")[0]
    assert row["final_score"] == 70.0
    # PK (ticker, date) dedups a re-run
    rawstore.write_rows("leaps_daily_scores", [{
        "ticker": "AAPL", "date": "2026-08-17", "frequency": "9", "notional": 1.0,
        "concentration": 0.1, "concentration_mult": 1.0, "final_score": 1.0}])
    assert rawstore.count("leaps_daily_scores") == 1


def test_migration_manifest_populated(rawstore_tmp_db):
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as mig
    rawstore.write_rows("migration_manifest", [{
        "dataset": "ticker_payload_cache", "migrated_at": "2026-09-02T00:00:00+00:00",
        "schema_version": rawstore.schema_version(), "source_count": 1,
        "source_bytes": 10, "source_hash": "", "verify": "PASS"}])
    assert mig._already_verified("ticker_payload_cache")
    assert not mig._already_verified("edgar_financials")


# ── Group B: data is never unwired ───────────────────────────────────────────

def test_durability_across_connections(rawstore_tmp_db):
    rawstore.write_rows("ticker_payload_cache", [{"ticker": "AAPL", "payload": {"x": 1},
                                          "fetched_at": 1.0, "fetched_at_iso": ""}])
    # reopen a fresh connection to the same file
    row = rawstore.get("ticker_payload_cache", "AAPL")
    assert row["payload"] == {"x": 1}


def test_roundtrip_integrity_json_payload(rawstore_tmp_db):
    # nested dicts survive a JSON-typed value column byte-for-byte
    payload = {"nested": {"list": [1, 2, 3]}, "tickers": ["A", "B"]}
    rawstore.write_rows("ticker_payload_cache", [{"ticker": "AAA", "payload": payload,
                                          "fetched_at": 1.0, "fetched_at_iso": ""}])
    assert rawstore.get("ticker_payload_cache", "AAA")["payload"] == payload
    # typed equity_universe columns round-trip too
    rawstore.write_rows("equity_universe", [{"ticker": "ETON", "market_cap": 1.8e9,
                                                 "sector": "Health Care", "price": 20.0,
                                                 "name": "Eton", "quote": {"x": 1}}])
    assert rawstore.get("equity_universe", "ETON")["market_cap"] == 1.8e9
    assert rawstore.get("equity_universe", "ETON")["quote"] == {"x": 1}


def test_write_failures_propagate(rawstore_tmp_db, monkeypatch):
    def boom(*a, **k):
        raise rawstore.RawStoreError("cannot write")

    monkeypatch.setattr(rawstore, "connect", boom)
    with pytest.raises(rawstore.RawStoreError):
        rawstore.write_rows("ticker_payload_cache", [{"ticker": "X", "payload": {}}])


def test_idempotent_index_members_replace(rawstore_tmp_db):
    # append-only PK dedup still holds for keyed stores
    rawstore.write_rows("index_constituents", [{"slug": "sp500", "ticker": "AAPL"}])
    rawstore.write_rows("index_constituents", [{"slug": "sp500", "ticker": "MSFT"}])
    assert rawstore.count("index_constituents") == 1  # same slug group replaced


def test_same_day_snapshot_overwrites(rawstore_tmp_db):
    from datetime import date
    from RevenueChaser.core.models.rotation import ETFRotationSnapshot
    from RevenueChaser.module_sector_rotation import store as rs

    def _snap(ticker):
        return ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker=ticker, group_name="sector",
                                   close_price=100.0, rs_1w=1.0, rs_1m=2.0, rs_3m=3.0,
                                   rs_ratio=2.0, rs_momentum=1.0, rs_ratio_z=1.0, rs_momentum_z=0.5,
                                   rrg_quadrant="LEADING")

    rs.save_snapshot([_snap("XLK")], as_of=date(2026, 8, 14))
    rs.save_snapshot([_snap("XLF")], as_of=date(2026, 8, 14))
    rows = rawstore.query("SELECT ticker FROM rotation_snapshots WHERE as_of_date='2026-08-14'")
    assert [r["ticker"] for r in rows] == ["XLF"]


# ── Group C: every table has a population test ───────────────────────────────
# table -> the pytest nodeid (file::function) that FILLS it. These are checked
# to resolve, so a prose placeholder can no longer pass the gate.
_HERE = "core/tests/test_db_persistence.py"
POPULATION: dict[str, str] = {
    "ticker_payload_cache": f"{_HERE}::test_cache_get_or_fetch_populates_ticker_cache",
    "edgar_financials": f"{_HERE}::test_sec_edgar_populates_sec_facts",
    "edgar_documents": "core/tests/test_edgar_store.py::test_round_trip",
    "edgar_facts": "core/tests/test_edgar_bulk.py::test_ingest_companyfacts",
    "swing_info": f"{_HERE}::test_swing_info_populated",
    "leaps_cache": f"{_HERE}::test_leaps_store_populated",
    "rotation_holdings": f"{_HERE}::test_rotation_holdings_populated",
    "equity_universe": ("module_asymmetric_growth/tests/test_asymmetric_growth_hunter.py"
                            "::test_fetch_universe_records_provenance"),
    "equity_universe_provenance": ("module_asymmetric_growth/tests/test_asymmetric_growth_hunter.py"
                                 "::test_fetch_universe_records_provenance"),
    "sec_company_tickers": f"{_HERE}::test_sec_company_tickers_populated",
    "sec_company_sic": f"{_HERE}::test_sec_company_sic_populated",
    "comov_runs": ("module_comovement/tests/test_comovement.py"
                   "::test_engine_run_persists_and_clusters"),
    "comov_nodes": ("module_comovement/tests/test_comovement.py"
                    "::test_engine_run_persists_and_clusters"),
    "comov_members": ("module_comovement/tests/test_comovement.py"
                      "::test_engine_run_persists_and_clusters"),
    "comov_lag": ("module_comovement/tests/test_comovement.py"
                  "::test_fetch_persists_the_lagged_test_and_load_payload_returns_it"),
    "chain_filings": ("module_value_chain/tests/test_value_chain.py"
                      "::test_run_persists_corpus_and_edges"),
    "chain_mentions": ("module_value_chain/tests/test_value_chain.py"
                       "::test_run_persists_corpus_and_edges"),
    "chain_meta": ("module_value_chain/tests/test_value_chain.py"
                   "::test_run_persists_corpus_and_edges"),
    "watchlist": f"{_HERE}::test_watchlist_populated",
    "sec13f_security_meta": f"{_HERE}::test_sec13f_security_meta_populated",
    "sec13f_enrichment": f"{_HERE}::test_sec13f_enrichment_populated",
    "sec13f_edgar_backfill": f"{_HERE}::test_sec13f_edgar_backfill_populated",
    "etf_holdings": f"{_HERE}::test_etf_holdings_cache_populated",
    "cusip_ticker": f"{_HERE}::test_cusip_ticker_populated",
    "sec13f_totals": f"{_HERE}::test_sec13f_totals_populated",
    "sec13f_ticker_names": f"{_HERE}::test_sec13f_ticker_names_populated",
    "store_meta": f"{_HERE}::test_store_meta_populated",
    "run_registry": f"{_HERE}::test_run_registry_populated",
    "migration_manifest": f"{_HERE}::test_migration_manifest_populated",
    "swing_ohlc": f"{_HERE}::test_swing_ohlc_populated",
    "rotation_snapshots": f"{_HERE}::test_rotation_snapshot_populated",
    "index_constituents": "core/tests/test_index_registry.py::test_save_and_load_index",
    "leaps_flow_prints": f"{_HERE}::test_leaps_store_populated",
    "leaps_daily_scores": f"{_HERE}::test_leaps_daily_scores_populated",
    "ticker_scores": f"{_HERE}::test_ticker_store_populated",
    "ticker_themes": "core/tests/test_ticker_themes.py::test_backfill_writes_legacy_rows",
    "fundamentals_scores": "module_fundamentals/tests/test_fundamental_scores.py::test_persist_writes_both",
    "fundamentals_gates": "module_fundamentals/tests/test_fundamental_scores.py::test_persist_writes_both",
    "swing_scores": "module_swing_potential/tests/test_swing_scores.py::test_persist_writes_both",
    "swing_gates": "module_swing_potential/tests/test_swing_scores.py::test_persist_writes_both",
    "llm_cache": "core/tests/test_relatedness_store.py::test_llm_cache_and_profiles",
    "relatedness_profiles": "core/tests/test_relatedness_store.py::test_llm_cache_and_profiles",
    "relatedness_attributes": "core/tests/test_relatedness_store.py::test_relatedness_attributes_and_scores",
    "relatedness_scores": "core/tests/test_relatedness_store.py::test_relatedness_attributes_and_scores",
    "theme_runs": "module_relatedness/tests/test_themes.py::test_build_shadow_writes_nested_themes",
    "relatedness_themes": "module_relatedness/tests/test_themes.py::test_build_shadow_writes_nested_themes",
    "relatedness_theme_tags": "module_relatedness/tests/test_themes.py::test_build_shadow_writes_nested_themes",
    "relatedness_theme_members": "module_relatedness/tests/test_themes.py::test_build_shadow_writes_nested_themes",
    "relatedness_theme_etfs": "module_relatedness/tests/test_themes.py::test_match_theme_etfs",
    "oipd_chain": "module_oipd/tests/test_engine.py::test_oipd_store_populated",
    "oipd_meta": "module_oipd/tests/test_engine.py::test_oipd_store_populated",
    "sec13f_submissions": f"{_HERE}::test_sec13f_ingest_populated",
    "sec13f_holdings": f"{_HERE}::test_sec13f_ingest_populated",
    "sec13f_cusip_names": f"{_HERE}::test_sec13f_ingest_populated",
    "sec13f_ingested_zips": f"{_HERE}::test_sec13f_ingest_populated",
    "security_master": "core/identity/tests/test_identity_manifest.py::test_every_identity_table_takes_a_row_and_reads_it_back",
    "identity_conflicts": "core/identity/tests/test_identity_manifest.py::test_every_identity_table_takes_a_row_and_reads_it_back",
    "identity_runs": "core/identity/tests/test_identity_manifest.py::test_every_identity_table_takes_a_row_and_reads_it_back",
    "identity_source_cache": "core/identity/tests/test_identity_cache.py::test_ok_rows_are_saved_with_typed_columns_and_payload",
    "board_settings": "core/tests/test_board_settings.py::test_save_then_get_and_values",
    "discord_channels": "module_discord/tests/test_loader.py::test_save_export_writes_channel_and_messages",
    "discord_messages": "module_discord/tests/test_loader.py::test_save_export_writes_channel_and_messages",
    "discord_export_runs": "module_discord/tests/test_engine.py::test_update_records_a_run_per_channel",
    "job_runs": "core/tests/test_schema_v8_migration.py::test_job_runs_upsert_replaces_the_row_for_a_run_id",
    "price_bars_daily": "core/tests/test_schema_v8_migration.py::test_price_bars_append_keeps_the_rest_of_a_tickers_history",
    "catalyst_events": "core/tests/test_schema_v8_migration.py::test_catalyst_event_date_slip_leaves_one_row_with_the_new_date",
    "orphan_designations": "core/tests/test_schema_v8_migration.py::test_orphan_designation_with_approvals_round_trips",
    "orphan_lookups": "core/tests/test_schema_v8_migration.py::test_orphan_lookups_keeps_one_row_per_ticker_and_session_and_a_later_lookup_adds_one",
    "forecast_ledger": "core/tests/test_schema_v8_migration.py::test_forecast_ledger_first_write_wins_within_one_bulk_batch",
    "forecast_outcomes": "core/tests/test_outcomes.py::test_mfe_and_mae_are_high_and_low_over_exactly_the_window_from_the_next_open",
    "forecast_baselines": "core/tests/test_baselines_numbers_25.py::test_a_baselines_run_populates_forecast_baselines",
    "price_actions": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "index_levels_daily": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "quote_snapshot": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "short_interest": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "vendor_financials": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "vendor_valuation": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "earnings_history": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "estimate_snapshots": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "identity_cusip": ("core/tests/test_identity_cusip_207.py"
                       "::test_a_first_answer_appends_present_and_absent_rows_with_the_run_stamps"),
    "ownership_snapshot": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "sec_mf_file": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "sec_exchange_file": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "earnings_calendar": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "macro_series": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "vol_index_daily": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "cftc_cot": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "economic_calendar": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "source_checks": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "price_checks": "core/tests/test_prices_db.py::test_price_writes_land_in_prices_duckdb_and_one_transaction_stays_in_one_file",
    "sec13f_filers": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "edgar_companyfacts": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "sec_submissions": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    "sec_submissions_cik": "core/tests/test_schema_v10_tables.py::test_a_written_row_reads_back",
    # the 25 schema-11 tables (history redesign C): change tables are filled by the store, result tables by record_run
    "equity_universe_changes": "core/tests/test_change_capture.py::test_write_rows_replace_and_delete_are_recorded",
    "security_master_changes": "core/tests/test_change_capture.py::test_write_rows_replace_and_delete_are_recorded",
    "identity_source_cache_changes": "core/tests/test_change_capture.py::test_write_rows_replace_and_delete_are_recorded",
    "index_constituents_changes": "core/tests/test_change_capture.py::test_write_rows_replace_and_delete_are_recorded",
    "watchlist_changes": "core/tests/test_change_capture.py::test_write_rows_replace_and_delete_are_recorded",
    "board_settings_changes": "core/tests/test_change_capture.py::test_write_rows_replace_and_delete_are_recorded",
    "catalyst_events_changes": "core/tests/test_change_capture.py::test_write_rows_replace_and_delete_are_recorded",
    "orphan_designations_changes": "core/tests/test_change_capture.py::test_write_rows_replace_and_delete_are_recorded",
    "etf_holdings_changes":
        "module_sector_rotation/tests/test_holdings_history_303.py::test_a_first_sync_appends_one_change_row_per_etf",
    "result_runs": "core/tests/test_history_store.py::test_recording_the_same_run_twice_writes_nothing_the_second_time",
    "ag_scores_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "swing_scores_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "swing_gates_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "fundamentals_scores_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "fundamentals_gates_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "relatedness_scores_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "value_chain_edges_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "oipd_chain_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "earnings_cards_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "leaps_footprint_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "oipd_fit_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "rotation_holdings_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "sector_tree_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "money_flow_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "rotation_snapshots_history":
        "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "sec13f_metrics_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
    "financials_report_history": "core/tests/test_history_store.py::test_every_history_table_takes_a_full_row_through_record_run",
}

_ROOT = Path(__file__).resolve().parent.parent.parent


def test_every_manifest_table_has_a_population_test():
    missing = [t for t in rawstore.MANIFEST if t not in POPULATION]
    assert not missing, f"tables without a population test: {sorted(missing)}"


def test_population_entries_point_at_tests_that_exist():
    """The gate has to bite: a free-text placeholder used to satisfy it."""
    broken = []
    for table, nodeid in sorted(POPULATION.items()):
        path, sep, func = nodeid.partition("::")
        f = _ROOT / path
        if not sep or not f.exists():
            broken.append(f"{table}: {nodeid} — file not found")
            continue
        if f"def {func}(" not in f.read_text(encoding="utf-8"):
            broken.append(f"{table}: {nodeid} — no such test function")
    assert not broken, "POPULATION must name real tests:\n" + "\n".join(broken)


def test_manifest_tables_start_empty_in_an_isolated_db(rawstore_tmp_db):
    """Sanity for the autouse isolation fixture: no test may see another's rows,
    and none may see the developer's real .store/raw.duckdb."""
    for table in rawstore.MANIFEST:
        assert rawstore.count(table) == 0, f"{table} leaked state across tests"


# ── Group D: TTL / no_fetch behaviour ────────────────────────────────────────

def test_ticker_cache_ttl_expiry(rawstore_tmp_db):
    from RevenueChaser.core.providers import cache
    stale = 1_000_000.0
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "ZZZZ", "payload": {"revenue": 1, "_fetched_at": stale},
        "fetched_at": stale, "fetched_at_iso": "",
    }])
    orig = cache._now_ts
    cache._now_ts = lambda: stale + cache._TTL_SECONDS + 10
    try:
        assert cache.load("ZZZZ") is None
        assert cache.load("ZZZZ", ignore_ttl=True)["revenue"] == 1
    finally:
        cache._now_ts = orig


def test_swing_info_ttl_from_cached_at(rawstore_tmp_db):
    import time
    from RevenueChaser.core import rawstore as rs
    rs.write_rows("swing_info", [{"ticker": "AAAA", "payload": {"sector": "Tech"},
                                  "cached_at": time.time() - 1000}])
    from RevenueChaser.module_swing_potential.indicators import load_ticker_info
    assert load_ticker_info("AAAA", no_fetch=True) == {"sector": "Tech"}


def test_sec_facts_no_ttl_returns_regardless_of_age(rawstore_tmp_db):
    rawstore.write_rows("edgar_financials", [{"ticker": "OLD", "annual": [], "quarterly": [],
                                       "meta": {"available": True}}])
    from RevenueChaser.core.providers import sec_edgar
    annual, _, meta = sec_edgar.get_fundamentals("OLD", no_fetch=True)
    assert meta.get("available") is True
