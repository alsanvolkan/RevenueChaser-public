"""Storage-layer fixes: reset scope, schema-on-first-use, compressed columns,
bulk writes, sessions, peer-aware lock budget, atomic multi-table writes,
compaction. Each class names the defect it guards against.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time

import duckdb
import pytest

from RevenueChaser.core import rawstore


def _raw_conn(path):
    return duckdb.connect(str(path))


# ── reset_db must never delete the live edgar.duckdb ─────────────────────────
class TestResetScope:
    def test_raw_only_reset_keeps_the_sibling_edgar_file(self, rawstore_tmp_db, monkeypatch):
        rawstore.write_rows("edgar_documents", [{"accession": "A1", "cik": "1", "form": "10-K"}])
        # exactly what `migrate --reset` does: point DB_PATH at raw.duckdb.new
        monkeypatch.setattr(rawstore, "DB_PATH", rawstore_tmp_db.with_name("raw.duckdb.new"))
        rawstore.reset_db(include_edgar=False)
        monkeypatch.setattr(rawstore, "DB_PATH", rawstore_tmp_db)
        assert rawstore.count("edgar_documents") == 1

    def test_default_reset_still_recreates_both_files(self, rawstore_tmp_db):
        rawstore.write_rows("edgar_documents", [{"accession": "A1", "cik": "1", "form": "10-K"}])
        rawstore.write_rows("watchlist", [{"ticker": "AAPL"}])
        rawstore.reset_db()
        assert rawstore.count("edgar_documents") == 0
        assert rawstore.count("watchlist") == 0

    def test_migrate_reset_passes_include_edgar_false(self):
        from pathlib import Path
        src = (Path(rawstore.__file__).parent.parent / "scripts"
               / "migrate_rawdata_to_duckdb.py").read_text(encoding="utf-8")
        assert "reset_db(include_edgar=False)" in src


# ── moving EDGAR tables out of raw.duckdb must not double-encode JSON ────────
class TestEdgarMove:
    def test_json_columns_survive_the_move_as_objects(self, isolate_rawstore):
        with _raw_conn(isolate_rawstore) as c:
            c.execute("CREATE TABLE edgar_financials (ticker TEXT PRIMARY KEY, "
                      "annual JSON, quarterly JSON, meta JSON)")
            c.execute("""INSERT INTO edgar_financials VALUES
                         ('MSFT', '[{"revenue": 1.5}]', '[]', '{"cik": "789"}')""")
        assert rawstore._migrate_edgar_tables() == ["edgar_financials"]
        row = rawstore.get("edgar_financials", "MSFT")
        assert row["annual"] == [{"revenue": 1.5}]
        assert row["quarterly"] == []
        assert row["meta"] == {"cik": "789"}


# ── large text/JSON cells are stored compressed, transparently ───────────────
class TestCompressedColumns:
    ANNUAL = [{"fiscal_year": str(y), "revenue": 1e9 + y, "ebit": None} for y in range(2000, 2026)]

    def test_round_trip_through_get_and_get_all(self, rawstore_tmp_db):
        rawstore.write_rows("edgar_financials", [{
            "ticker": "AAPL", "annual": self.ANNUAL, "quarterly": [], "meta": {"is_us": True}}])
        row = rawstore.get("edgar_financials", "AAPL")
        assert row["annual"] == self.ANNUAL and row["quarterly"] == []
        assert row["meta"] == {"is_us": True}
        assert rawstore.get_all("edgar_financials")[0]["annual"] == self.ANNUAL

    def test_cells_are_stored_as_smaller_blobs(self, rawstore_tmp_db):
        text = "The Company designs semiconductors. " * 2000
        rawstore.write_rows("chain_filings", [{"ticker": "NVDA", "item_text": text}])
        raw = rawstore.query("SELECT item_text, typeof(item_text) AS t FROM chain_filings",
                             db="edgar")[0]
        assert raw["t"] == "BLOB"
        assert len(raw["item_text"]) < len(text) / 10
        assert rawstore.get("chain_filings", "NVDA")["item_text"] == text

    def test_legacy_text_table_is_rebuilt_and_double_encoding_undone(self, isolate_rawstore):
        """The live store: JSON columns whose every cell is a JSON *string*."""
        edgar = rawstore.edgar_db_path()
        double = json.dumps(json.dumps(self.ANNUAL))
        with _raw_conn(edgar) as c:
            c.execute("CREATE TABLE edgar_financials (ticker TEXT PRIMARY KEY, "
                      "annual JSON, quarterly JSON, meta JSON)")
            c.execute("INSERT INTO edgar_financials VALUES (?, ?, '[]', '{}')", ["A", double])
        # readable (and repaired) even before the rebuild
        assert rawstore.get("edgar_financials", "A")["annual"] == self.ANNUAL
        rawstore.ensure_all_tables()
        t = rawstore.query("SELECT typeof(annual) AS t FROM edgar_financials", db="edgar")
        assert t == [{"t": "BLOB"}]
        assert rawstore.get("edgar_financials", "A")["annual"] == self.ANNUAL

    def test_a_write_rebuilds_a_legacy_table_first(self, isolate_rawstore):
        with _raw_conn(rawstore.edgar_db_path()) as c:
            c.execute("CREATE TABLE chain_filings (ticker TEXT PRIMARY KEY, cik TEXT, form TEXT, "
                      "filed TEXT, accession TEXT, primary_doc TEXT, item_text TEXT, "
                      "quality TEXT, fetched_at DOUBLE)")
            c.execute("INSERT INTO chain_filings (ticker, item_text) VALUES ('OLD', 'old text')")
        rawstore.write_rows("chain_filings", [{"ticker": "NEW", "item_text": "new text"}])
        got = {r["ticker"]: r["item_text"] for r in rawstore.get_all("chain_filings")}
        assert got == {"OLD": "old text", "NEW": "new text"}

    def test_get_all_can_skip_the_heavy_column(self, rawstore_tmp_db):
        rawstore.write_rows("chain_filings", [{"ticker": "X", "filed": "2026-01-01",
                                               "item_text": "long"}])
        assert rawstore.get_all("chain_filings", columns=["ticker", "filed"]) == [
            {"ticker": "X", "filed": "2026-01-01"}]
        with pytest.raises(rawstore.RawStoreError):
            rawstore.get_all("chain_filings", columns=["nope"])


# ── the manifest is applied on a process's first store call ──────────────────
class TestAutoEnsure:
    @pytest.fixture
    def auto(self, monkeypatch):
        monkeypatch.setenv("RC_STORE_AUTO_ENSURE", "1")
        monkeypatch.setattr(rawstore, "_ENSURED", {})

    def test_views_exist_on_a_store_built_by_plain_writes(self, rawstore_tmp_db, auto):
        rawstore.write_rows("equity_universe", [{"ticker": "AAPL", "market_cap": 3e12}])
        rows = rawstore.query("SELECT ticker, market_cap FROM company_profile")
        assert rows == [{"ticker": "AAPL", "market_cap": 3e12}]

    def test_a_read_of_a_missing_store_does_not_create_it(self, isolate_rawstore, auto):
        assert rawstore.query("SELECT 1 FROM company_profile") == []
        assert not isolate_rawstore.exists()

    def test_it_runs_once_per_process(self, rawstore_tmp_db, auto, monkeypatch):
        calls = []
        real = rawstore.ensure_all_tables
        monkeypatch.setattr(rawstore, "ensure_all_tables",
                            lambda: (calls.append(1), real())[1])
        for _ in range(3):
            rawstore.write_rows("watchlist", [{"ticker": "A"}])
            rawstore.query("SELECT * FROM watchlist")
        assert len(calls) == 1

    def test_a_failure_is_logged_not_raised_and_retried_later(self, rawstore_tmp_db, auto,
                                                              monkeypatch):
        def boom():
            raise rawstore.RawStoreError("locked")
        monkeypatch.setattr(rawstore, "ensure_all_tables", boom)
        rawstore.write_rows("watchlist", [{"ticker": "A"}])  # does not raise
        assert rawstore._ENSURED[str(rawstore.DB_PATH)] > 0  # failure time recorded


class TestRenameTrap:
    def test_an_empty_new_table_does_not_block_the_rename(self, isolate_rawstore):
        with _raw_conn(isolate_rawstore) as c:
            c.execute("CREATE TABLE ticker_cache (ticker TEXT PRIMARY KEY, payload JSON, "
                      "fetched_at DOUBLE, fetched_at_iso TEXT)")
            c.execute("""INSERT INTO ticker_cache VALUES ('AAPL', '{"a": 1}', 1.0, '')""")
            # a write that self-healed the NEW name into existence first
            c.execute("CREATE TABLE ticker_payload_cache (ticker TEXT PRIMARY KEY, "
                      "payload JSON, fetched_at DOUBLE, fetched_at_iso TEXT)")
        rawstore.ensure_all_tables()
        assert rawstore.get("ticker_payload_cache", "AAPL")["payload"] == {"a": 1}

    def test_two_populated_tables_are_left_alone(self, isolate_rawstore):
        with _raw_conn(isolate_rawstore) as c:
            c.execute("CREATE TABLE ticker_cache (ticker TEXT PRIMARY KEY)")
            c.execute("INSERT INTO ticker_cache VALUES ('OLD')")
            c.execute("CREATE TABLE ticker_payload_cache (ticker TEXT PRIMARY KEY, "
                      "payload JSON, fetched_at DOUBLE, fetched_at_iso TEXT)")
            c.execute("INSERT INTO ticker_payload_cache (ticker) VALUES ('NEW')")
        rawstore.ensure_all_tables()
        assert [r["ticker"] for r in rawstore.get_all("ticker_payload_cache")] == ["NEW"]
        assert rawstore.query("SELECT ticker FROM ticker_cache") == [{"ticker": "OLD"}]


# ── "replace" clears the whole group, not just the keys in the new batch ─────
class TestReplaceGroups:
    def test_a_rerun_drops_clusters_that_disappeared(self, rawstore_tmp_db):
        rows = lambda ids: [{"as_of": "D", "cut": 20, "cluster_id": c} for c in ids]
        rawstore.write_rows("comov_nodes", rows("abc"))
        rawstore.write_rows("comov_nodes", rows("ax"))
        got = rawstore.query("SELECT cluster_id FROM comov_nodes ORDER BY 1")
        assert [r["cluster_id"] for r in got] == ["a", "x"]

    def test_other_runs_are_untouched(self, rawstore_tmp_db):
        rawstore.write_rows("comov_nodes", [{"as_of": "D1", "cut": 20, "cluster_id": "k"}])
        rawstore.write_rows("comov_nodes", [{"as_of": "D2", "cut": 20, "cluster_id": "z"}])
        assert rawstore.count("comov_nodes") == 2

    def test_a_profile_rewrite_drops_old_fields(self, rawstore_tmp_db):
        rawstore.write_rows("relatedness_attributes", [
            {"ticker": "A", "field": f, "value": "[]"} for f in ("x", "y")])
        rawstore.write_rows("relatedness_attributes", [{"ticker": "A", "field": "x", "value": "[]"}])
        assert rawstore.count("relatedness_attributes") == 1


# ── bulk writes store exactly what row-by-row writes stored ──────────────────
def _mixed_rows(n):
    rows = []
    for i in range(n):
        rows.append({
            "ticker": f"T{i:04d}", "name": None if i % 7 == 0 else f"Name {i}",
            "price": [1.5, 3, None, float("inf"), 1e-12, 123456789.123456789][i % 6],
            "market_cap": float(i) * 1e9, "total_points": [3, 3.7, True, None][i % 4],
            "dilution_warning": [True, False, None][i % 3], "us_listed": 1,
            "earnings_date": None, "themes": ["AI", "Cloud"] if i % 2 else None,
            "signals": {"s": i}, "source": i if i % 5 == 0 else "screener",
        })
    return rows


def _table_rows(name):
    out = []
    for r in sorted(rawstore.get_all(name), key=lambda r: json.dumps(
            [r.get(k) for k in rawstore.MANIFEST[name]["key"]], default=str)):
        out.append({k: ("nan" if isinstance(v, float) and math.isnan(v) else v)
                    for k, v in r.items()})
    return out


class TestBulkParity:
    @pytest.mark.parametrize("mode", ["write_rows", "replace_all"])
    def test_same_contents_as_row_by_row(self, rawstore_tmp_db, monkeypatch, mode):
        rows = _mixed_rows(150)
        getattr(rawstore, mode)("ticker_scores", rows)  # bulk (150 >= 64)
        bulk = _table_rows("ticker_scores")
        rawstore.clear("ticker_scores")
        monkeypatch.setattr(rawstore, "_BULK_MIN_ROWS", 10**9)
        getattr(rawstore, mode)("ticker_scores", rows)  # executemany
        assert _table_rows("ticker_scores") == bulk

    def test_json_column_round_trips(self, rawstore_tmp_db):
        rows = [{"ticker": f"T{i}", "quote": {"p": [["2026-08-01", 100.0]], "i": i}}
                for i in range(100)]
        rawstore.write_rows("equity_universe", rows)
        assert rawstore.get("equity_universe", "T7")["quote"] == {"p": [["2026-08-01", 100.0]], "i": 7}

    def test_upsert_duplicate_keys_last_wins(self, rawstore_tmp_db):
        rows = [{"ticker": "DUP", "market_cap": float(i)} for i in range(100)]
        rawstore.write_rows("equity_universe", rows)
        assert rawstore.get("equity_universe", "DUP")["market_cap"] == 99.0

    def test_append_duplicate_keys_first_wins(self, rawstore_tmp_db):
        rows = [{"id": "P1", "ticker": "AAPL", "premium": float(i)} for i in range(100)]
        rawstore.write_rows("leaps_flow_prints", rows)
        assert rawstore.get("leaps_flow_prints", "P1")["premium"] == 0.0

    def test_replace_with_many_groups(self, rawstore_tmp_db):
        old = [{"ticker": f"T{i}", "ts": "2026-01-01", "close": 1.0} for i in range(100)]
        new = [{"ticker": f"T{i}", "ts": "2026-02-01", "close": 2.0} for i in range(100)]
        rawstore.write_rows("swing_ohlc", old)
        rawstore.write_rows("swing_ohlc", new)
        assert rawstore.query("SELECT DISTINCT ts FROM swing_ohlc") == [{"ts": "2026-02-01"}]

    def test_bulk_update_columns(self, rawstore_tmp_db):
        rawstore.write_rows("equity_universe", [
            {"ticker": f"T{i}", "name": f"N{i}", "market_cap": 1.0} for i in range(100)])
        rawstore.update_columns("equity_universe", "ticker",
                                [{"ticker": f"T{i}", "market_cap": float(i)} for i in range(100)]
                                + [{"ticker": "MISSING", "market_cap": 5.0}], ["market_cap"])
        row = rawstore.get("equity_universe", "T42")
        assert row["market_cap"] == 42.0 and row["name"] == "N42"
        assert rawstore.count("equity_universe") == 100

    def test_a_bad_value_in_a_bulk_batch_still_rolls_back(self, rawstore_tmp_db):
        good = [{"ticker": "AAPL", "ts": f"2026-01-{i:02d}", "close": 1.0} for i in range(1, 29)]
        rawstore.write_rows("swing_ohlc", good)
        bad = [{"ticker": "AAPL", "ts": f"2026-03-{i:03d}", "close": 2.0} for i in range(100)]
        bad[50]["close"] = "not-a-number"
        with pytest.raises(duckdb.Error):
            rawstore.write_rows("swing_ohlc", bad)
        assert rawstore.count("swing_ohlc") == 28


# ── one handle per burst ─────────────────────────────────────────────────────
class TestSession:
    def test_a_session_opens_the_file_once(self, rawstore_tmp_db, monkeypatch):
        rawstore.write_rows("watchlist", [{"ticker": "A"}])
        opened = []
        real = duckdb.connect
        monkeypatch.setattr(duckdb, "connect", lambda *a, **k: (opened.append(a), real(*a, **k))[1])
        with rawstore.session():
            for _ in range(10):
                rawstore.load_watchlist()
            rawstore.write_rows("watchlist", [{"ticker": "B"}])
        assert len(opened) == 1
        assert rawstore.load_watchlist() == {"A", "B"}

    def test_the_handle_is_closed_after_the_block(self, rawstore_tmp_db):
        rawstore.write_rows("watchlist", [{"ticker": "A"}])
        with rawstore.session():
            rawstore.load_watchlist()
        # another PROCESS can open the file again once the session ended
        code = subprocess.run([sys.executable, "-c",
                               f"import duckdb; duckdb.connect(r'{rawstore_tmp_db}').close()"],
                              capture_output=True, timeout=60).returncode
        assert code == 0

    def test_a_failed_write_inside_a_session_rolls_back(self, rawstore_tmp_db):
        rawstore.write_rows("swing_ohlc", [{"ticker": "A", "ts": "1", "close": 1.0}])
        with rawstore.session():
            with pytest.raises(duckdb.Error):
                rawstore.write_rows("swing_ohlc", [{"ticker": "A", "ts": "2", "close": "x"}])
            assert rawstore.count("swing_ohlc") == 1


# ── another RevenueChaser process holding the lock is waited out ─────────────
WINDOWS_LOCK = ("IO Error: Cannot open file \"raw.duckdb\": The process cannot access "
                "the file because it is being used by another process.")


class TestPeerLockBudget:
    @pytest.fixture
    def peer(self, rawstore_tmp_db):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        f = rawstore._presence_file(rawstore_tmp_db.parent, proc.pid)
        f.write_text(str(proc.pid))
        yield proc
        proc.kill()
        proc.wait()

    def _locked(self, monkeypatch):
        def _raise(*a, **k):
            raise duckdb.IOException(WINDOWS_LOCK)
        monkeypatch.setattr(duckdb, "connect", _raise)
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", slept.append)
        return slept

    def test_a_peer_gets_the_full_budget_even_for_a_write(self, rawstore_tmp_db, peer, monkeypatch):
        slept = self._locked(monkeypatch)
        with pytest.raises(rawstore.StoreLockedError):
            rawstore._connect_with_retry(rawstore._WRITE_DELAYS)
        assert sum(slept) > 20.0

    def test_an_unknown_holder_still_fails_fast(self, rawstore_tmp_db, monkeypatch):
        slept = self._locked(monkeypatch)
        with pytest.raises(rawstore.StoreLockedError):
            rawstore._connect_with_retry(rawstore._READ_DELAYS)
        assert sum(slept) < 1.0

    def test_a_dead_peers_presence_file_is_ignored_and_removed(self, rawstore_tmp_db, monkeypatch):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        f = rawstore._presence_file(rawstore_tmp_db.parent, proc.pid)
        f.write_text(str(proc.pid))
        assert rawstore._peer_pids(rawstore_tmp_db) == []
        assert not f.exists()

    def test_opening_the_store_registers_this_process(self, rawstore_tmp_db):
        rawstore.write_rows("watchlist", [{"ticker": "A"}])
        assert rawstore._presence_file(rawstore_tmp_db.parent, os.getpid()).exists()


# ── several tables in one retried transaction ────────────────────────────────
class TestAtomic:
    def test_a_failure_leaves_every_table_as_it_was(self, rawstore_tmp_db):
        rawstore.write_rows("comov_runs", [{"as_of": "D", "n_tickers": 1}])
        rawstore.write_rows("comov_nodes", [{"as_of": "D", "cut": 20, "cluster_id": "a"}])

        def fn(tx):
            tx.ensure_table("comov_nodes")
            tx.execute("DELETE FROM comov_nodes WHERE as_of = ?", ["D"])
            tx.write_rows("comov_runs", [{"as_of": "D", "n_tickers": 2}])
            raise RuntimeError("members failed")

        with pytest.raises(RuntimeError):
            rawstore.atomic(fn)
        assert rawstore.get("comov_runs", "D")["n_tickers"] == 1
        assert rawstore.count("comov_nodes") == 1

    def test_returns_the_block_result(self, rawstore_tmp_db):
        assert rawstore.atomic(lambda tx: tx.write_rows("watchlist", [{"ticker": "A"}])) == 1


# ── compaction ───────────────────────────────────────────────────────────────
class TestCompact:
    def test_shrinks_after_deletes_and_keeps_every_row(self, rawstore_tmp_db):
        rawstore.ensure_all_tables()
        rawstore.write_rows("swing_ohlc", [{"ticker": f"T{i % 50}", "ts": f"{i}", "close": 1.0,
                                            "open": 1.0, "high": 1.0, "low": 1.0}
                                           for i in range(200_000)])
        rawstore.execute("DELETE FROM swing_ohlc WHERE ticker <> 'T1'")
        rawstore.write_rows("watchlist", [{"ticker": "KEEP"}])
        with _raw_conn(rawstore_tmp_db) as c:
            c.execute("CHECKPOINT")
        before = rawstore_tmp_db.stat().st_size
        r = rawstore.compact("raw")
        assert r["after_bytes"] < before
        assert rawstore.count("swing_ohlc") == 4000
        assert rawstore.load_watchlist() == {"KEEP"}
        assert rawstore.query("SELECT count(*) AS n FROM company_profile")  # views copied
        assert (rawstore_tmp_db.with_name("raw.duckdb.pre-compact")).exists()

    def test_refuses_while_a_peer_uses_the_store(self, rawstore_tmp_db):
        rawstore.write_rows("watchlist", [{"ticker": "A"}])
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            rawstore._presence_file(rawstore_tmp_db.parent, proc.pid).write_text(str(proc.pid))
            with pytest.raises(rawstore.RawStoreError, match="in use"):
                rawstore.compact("raw")
        finally:
            proc.kill()
            proc.wait()


# ── #202: edgar_financials.meta is a JSON column that legacy rows hold double-encoded ──
class TestDoubleEncodedJsonColumn:
    def _legacy(self, edgar_path, meta_cell: str):
        with _raw_conn(edgar_path) as c:
            c.execute("CREATE TABLE edgar_financials (ticker TEXT PRIMARY KEY, "
                      "annual JSON, quarterly JSON, meta JSON)")
            c.execute("INSERT INTO edgar_financials VALUES ('AAPL', '[]', '[]', ?)", [meta_cell])

    def test_writer_stores_meta_as_a_single_encoded_object(self, rawstore_tmp_db):
        rawstore.write_rows("edgar_financials", [{
            "ticker": "AAPL", "annual": [], "quarterly": [], "meta": {"is_us": True}}])
        raw = rawstore.query("SELECT json_type(meta) AS t FROM edgar_financials", db="edgar")
        assert raw == [{"t": "OBJECT"}]
        assert rawstore.get("edgar_financials", "AAPL")["meta"] == {"is_us": True}

    def test_reader_returns_a_dict_for_a_double_encoded_meta(self, isolate_rawstore):
        double = json.dumps(json.dumps({"is_us": True, "entity_name": "Apple"}))
        self._legacy(rawstore.edgar_db_path(), double)
        assert rawstore.get("edgar_financials", "AAPL")["meta"] == {
            "is_us": True, "entity_name": "Apple"}
        assert rawstore.get_all("edgar_financials")[0]["meta"]["is_us"] is True

    def test_a_plain_string_in_a_json_column_stays_a_string(self, isolate_rawstore):
        # only a string that itself parses to an object/array is decoded a second time
        self._legacy(rawstore.edgar_db_path(), json.dumps("just text"))
        assert rawstore.get("edgar_financials", "AAPL")["meta"] == "just text"
        rawstore.write_rows("edgar_financials", [{
            "ticker": "BBB", "annual": [], "quarterly": [], "meta": "[not json"}])
        assert rawstore.get("edgar_financials", "BBB")["meta"] == "[not json"

    def test_text_verbatim_tables_are_never_reparsed(self, rawstore_tmp_db):
        spec_tables = [n for n, s in rawstore.MANIFEST.items() if s.get("text_verbatim")]
        assert spec_tables, "manifest lost its text_verbatim tables"
        name = spec_tables[0]
        spec = rawstore.MANIFEST[name]
        col = next(c for c in rawstore._columns_of(spec)
                   if rawstore._col_type(name, c).split("(", 1)[0] == "TEXT"
                   and c not in spec["key"])
        row = {c: None for c in rawstore._columns_of(spec)}
        text = json.dumps(json.dumps({"a": 1}))
        row[col] = text
        assert rawstore._decode_row(name, dict(row))[col] == text


class TestDoubleDecodeNarrowness:
    """#202 verification: the second decode is narrow (reader in ``_decode_row``)."""

    def _cell(self, table, col, text):
        spec = rawstore.MANIFEST[table]
        row = {c: None for c in rawstore._columns_of(spec)}
        row[col] = text
        return rawstore._decode_row(table, row)[col]

    def test_a_double_encoded_list_is_decoded(self):
        double = json.dumps(json.dumps([{"a": 1}, 2]))
        assert self._cell("edgar_financials", "meta", double) == [{"a": 1}, 2]

    def test_an_encoded_number_string_is_not_turned_into_a_number(self):
        # JSON column holds the string "123": stays '123', never the int 123
        assert self._cell("edgar_financials", "meta", json.dumps("123")) == "123"
        assert self._cell("edgar_financials", "meta", json.dumps("true")) == "true"

    def test_text_verbatim_json_column_is_decoded_only_once(self):
        name = next(n for n, s in rawstore.MANIFEST.items() if s.get("text_verbatim")
                    and any(rawstore._col_type(n, c).split("(", 1)[0] == "JSON"
                            for c in rawstore._columns_of(s)))
        col = next(c for c in rawstore._columns_of(rawstore.MANIFEST[name])
                   if rawstore._col_type(name, c).split("(", 1)[0] == "JSON")
        inner = json.dumps({"a": 1})
        assert self._cell(name, col, json.dumps(inner)) == inner


