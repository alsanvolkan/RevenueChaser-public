"""Per-dataset migration round-trips — the part ``--purge`` bets the data on.

The migration verdict is only ``stored == len(rows)``. A migrator that drops or
mangles a field still reports PASS, and ``--delete``/``--purge`` then removes
the originals. So every dataset in the manifest needs a test that seeds a real
legacy source, migrates it, and asserts the CONTENT came back — not just the
count.

Two source shapes are covered here:
- file/dir sources handled by ``_rows_for`` (JSON and CSV);
- the composite SQLite DBs (``sec13f.db``, ``radar.db``) bulk-copied via
  DuckDB's ``sqlite_scan``, which nothing exercised before.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m


def _w(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def legacy(rawstore_tmp_db, tmp_path, monkeypatch):
    """An empty legacy ``rawData/`` tree the migrator will discover."""
    rd = tmp_path / "rawData"
    rd.mkdir()
    monkeypatch.setattr(m, "LEGACY_DATA_DIR", rd)
    return rd


def _migrate(rd, ds):
    sources = m.discover_sources(rd)
    assert sources.get(ds), f"{ds}: nothing discovered under {rd}"
    res = m.migrate_dataset(ds, sources[ds])
    assert res["status"] == "PASS", res
    return res


# ── file / directory sources ────────────────────────────────────────────────

class TestFileSourceMigrators:
    def test_ticker_cache(self, legacy):
        _w(legacy / "tickers" / "AAPL_raw.json",
           {"revenue": 100, "_fetched_at": 12.5, "_fetched_at_iso": "2026-01-01T00:00:00Z"})
        _migrate(legacy, "ticker_payload_cache")
        row = rawstore.get("ticker_payload_cache", "AAPL")
        assert row["payload"]["revenue"] == 100
        assert row["fetched_at"] == 12.5
        assert row["fetched_at_iso"] == "2026-01-01T00:00:00Z"

    def test_sec_facts(self, legacy):
        _w(legacy / "edgar_financials" / "AAPL.json",
           {"annual": [{"year": 2026, "revenue": 1}], "quarterly": [], "meta": {"available": True}})
        _migrate(legacy, "edgar_financials")
        row = rawstore.get("edgar_financials", "AAPL")
        assert row["annual"] == [{"year": 2026, "revenue": 1}]
        assert row["meta"] == {"available": True}

    def test_swing_info(self, legacy):
        _w(legacy / "swing_potential" / "info" / "NVDA.json", {"sector": "Tech", "beta": 1.7})
        _migrate(legacy, "swing_info")
        row = rawstore.get("swing_info", "NVDA")
        assert row["payload"] == {"sector": "Tech", "beta": 1.7}
        assert row["cached_at"] > 0, "freshness must be seeded from the source mtime"

    def test_swing_ohlc(self, legacy):
        d = legacy / "swing_potential" / "ohlc"
        d.mkdir(parents=True)
        (d / "MSFT.csv").write_text(
            "Date,Open,High,Low,Close,Volume\n"
            "2026-01-02,1.0,2.0,0.5,1.5,1000\n"
            "2026-01-03,1.5,2.5,1.0,2.0,2000\n", encoding="utf-8")
        _migrate(legacy, "swing_ohlc")
        rows = rawstore.query("SELECT * FROM swing_ohlc ORDER BY ts")
        assert len(rows) == 2
        assert rows[0]["ticker"] == "MSFT"
        assert (rows[0]["open"], rows[0]["high"], rows[0]["low"],
                rows[0]["close"], rows[0]["volume"]) == (1.0, 2.0, 0.5, 1.5, 1000.0)
        assert rows[1]["close"] == 2.0
        assert rows[0]["cached_at"] > 0

    def test_swing_ohlc_ticker_is_uppercased_like_its_reader(self, legacy):
        """``load_ohlc`` uppercases its argument, so a lowercase-named CSV must
        not migrate to a row nothing can look up."""
        d = legacy / "swing_potential" / "ohlc"
        d.mkdir(parents=True)
        (d / "msft.csv").write_text(
            "Date,Open,High,Low,Close,Volume\n2026-01-02,1.0,2.0,0.5,1.5,1000\n", encoding="utf-8")
        _migrate(legacy, "swing_ohlc")
        assert [r["ticker"] for r in rawstore.query("SELECT DISTINCT ticker FROM swing_ohlc")] == ["MSFT"]
        from RevenueChaser.module_swing_potential.indicators import load_ohlc
        assert len(load_ohlc("msft", no_fetch=True)) == 1
        assert len(load_ohlc("MSFT", no_fetch=True)) == 1

    def test_per_ticker_caches_are_keyed_uppercase(self, legacy):
        """Every per-ticker source file migrates under an uppercase key."""
        _w(legacy / "tickers" / "aapl_raw.json", {"revenue": 1})
        _w(legacy / "edgar_financials" / "msft.json", {"annual": [], "quarterly": [], "meta": {}})
        _w(legacy / "swing_potential" / "info" / "nvda.json", {"sector": "Tech"})
        _w(legacy / "leaps" / "cache" / "nok.json", {"latest": {}})
        _w(legacy / "sector_rotation" / "holdings" / "xlk.json", {"date": "", "rows": []})
        for ds, key in [("ticker_payload_cache", "AAPL"), ("edgar_financials", "MSFT"), ("swing_info", "NVDA"),
                        ("leaps_cache", "NOK"), ("rotation_holdings", "XLK")]:
            _migrate(legacy, ds)
            assert rawstore.get(ds, key) is not None, f"{ds} not keyed as {key}"

    def test_leaps_cache(self, legacy):
        _w(legacy / "leaps" / "cache" / "NOK.json",
           {"latest": {"label": "Quiet"}, "history": [{"label": "Quiet"}]})
        _migrate(legacy, "leaps_cache")
        assert rawstore.get("leaps_cache", "NOK")["payload"]["latest"]["label"] == "Quiet"

    def test_rotation_holdings(self, legacy):
        _w(legacy / "sector_rotation" / "holdings" / "XLK.json",
           {"date": "2026-08-14", "rows": [{"ticker": "AAPL", "weight": 0.2}]})
        _migrate(legacy, "rotation_holdings")
        row = rawstore.get("rotation_holdings", "XLK")
        assert row["date"] == "2026-08-14"
        assert row["rows"] == [{"ticker": "AAPL", "weight": 0.2}]

    def test_rotation_snapshots_unpacks_the_etf_cross_section(self, legacy):
        _w(legacy / "sector_rotation" / "snapshots" / "2026-08-14.json", {
            "as_of_date": "2026-08-14", "benchmark": "SPY",
            "generated_at": "2026-08-14T22:30:00",
            "etfs": [
                {"ticker": "XLK", "name": "Tech", "group": "sector", "close_price": 100.0,
                 "rs_1w": 1.0, "rs_1m": 2.0, "rs_3m": 3.0, "rs_ratio": 2.0,
                 "rs_momentum": 1.0, "rs_ratio_z": 1.0, "rs_momentum_z": 0.5,
                 "quadrant": "LEADING"},
                {"ticker": "XLF", "name": "Fin", "group": "sector", "close_price": 50.0,
                 "rs_1w": 0.0, "rs_1m": 0.0, "rs_3m": 0.0, "rs_ratio": 0.0,
                 "rs_momentum": 0.0, "rs_ratio_z": -1.0, "rs_momentum_z": -0.5,
                 "quadrant": "LAGGING"},
            ]})
        _migrate(legacy, "rotation_snapshots")
        rows = rawstore.query("SELECT * FROM rotation_snapshots ORDER BY ticker")
        assert [r["ticker"] for r in rows] == ["XLF", "XLK"]
        xlk = rows[1]
        assert xlk["as_of_date"] == "2026-08-14"
        assert xlk["benchmark"] == "SPY"
        assert xlk["generated_at"] == "2026-08-14T22:30:00"
        assert xlk["quadrant"] == "LEADING"
        # "group" is a SQL reserved word — a NULL here blanks the RRG sector filter
        assert xlk["group"] == "sector"

    def test_index_members(self, legacy):
        _w(legacy / "indexes" / "sp500.json", ["AAPL", "MSFT"])
        _w(legacy / "indexes" / "dow.json", ["AAPL"])
        _migrate(legacy, "index_constituents")
        from RevenueChaser.core import index_registry as ir
        assert ir.load_index("sp500") == {"AAPL", "MSFT"}
        assert ir.load_index("dow") == {"AAPL"}

    def test_asymmetric_universe_rows_and_meta_sentinel(self, legacy):
        _w(legacy / "asymmetric_growth_universe.json",
           [{"ticker": "eton", "market_cap": 1.8e9}, {"ticker": "NVDA", "market_cap": 3e12}])
        _w(legacy / "asymmetric_growth_universe_meta.json",
           {"generated_at": "2026-08-22T10:00:00", "refreshed_by": "Swing Potential"})
        _migrate(legacy, "equity_universe")
        _migrate(legacy, "equity_universe_provenance")
        row = rawstore.get("equity_universe", "ETON")
        assert row["market_cap"] == 1.8e9
        meta = rawstore.get("equity_universe_provenance", "__meta__")
        assert meta["generated_at"] == "2026-08-22T10:00:00"
        assert meta["refreshed_by"] == "Swing Potential"

    def test_sec_company_tickers(self, legacy):
        _w(legacy / "sec_company_tickers.json",
           {"AAPL": {"cik": "0000320193", "name": "Apple Inc."}})
        _migrate(legacy, "sec_company_tickers")
        row = rawstore.get("sec_company_tickers", "AAPL")
        assert row["cik"] == "0000320193" and row["name"] == "Apple Inc."

    def test_watchlist(self, legacy):
        _w(legacy / "watchlist_tickers.json", ["NOK", "AAPL"])
        _migrate(legacy, "watchlist")
        assert rawstore.load_watchlist() == {"NOK", "AAPL"}

    @pytest.mark.parametrize("ds,rel,payload,key,check", [
        ("sec13f_security_meta", "universe_meta.json",
         {"AAPL": {"name": "Apple", "sector": "Technology"}}, "AAPL", "name"),
        ("sec13f_enrichment", "sec_13f/enrichment.json",
         {"AAPL": {"close": 190.0, "float": 1.5e10}}, "AAPL", "close"),
        ("cusip_ticker", "sec_13f/cusip_ticker.json",
         {"037833100": {"ticker": "AAPL", "source": "openfigi"}}, "037833100", "ticker"),
        ("sec13f_totals", "sec_13f/totals.json",
         {"AAPL": {"2026-03-31": {"shares": 1.0, "holders": 2}}}, "AAPL", "2026-03-31"),
        ("sec13f_ticker_names", "sec_13f/ticker_name.json",
         {"AAPL": {"name": "APPLE INC", "figi": "BBG000B9XRY4"}}, "AAPL", "name"),
    ])
    def test_keyed_json_map_datasets(self, legacy, ds, rel, payload, key, check):
        _w(legacy / rel, payload)
        _migrate(legacy, ds)
        row = rawstore.get(ds, key)
        assert row is not None, f"{ds}: key {key} not stored"
        # the blob column differs per table ("entry" vs "payload"); writing the
        # wrong one stored NULL while the row count still matched -> false PASS
        blob = row[m._value_col(ds)]
        assert isinstance(blob, dict), f"{ds}: value column is {blob!r}, not the migrated object"
        assert check in blob, f"{ds}: {check} lost in migration"

    def test_etf_holdings_cache_unwraps_the_holdings_key(self, legacy):
        _w(legacy / "sector_rotation" / "etf_holdings.json",
           {"generated_at": "2026-08-14T10:00:00",
            "holdings": {"XLK": [{"ticker": "AAPL", "weight": 0.2}]}})
        _migrate(legacy, "etf_holdings")
        assert rawstore.get("etf_holdings", "XLK")["entry"] == [{"ticker": "AAPL", "weight": 0.2}]

    def test_ticker_names_are_migrated(self, legacy):
        """Regression: previously neither migrated nor salvaged, so --purge deleted it. #207 retired the 13F
        name match that read it; the migration still carries the legacy rows."""
        _w(legacy / "sec_13f" / "ticker_name.json",
           {"AAPL": {"name": "APPLE INC", "ticker": "AAPL", "figi": "BBG000B9XRY4"},
            "MSFT": {"name": "MICROSOFT CORP", "ticker": "MSFT"}})
        _migrate(legacy, "sec13f_ticker_names")
        rows = rawstore.query("SELECT ticker FROM sec13f_ticker_names ORDER BY ticker")
        assert [r["ticker"] for r in rows] == ["AAPL", "MSFT"]

    def test_ticker_store_carries_json_columns_and_legacy_meta(self, legacy):
        db = legacy / "ticker_store" / "tickers.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE tickers (ticker TEXT PRIMARY KEY, name TEXT, sector TEXT, "
                     "market_cap REAL, total_points INTEGER, themes TEXT, signals TEXT)")
        conn.execute("INSERT INTO tickers VALUES (?,?,?,?,?,?,?)",
                     ("ETON", "Eton", "Health Care", 1.8e9, 9, '["Biotech"]', "[]"))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('asof', '2026-08-22')")
        conn.commit()
        conn.close()

        _migrate(legacy, "ticker_scores")
        from RevenueChaser.core import ticker_store
        card = ticker_store.get("ETON")
        assert card["name"] == "Eton" and card["market_cap"] == 1.8e9
        assert card["themes"] == ["Biotech"] and card["signals"] == []
        assert ticker_store.asof() == "2026-08-22"


# ── composite SQLite sources (sqlite_scan bulk copy) ────────────────────────

def _sec13f_db(path, with_optional_cols=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE submissions(accession TEXT PRIMARY KEY, cik TEXT, period TEXT,"
                 " filing_date TEXT, type TEXT)")
    conn.execute("CREATE TABLE holdings(accession TEXT, cusip TEXT, cik TEXT, shares REAL, value REAL)")
    conn.execute("CREATE TABLE cusip_names(cusip TEXT PRIMARY KEY, name_norm TEXT, name_raw TEXT, count INTEGER)")
    conn.execute("CREATE TABLE ingested_zips(filename TEXT PRIMARY KEY, ingested_at TEXT)")
    conn.execute("INSERT INTO submissions VALUES ('acc1','1000','2026-03-31','01-MAY-2026','13F-HR')")
    conn.execute("INSERT INTO submissions VALUES ('acc2','2000','2026-03-31','01-MAY-2026','13F-HR')")
    # the same filing listing one CUSIP twice — must survive as two rows
    conn.executemany("INSERT INTO holdings VALUES (?,?,?,?,?)", [
        ("acc1", "111111111", "1000", 100.0, 1000.0),
        ("acc1", "111111111", "1000", 200.0, 2000.0),
        ("acc2", "111111111", "2000", 50.0, 500.0),
    ])
    conn.execute("INSERT INTO cusip_names VALUES ('111111111','APPLE INC','Apple Inc.',3)")
    conn.execute("INSERT INTO ingested_zips VALUES ('2026q2_form13f.zip','2026-08-01T00:00:00')")
    conn.commit()
    conn.close()
    return path


def _radar_db(path, with_optional_cols=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    extra = ", ask_vol REAL, bid_vol REAL" if with_optional_cols else ""
    conn.execute(f"CREATE TABLE flow_prints(id TEXT PRIMARY KEY, ticker TEXT, strike REAL,"
                 f" expiry TEXT, premium REAL, size INTEGER, option_type TEXT, delta REAL,"
                 f" next_earnings_date TEXT, executed_at TEXT, discovered_at TEXT{extra})")
    vals = ["p1", "AAPL", 200.0, "2027-01-15", 1000.0, 1, "call", 0.8, None,
            "2026-08-17T00:00:00Z", "2026-08-17"]
    if with_optional_cols:
        vals += [1.0, 2.0]
    conn.execute(f"INSERT INTO flow_prints VALUES ({','.join('?' * len(vals))})", vals)
    # legacy schema stored frequency as INTEGER; the manifest column is TEXT
    conn.execute("CREATE TABLE daily_scores(ticker TEXT, date TEXT, frequency INTEGER,"
                 " notional REAL, concentration REAL, concentration_mult REAL, final_score REAL,"
                 " PRIMARY KEY (ticker, date))")
    conn.execute("INSERT INTO daily_scores VALUES ('AAPL','2026-08-17',4,4e5,0.5,1.2,70.0)")
    conn.commit()
    conn.close()
    return path


class TestCompositeSqliteMigration:
    """``_bulk_copy_sqlite`` moves the two biggest, least replaceable datasets
    (16.6M-row 13F holdings, the LEAPS radar) — and had no coverage at all."""

    def test_sec13f_db_copies_every_table(self, legacy):
        _sec13f_db(legacy / "sec_13f" / "sec13f.db")
        res = _migrate(legacy, "sec13f")
        assert res["expected"] == res["stored"] == 2 + 3 + 1 + 1

        assert rawstore.count("sec13f_submissions") == 2
        assert rawstore.count("sec13f_holdings") == 3
        assert rawstore.get("sec13f_cusip_names", "111111111")["name_norm"] == "APPLE INC"
        assert rawstore.get("sec13f_ingested_zips", "2026q2_form13f.zip") is not None

        rows = rawstore.query("SELECT * FROM sec13f_holdings WHERE accession = 'acc1'")
        assert sorted(r["shares"] for r in rows) == [100.0, 200.0]

    def test_migrated_13f_store_computes_the_same_totals(self, legacy):
        _sec13f_db(legacy / "sec_13f" / "sec13f.db")
        _migrate(legacy, "sec13f")
        from RevenueChaser.module_sec_13f import engine as sf
        totals, _factors = sf.compute_totals({"111111111": "AAPL"})
        assert totals["AAPL"]["2026-03-31"]["shares"] == 350.0
        assert totals["AAPL"]["2026-03-31"]["holders"] == 2

    def test_sec13f_db_reimport_is_a_full_refresh_not_an_append(self, legacy):
        _sec13f_db(legacy / "sec_13f" / "sec13f.db")
        _migrate(legacy, "sec13f")
        _migrate(legacy, "sec13f")
        assert rawstore.count("sec13f_holdings") == 3, "re-running the migration duplicated rows"

    def test_radar_db_copies_prints_and_scores(self, legacy):
        _radar_db(legacy / "leaps" / "radar.db")
        res = _migrate(legacy, "leaps")
        assert res["expected"] == res["stored"] == 2
        row = rawstore.get("leaps_flow_prints", "p1")
        assert row["ticker"] == "AAPL" and row["premium"] == 1000.0
        assert row["ask_vol"] == 1.0 and row["bid_vol"] == 2.0
        scores = rawstore.query("SELECT * FROM leaps_daily_scores")
        assert scores[0]["ticker"] == "AAPL"
        assert str(scores[0]["frequency"]) == "4", "INTEGER -> TEXT column must survive the copy"

    def test_columns_missing_from_a_legacy_schema_become_null(self, legacy):
        """``flow_prints.ask_vol``/``bid_vol`` were added by an ALTER TABLE
        fixup; a DB predating it must still migrate, with NULLs."""
        _radar_db(legacy / "leaps" / "radar.db", with_optional_cols=False)
        _migrate(legacy, "leaps")
        row = rawstore.get("leaps_flow_prints", "p1")
        assert row["ticker"] == "AAPL"
        assert row["ask_vol"] is None and row["bid_vol"] is None

    def test_a_short_copy_is_reported_as_fail(self, legacy, monkeypatch):
        """The PASS verdict is what authorises ``--delete``; it has to be able
        to say FAIL."""
        _sec13f_db(legacy / "sec_13f" / "sec13f.db")
        real = m._bulk_copy_sqlite
        monkeypatch.setattr(m, "_bulk_copy_sqlite",
                            lambda db, tables: (real(db, tables)[0] + 1, real(db, tables)[1]))
        sources = m.discover_sources(legacy)
        assert m.migrate_dataset("sec13f", sources["sec13f"])["status"] == "FAIL"


class TestEveryManifestDatasetHasARoundTripTest:
    """Gate: a new manifest table must arrive with a migration test, or the
    ``--purge`` verdict for it is unverified."""

    def test_no_dataset_is_silently_unmigratable(self, legacy):
        # datasets written only by the app itself, never present as a legacy source
        app_only = {"store_meta", "migration_manifest", "sec_company_sic",
                    "comov_runs", "comov_nodes", "comov_members", "comov_lag",
                    "chain_filings", "chain_mentions", "chain_meta", "run_registry",
                    "ticker_themes", "fundamentals_scores", "fundamentals_gates",
                    "swing_scores", "swing_gates", "edgar_facts", "edgar_documents",
                    "llm_cache", "relatedness_profiles", "relatedness_attributes",
                    "relatedness_scores", "theme_runs", "relatedness_themes",
                    "relatedness_theme_tags", "relatedness_theme_members",
                    "relatedness_theme_etfs",
                    "oipd_chain", "oipd_meta",
                    "security_master", "identity_conflicts", "identity_source_cache",
                    "identity_runs", "board_settings",
                    "discord_channels", "discord_messages", "discord_export_runs",
                    "job_runs", "price_bars_daily", "catalyst_events",
                    "orphan_designations", "orphan_lookups", "forecast_ledger",
                    # the 22 G1 tables of #292 (schema 10): no legacy source, filled only by their writers
                    "price_actions", "index_levels_daily", "quote_snapshot", "short_interest",
                    "vendor_financials", "vendor_valuation", "earnings_history", "estimate_snapshots",
                    "identity_cusip", "ownership_snapshot", "sec_mf_file", "sec_exchange_file",
                    "earnings_calendar", "macro_series", "vol_index_daily", "cftc_cot", "economic_calendar",
                    "source_checks", "sec13f_filers", "edgar_companyfacts", "sec_submissions",
                    "sec_submissions_cik",
                    # the price watermarks in prices.duckdb (#156): filled only by core/prices
                    "price_checks",
                    # the 25 schema-11 tables (history redesign C): filled by the store and record_run only
                    "equity_universe_changes", "security_master_changes", "identity_source_cache_changes",
                    "index_constituents_changes", "watchlist_changes", "board_settings_changes",
                    "catalyst_events_changes", "orphan_designations_changes", "result_runs",
                    "ag_scores_history", "swing_scores_history", "swing_gates_history",
                    "fundamentals_scores_history", "fundamentals_gates_history", "relatedness_scores_history",
                    "value_chain_edges_history", "oipd_chain_history", "earnings_cards_history",
                    "leaps_footprint_history", "oipd_fit_history", "rotation_holdings_history",
                    "sector_tree_history", "money_flow_history", "sec13f_metrics_history",
                    "financials_report_history",
                    # #303: the change history of etf_holdings, filled by the store
                    "etf_holdings_changes",
                    # #374: the RRG points of a live Rotation Fetch, filled by record_run (red on main 0a75e3b)
                    "rotation_snapshots_history",
                    # #380 S0: derived from forecast_ledger and the bars by core/outcomes.py
                    "forecast_outcomes"}
        # composite SQLite targets are covered through their source dataset
        composite_targets = {t for _rel, tables in m.COMPOSITE_SQLITE.values() for _src, t in tables}
        for ds in rawstore.MANIFEST:
            if ds in app_only or ds in composite_targets:
                continue
            try:
                m._rows_for(ds, [])
            except ValueError as exc:
                pytest.fail(f"{ds}: no migrator in _rows_for ({exc})")
            except Exception:  # noqa: BLE001 - migrator exists; empty input may throw
                pass
