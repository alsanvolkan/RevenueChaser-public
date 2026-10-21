"""#108 sector coarse measurement script: the read-only mode writes nothing and measures the same sector plan.

Offline: two small theme runs in a temp store.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import themes

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "108_sector_coarse.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(),
                                reason=f"{_SCRIPT.name} is not in this tree")


@pytest.fixture(scope="module")
def script():
    # The file name starts with a digit, so it cannot be imported by name.
    spec = importlib.util.spec_from_file_location("sector_coarse_108", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SECTORS = {"T0": "Technology", "T1": "Technology", "T2": "Healthcare", "T3": "Technology", "T4": "Technology",
           "B0": "Financial Services", "B1": "Financial Services", "B2": "Financial Services", "B3": "Technology"}


def _seed_run(run, mediums, current):
    rows = [{"theme_id": f"{run}:c0", "level": "coarse", "parent_id": "", "label": "Giant", "size": 10}]
    members = []
    for mi, tickers in mediums.items():
        rows.append({"theme_id": f"{run}:m{mi}", "level": "medium", "parent_id": f"{run}:c0",
                     "label": f"Med{mi}", "size": len(tickers)})
        rows.append({"theme_id": f"{run}:f{mi}", "level": "fine", "parent_id": f"{run}:m{mi}",
                     "label": f"Fine{mi}", "size": len(tickers)})
        members += [{"ticker": t, "theme_id": f"{run}:f{mi}", "weight": 1.0} for t in tickers]
    rawstore.write_rows("relatedness_themes", rows)
    rawstore.write_rows("relatedness_theme_members", members)
    rawstore.write_rows("theme_runs", [{"id": run, "created_at": "2026-10-16T00:00:00+00:00",
                                        "k_fine": len(mediums), "k_medium": len(mediums), "k_coarse": 1,
                                        "jaccard": 0.74, "is_current": current}])


@pytest.fixture
def two_runs(rawstore_tmp_db):
    rawstore.write_rows("equity_universe", [{"ticker": t, "market_cap": 1e9, "sector": s}
                                            for t, s in SECTORS.items()])
    _seed_run("base", {0: ["T0", "T1", "T2", "T3", "T4"], 1: ["B0", "B1", "B2", "B3", "U0"]}, current=1)
    _seed_run("shadow", {0: ["T0", "T1", "T2"], 1: ["T3", "T4"], 2: ["B0", "B1", "B2", "B3"], 3: ["U0"]},
              current=0)


def _snapshot():
    out = {}
    for table in ("relatedness_themes", "relatedness_theme_members", "theme_runs"):
        out[table] = sorted(tuple(sorted(r.items())) for r in rawstore.get_all(table))
    return out


def test_read_only_measures_the_sector_plan_and_writes_nothing(script, two_runs, monkeypatch):
    before = _snapshot()
    script.block_writes(monkeypatch.setattr)
    out = script.measure("shadow", None, read_only=True)

    assert out["mode"] == "read-only" and out["base_run"] == "base"
    assert out["nmf_stored"]["k_coarse"] == 1 and out["nmf_stored"]["max_share"] == 1.0
    assert out["sector"]["k_coarse"] == 3 and out["sector"]["max_share"] == 0.5
    assert out["sector"]["summary"]["sizes"] == {"Technology": 5, "Financial Services": 4, "Unclassified": 1}
    assert out["sector"]["purity_vs_own_sector"] == 0.8
    assert "skipped" in out["nmf_rebalanced"] and "sector_written" not in out
    assert out["run_to_run"]["common_names"] == 10
    monkeypatch.undo()
    assert _snapshot() == before


def test_block_writes_refuses_every_rawstore_writer(script, rawstore_tmp_db, monkeypatch):
    script.block_writes(monkeypatch.setattr)
    for name in ("write_rows", "replace_all", "execute", "atomic", "transaction", "ensure_table",
                 "ensure_all_tables", "delete"):
        with pytest.raises(RuntimeError, match="read-only"):
            getattr(rawstore, name)("relatedness_themes", [])


def test_copy_mode_writes_the_plan_it_measured(script, two_runs):
    ro = script.measure("shadow", None, read_only=True)
    out = script.measure("shadow", None)
    assert out["mode"] == "copy"
    assert out["sector_written"]["matches_plan"] is True
    assert out["sector"]["summary"] == ro["sector"]["summary"] == out["sector_written"]["result"]
    assert {r["label"] for r in themes.run_rows("relatedness_themes", "shadow", "coarse")} == {
        "Technology", "Financial Services", "Unclassified"}


def _store(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "raw.duckdb").write_bytes(b"")
    return path


def test_the_live_store_is_accepted_only_read_only(script, tmp_path):
    live = _store(tmp_path / ".store")
    copy = _store(tmp_path / ".cache" / "research" / "108" / "sector")
    other = _store(tmp_path / "elsewhere")
    assert script.store_refusal(copy, False, repo_dir=tmp_path) is None
    assert script.store_refusal(copy, True, repo_dir=tmp_path) is None
    assert script.store_refusal(live, True, repo_dir=tmp_path) is None
    assert "read-only" in script.store_refusal(live, False, repo_dir=tmp_path)
    assert script.store_refusal(other, True, repo_dir=tmp_path)
    assert script.store_refusal(tmp_path / "missing", True, repo_dir=tmp_path)


def _sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_read_only_leaves_a_leftover_wal_and_the_store_file_untouched(script, two_runs, monkeypatch):
    """Opus review M1: rawstore opens every handle read-write, and a read-write open folds a leftover WAL (a killed
    dashboard) into the file on close. ``--read-only`` must open DuckDB ``read_only=True``."""
    import duckdb
    db = Path(rawstore.DB_PATH)
    wal = Path(str(db) + ".wal")
    con = duckdb.connect(str(db))
    con.execute("PRAGMA disable_checkpoint_on_shutdown")
    con.execute("UPDATE theme_runs SET jaccard = 0.5 WHERE id = 'base'")
    con.close()
    assert wal.exists()
    before = _sha(db)

    script.block_writes(monkeypatch.setattr)
    script.open_read_only(monkeypatch.setattr)
    out = script.measure("shadow", None, read_only=True)

    assert out["sector"]["k_coarse"] == 3
    assert _sha(db) == before
    assert wal.exists()


def test_open_read_only_refuses_a_write(script, two_runs, monkeypatch):
    import duckdb
    script.open_read_only(monkeypatch.setattr)
    with pytest.raises(duckdb.Error):
        with rawstore.connect() as conn:
            conn.execute("UPDATE theme_runs SET jaccard = 0.1")


def test_main_read_only_opens_duckdb_read_only_before_measuring(script, monkeypatch, tmp_path):
    # Without the open_read_only() call every other test still passed (R8a review B S8, API review RB-9)
    store = tmp_path.resolve() / ".store"
    seen = []
    monkeypatch.setattr(script, "_args", lambda argv: argparse.Namespace(
        store_dir=store, read_only=True, run_id="shadow", base_run=None, max_share=0.45))
    monkeypatch.setattr(script, "_redirect", lambda store_dir, read_only: None)
    monkeypatch.setattr(rawstore, "DB_PATH", store / "raw.duckdb")
    monkeypatch.setattr(script, "block_writes", lambda *a: seen.append("block"))
    monkeypatch.setattr(script, "open_read_only", lambda *a: seen.append("read_only"))
    monkeypatch.setattr(script, "measure", lambda *a, **k: seen.append("measure") or {"sector": {}})
    monkeypatch.setattr(script, "GUARD", tmp_path / "guard")
    assert script.main([]) == 0
    assert seen == ["block", "read_only", "measure"]