# ── an empty replace_all would empty the table: refused unless asked for (#284, B9) ─────────
class TestReplaceAllEmptyGuard:
    """``replace_all(table, [])`` is DELETE + nothing: a degraded run (no peers, no edges, a
    failed fetch that produced no rows) used to wipe the last good data without a word."""

    def _seed(self):
        rawstore.write_rows("watchlist", [{"ticker": "AAPL"}, {"ticker": "MSFT"}])

    def test_an_empty_batch_raises_and_keeps_the_rows(self, rawstore_tmp_db):
        self._seed()
        with pytest.raises(rawstore.RawStoreError, match="allow_empty"):
            rawstore.replace_all("watchlist", [])
        assert rawstore.load_watchlist() == {"AAPL", "MSFT"}

    def test_allow_empty_empties_the_table(self, rawstore_tmp_db):
        self._seed()
        assert rawstore.replace_all("watchlist", [], allow_empty=True) == 0
        assert rawstore.count("watchlist") == 0

    def test_a_non_empty_batch_still_replaces_everything(self, rawstore_tmp_db):
        self._seed()
        assert rawstore.replace_all("watchlist", [{"ticker": "NVDA"}]) == 1
        assert rawstore.load_watchlist() == {"NVDA"}

    def test_the_flag_is_keyword_only(self, rawstore_tmp_db):
        self._seed()
        with pytest.raises(TypeError):
            rawstore.replace_all("watchlist", [], True)
        assert rawstore.count("watchlist") == 2

    def test_clearing_the_watchlist_is_an_explicit_choice(self, rawstore_tmp_db):
        # the one caller that really means "empty": the user removed their last ticker
        self._seed()
        rawstore.save_watchlist(set())
        assert rawstore.load_watchlist() == set()

    def test_an_unknown_table_is_still_reported_as_unknown(self, rawstore_tmp_db):
        with pytest.raises(rawstore.RawStoreError, match="unknown table"):
            rawstore.replace_all("no_such_table", [])
