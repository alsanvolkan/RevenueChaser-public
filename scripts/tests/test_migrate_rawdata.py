"""Tests for the rawData -> .store/raw.duckdb migration script (Phase 5).

Earnings Radar calibration logs are deliberately NOT migrated (append-only
file-based).
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import rawstore


def _seed_rawdata(data_dir):
    """Create a minimal legacy rawData tree to migrate."""
    (data_dir / "tickers").mkdir(parents=True, exist_ok=True)
    (data_dir / "tickers" / "AAPL_raw.json").write_text(
        json.dumps({"revenue": 100, "_fetched_at": 1.0}), encoding="utf-8")
    (data_dir / "watchlist_tickers.json").write_text(json.dumps(["NOK"]), encoding="utf-8")


def test_migrate_populates_db_and_keeps_originals_by_default(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
    data_dir = rawstore_tmp_db.parent
    monkeypatch.setattr(m, "LEGACY_DATA_DIR", data_dir)
    _seed_rawdata(data_dir)

    rc = m.main(["--dataset", "ticker_payload_cache", "watchlist"])
    assert rc == 0
    assert rawstore.get("ticker_payload_cache", "AAPL")["payload"]["revenue"] == 100
    assert {r["ticker"] for r in rawstore.get_all("watchlist")} == {"NOK"}   # the store table, not the ops list
    # default --no-delete: originals still on disk
    assert (data_dir / "tickers" / "AAPL_raw.json").exists()
    assert (data_dir / "watchlist_tickers.json").exists()


def test_migrate_is_idempotent(rawstore_tmp_db, monkeypatch, capsys):
    """A full re-run SKIPS datasets already recorded PASS in migration_manifest.

    The skip is bypassed by ``--dataset``, so this has to be a full run — an
    earlier version of this test filtered by dataset and therefore never
    exercised ``_already_verified`` at all.
    """
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
    data_dir = rawstore_tmp_db.parent
    monkeypatch.setattr(m, "LEGACY_DATA_DIR", data_dir)
    _seed_rawdata(data_dir)

    assert m.main([]) == 0
    capsys.readouterr()
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert "SKIP ticker_payload_cache: already migrated" in out
    assert "SKIP watchlist: already migrated" in out
    assert rawstore.count("ticker_payload_cache") == 1
    assert {r["ticker"] for r in rawstore.get_all("watchlist")} == {"NOK"}   # the store table, not the ops list


def test_already_verified_reflects_the_manifest(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
    data_dir = rawstore_tmp_db.parent
    monkeypatch.setattr(m, "LEGACY_DATA_DIR", data_dir)
    _seed_rawdata(data_dir)
    assert not m._already_verified("ticker_payload_cache")
    assert m.main([]) == 0
    assert m._already_verified("ticker_payload_cache")
    assert not m._already_verified("edgar_financials")


def test_calibration_logs_are_not_a_migration_dataset():
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
    assert "decisions" not in m.MANIFEST
    assert "outcomes" not in m.MANIFEST
    assert "tuning_log" not in m.MANIFEST


def test_reset_guard_refuses_when_table_grew(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
    data_dir = rawstore_tmp_db.parent
    monkeypatch.setattr(m, "LEGACY_DATA_DIR", data_dir)
    (data_dir / "tickers").mkdir(parents=True)
    (data_dir / "tickers" / "AAPL_raw.json").write_text('{"x": 1}', encoding="utf-8")
    assert m.main(["--dataset", "ticker_payload_cache"]) == 0  # completed migration, manifest recorded
    assert rawstore.count("ticker_payload_cache") == 1
    # post-migration growth in the live DB (originals deleted in real life)
    rawstore.write_rows("ticker_payload_cache", [{"ticker": "MSFT", "payload": {"x": 2},
                                          "fetched_at": 1.0, "fetched_at_iso": ""}])

    rc = m.main(["--reset"])
    assert rc == 1  # refuses without --force
    assert rawstore.count("ticker_payload_cache") == 2  # DB untouched


def test_reset_force_rebuilds(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
    data_dir = rawstore_tmp_db.parent
    monkeypatch.setattr(m, "LEGACY_DATA_DIR", data_dir)
    (data_dir / "tickers").mkdir(parents=True)
    (data_dir / "tickers" / "AAPL_raw.json").write_text('{"revenue": 7}', encoding="utf-8")
    rawstore.write_rows("ticker_payload_cache", [{"ticker": "MSFT", "payload": {"x": 2},
                                          "fetched_at": 1.0, "fetched_at_iso": ""}])

    rc = m.main(["--reset", "--force", "--dataset", "ticker_payload_cache"])
    assert rc == 0
    assert rawstore.count("ticker_payload_cache") == 1  # DB was dropped and rebuilt
    assert rawstore.get("ticker_payload_cache", "AAPL")["payload"]["revenue"] == 7


def test_cli_entrypoint_works_via_app_sh():
    """The app.sh wrapper resolves to the same module."""
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
    assert callable(m.main)


def test_purge_removes_legacy_folders_and_salvages_zips(rawstore_tmp_db, tmp_path, monkeypatch):
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
    from RevenueChaser.core import config as cfg

    legacy = tmp_path / "legacy_root"
    data_dir = legacy / "rawData"
    out_dir = legacy / "output"
    cache = tmp_path / ".cache"
    monkeypatch.setattr(m, "LEGACY_DATA_DIR", data_dir)
    monkeypatch.setattr(m, "LEGACY_OUTPUT_DIR", out_dir)
    monkeypatch.setattr(m, "LEGACY_EMPTY_DATA_DIR", legacy / "data")
    monkeypatch.setattr(cfg, "OUTPUT_DIR", cache)

    # legacy sources: a ticker cache, a 13F zip, and output/ artifacts
    (data_dir / "tickers").mkdir(parents=True)
    (data_dir / "tickers" / "AAPL_raw.json").write_text('{"revenue": 100}', encoding="utf-8")
    zips = data_dir / "sec_13f"
    zips.mkdir(parents=True)
    (zips / "2026q2_form13f.zip").write_bytes(b"PK\x03\x04 fake zip")
    (out_dir / "earnings").mkdir(parents=True)
    (out_dir / "earnings" / "revenue_chaser_1.html").write_text("<html>", encoding="utf-8")

    rc = m.main(["--reset", "--force", "--purge"])
    assert rc == 0
    assert not data_dir.exists(), "rawData/ purged"
    assert not out_dir.exists(), "output/ purged"
    assert rawstore.get("ticker_payload_cache", "AAPL")["payload"]["revenue"] == 100
    # zip salvaged to the .cache ingest dir, output artifact moved (not overwritten)
    assert (cache / "sec_13f" / "2026q2_form13f.zip").exists()
    assert (cache / "earnings" / "revenue_chaser_1.html").read_text() == "<html>"


def test_purge_refused_when_a_dataset_fails(rawstore_tmp_db, tmp_path, monkeypatch):
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
    data_dir = tmp_path / "rawData"
    monkeypatch.setattr(m, "LEGACY_DATA_DIR", data_dir)
    (data_dir / "tickers").mkdir(parents=True)
    (data_dir / "tickers" / "AAPL_raw.json").write_text('{"revenue": 1}', encoding="utf-8")

    # tamper with the writer so migration records wrong counts -> FAIL
    import pytest
    orig = rawstore.write_rows

    def bad_write(name, rows):
        if name == "ticker_payload_cache":
            raise RuntimeError("boom")
        return orig(name, rows)

    monkeypatch.setattr(rawstore, "write_rows", bad_write)
    with pytest.raises(RuntimeError):
        m.main(["--reset", "--force", "--purge"])
    assert data_dir.exists(), "originals kept on failure"


def test_purge_refused_on_partial_dataset_run(rawstore_tmp_db, tmp_path, monkeypatch):
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
    data_dir = tmp_path / "rawData"
    monkeypatch.setattr(m, "LEGACY_DATA_DIR", data_dir)
    (data_dir / "tickers").mkdir(parents=True)
    (data_dir / "tickers" / "AAPL_raw.json").write_text('{"revenue": 1}', encoding="utf-8")
    (data_dir / "watchlist_tickers.json").write_text('["NOK"]', encoding="utf-8")

    # restricted run: watchlist migrates, but rawData still holds un-migrated
    # ticker_payload_cache sources -> purge must refuse
    rc = m.main(["--reset", "--force", "--purge", "--dataset", "watchlist"])
    assert rc == 1
    assert data_dir.exists(), "rawData kept — not everything was migrated"


def test_purge_refuses_to_delete_the_durable_store(rawstore_tmp_db, monkeypatch, capsys):
    """Regression: once ``rawData/`` is gone, ``default_source_dir()`` falls back
    to ``.store/`` — the folder holding raw.duckdb — and ``--purge`` rmtree's its
    source dir. It must refuse by name, not by luck."""
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
    monkeypatch.setattr(m, "LEGACY_DATA_DIR", rawstore_tmp_db.parent / "does_not_exist")

    assert m.default_source_dir() == rawstore_tmp_db.parent
    # a discoverable source sitting in .store/ must not turn the purge on
    (rawstore_tmp_db.parent / "watchlist_tickers.json").write_text('["NOK"]', encoding="utf-8")

    rc = m.main(["--purge"])
    assert rc == 1
    assert "refusing to purge" in capsys.readouterr().out
    assert rawstore_tmp_db.exists(), "raw.duckdb must survive"
    assert rawstore_tmp_db.parent.exists()


def test_purge_refuses_when_the_source_dir_is_gone(rawstore_tmp_db, tmp_path, monkeypatch, capsys):
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
    monkeypatch.setattr(m, "LEGACY_DATA_DIR", tmp_path / "rawData")
    (tmp_path / "rawData").mkdir()
    monkeypatch.setattr(m, "default_source_dir", lambda: tmp_path / "vanished")
    assert m.main(["--purge"]) == 1
    assert "does not exist" in capsys.readouterr().out


@pytest.mark.rawstore_schema_from_scratch
def test_failed_reset_restores_the_real_db_path(rawstore_tmp_db, tmp_path, monkeypatch):
    """A crashed ``--reset`` must leave ``rawstore.DB_PATH`` pointing at the real
    store, not at the temp file it just deleted."""
    from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
    data_dir = tmp_path / "rawData"
    monkeypatch.setattr(m, "LEGACY_DATA_DIR", data_dir)
    (data_dir / "tickers").mkdir(parents=True)
    (data_dir / "tickers" / "AAPL_raw.json").write_text('{"revenue": 1}', encoding="utf-8")

    real = rawstore.write_rows

    def boom(name, rows):
        if name == "ticker_payload_cache":
            raise RuntimeError("disk full")
        return real(name, rows)

    monkeypatch.setattr(rawstore, "write_rows", boom)
    try:
        with pytest.raises(RuntimeError):
            m.main(["--reset", "--force"])
    finally:
        rawstore.write_rows = real

    assert rawstore.DB_PATH == rawstore_tmp_db
    assert not rawstore.DB_PATH.with_name(rawstore.DB_PATH.name + ".new").exists()
    rawstore.ensure_all_tables()
    rawstore.write_rows("watchlist", [{"ticker": "AAPL"}])
    assert {r["ticker"] for r in rawstore.get_all("watchlist")} == {"AAPL"}   # the store table, not the ops list


class TestPurgeClassification:
    """``--purge`` rmtree's the whole source tree, so nothing may leave it
    unexamined. Regression: sec_13f/ticker_name.json (durable OpenFIGI input,
    read by run_sec13f, written by nothing) was neither migrated nor salvaged
    and would have been deleted silently."""

    def _tree(self, tmp_path):
        from RevenueChaser.scripts import migrate_rawdata_to_duckdb as m
        rd = tmp_path / "rawData"
        (rd / "tickers").mkdir(parents=True)
        (rd / "tickers" / "AAPL_raw.json").write_text('{"revenue": 1}', encoding="utf-8")
        return m, rd

    def test_regenerable_artifacts_are_recognised(self, rawstore_tmp_db, tmp_path):
        m, rd = self._tree(tmp_path)
        (rd / "fundamentals").mkdir()
        (rd / "fundamentals" / "fundamentals_latest.json").write_text("{}", encoding="utf-8")
        (rd / "leaps_latest.html").write_text("<html>", encoding="utf-8")
        (rd / "ticker_store").mkdir()
        (rd / "ticker_store" / "tickers.db-wal").write_bytes(b"")
        (rd / "sec_13f").mkdir()
        (rd / "sec_13f" / "aggregate.json").write_text("{}", encoding="utf-8")

        regen, unknown = m.classify_for_purge(rd, m.discover_sources(rd))
        assert not unknown, [str(p) for p in unknown]
        assert {p.name for p in regen} == {"fundamentals_latest.json", "leaps_latest.html",
                                           "tickers.db-wal", "aggregate.json"}

    def test_an_unrecognised_file_blocks_the_purge(self, rawstore_tmp_db, tmp_path,
                                                   monkeypatch, capsys):
        m, rd = self._tree(tmp_path)
        (rd / "sec_13f").mkdir()
        (rd / "sec_13f" / "something_precious.json").write_text('{"a": 1}', encoding="utf-8")
        monkeypatch.setattr(m, "LEGACY_DATA_DIR", rd)

        rc = m.main(["--purge", "--source-dir", str(rd)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "neither migrated nor recognised as regenerable" in out
        assert "something_precious.json" in out
        assert rd.exists(), "the tree must survive an unclassified file"
        assert (rd / "sec_13f" / "something_precious.json").exists()

    def test_force_lets_the_purge_proceed(self, rawstore_tmp_db, tmp_path, monkeypatch):
        m, rd = self._tree(tmp_path)
        (rd / "junk.dat").write_text("x", encoding="utf-8")
        monkeypatch.setattr(m, "LEGACY_DATA_DIR", rd)
        monkeypatch.setattr(m, "LEGACY_OUTPUT_DIR", tmp_path / "output")
        monkeypatch.setattr(m, "LEGACY_EMPTY_DATA_DIR", tmp_path / "data")
        assert m.main(["--reset", "--force", "--purge", "--source-dir", str(rd)]) == 0
        assert not rd.exists()

    def test_ticker_name_json_is_migrated_not_classified_away(self, rawstore_tmp_db, tmp_path):
        m, rd = self._tree(tmp_path)
        (rd / "sec_13f").mkdir()
        (rd / "sec_13f" / "ticker_name.json").write_text(
            '{"AAPL": {"name": "APPLE INC"}}', encoding="utf-8")
        sources = m.discover_sources(rd)
        assert sources.get("sec13f_ticker_names"), "must be discovered as a source"
        regen, unknown = m.classify_for_purge(rd, sources)
        assert not unknown and "ticker_name.json" not in {p.name for p in regen}

    def test_stray_latest_artifacts_are_salvaged_into_cache(self, rawstore_tmp_db, tmp_path,
                                                            monkeypatch):
        from RevenueChaser.core import config as cfg
        m, rd = self._tree(tmp_path)
        cache = tmp_path / ".cache"
        (rd / "fundamentals").mkdir()
        (rd / "fundamentals" / "fundamentals_latest.json").write_text('{"x": 1}', encoding="utf-8")
        monkeypatch.setattr(m, "LEGACY_DATA_DIR", rd)
        monkeypatch.setattr(m, "LEGACY_OUTPUT_DIR", tmp_path / "output")
        monkeypatch.setattr(m, "LEGACY_EMPTY_DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(cfg, "OUTPUT_DIR", cache)

        assert m.main(["--reset", "--force", "--purge", "--source-dir", str(rd)]) == 0
        assert not rd.exists()
        assert (cache / "fundamentals" / "fundamentals_latest.json").read_text() == '{"x": 1}'
