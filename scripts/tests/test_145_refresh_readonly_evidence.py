"""#145/#146 evidence script: each scenario's scratch store holds edgar.duckdb next to raw.duckdb.

Without edgar.duckdb the EDGAR readers return nothing, so Asymmetric Growth and Fundamentals score nothing on
either side and their PASS rows prove nothing (review on #146, B1). The child process is faked: these tests check
only what ``run`` copies into the scratch folder and what it leaves on the source.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "145_refresh_readonly_evidence.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(),
                                reason=f"{_SCRIPT.name} is not in this tree")


@pytest.fixture(scope="module")
def evidence():
    # The file name starts with a digit, so it cannot be imported by name.
    spec = importlib.util.spec_from_file_location("evidence_145", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def seen(evidence, monkeypatch):
    """Fake child: records the scratch store's files and their bytes, then writes an empty scenario result."""
    record: list[dict] = []

    def fake_run(cmd, env=None, **kw):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="abc1234\n", stderr="")
        store = Path(env["RC_STORE_DIR"])
        record.append({p.name: p.read_bytes() for p in store.iterdir()})
        Path(cmd[cmd.index("--result") + 1]).write_text(json.dumps({}), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(evidence.subprocess, "run", fake_run)
    return record


def _args(db: Path, out: Path, edgar_db: Path | None = None) -> argparse.Namespace:
    return argparse.Namespace(db=str(db), edgar_db=str(edgar_db) if edgar_db else None, label="branch",
                              repo=None, out=str(out))


def test_edgar_beside_the_db_is_copied_into_every_scenario(evidence, seen, tmp_path):
    store = tmp_path / ".store"
    store.mkdir()
    (store / "raw.duckdb").write_bytes(b"raw")
    (store / "edgar.duckdb").write_bytes(b"edgar")
    (store / "edgar.duckdb.wal").write_bytes(b"edgar-wal")

    assert evidence.run(_args(store / "raw.duckdb", tmp_path / "out")) == 0

    assert len(seen) == len(evidence.SCENARIOS)
    for files in seen:
        assert files == {"raw.duckdb": b"raw", "edgar.duckdb": b"edgar", "edgar.duckdb.wal": b"edgar-wal"}
    report = json.loads((tmp_path / "out" / "branch.json").read_text(encoding="utf-8"))
    assert report["edgar_sha256"] == _sha(store / "edgar.duckdb")
    assert report["source_unchanged"] is True


def test_a_backup_pair_is_copied_under_the_store_names(evidence, seen, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    raw = backups / "raw_20261004_101010_000001.duckdb"
    edgar = backups / "edgar_20261004_101010_000002.duckdb"
    raw.write_bytes(b"raw")
    edgar.write_bytes(b"edgar")

    assert evidence.run(_args(raw, tmp_path / "out", edgar_db=edgar)) == 0

    for files in seen:
        assert files == {"raw.duckdb": b"raw", "edgar.duckdb": b"edgar"}


def test_a_missing_edgar_store_is_skipped_with_a_warning(evidence, seen, tmp_path, capsys):
    store = tmp_path / ".store"
    store.mkdir()
    (store / "raw.duckdb").write_bytes(b"raw")

    assert evidence.run(_args(store / "raw.duckdb", tmp_path / "out")) == 0

    assert all(files == {"raw.duckdb": b"raw"} for files in seen)
    assert "no edgar.duckdb" in capsys.readouterr().out
    report = json.loads((tmp_path / "out" / "branch.json").read_text(encoding="utf-8"))
    assert report["edgar_source"] is None


def test_an_explicit_edgar_path_that_does_not_exist_stops_the_run(evidence, seen, tmp_path):
    store = tmp_path / ".store"
    store.mkdir()
    (store / "raw.duckdb").write_bytes(b"raw")

    assert evidence.run(_args(store / "raw.duckdb", tmp_path / "out", edgar_db=tmp_path / "nope.duckdb")) == 2
    assert seen == []


def test_the_cli_takes_edgar_db(evidence, monkeypatch):
    got = {}
    monkeypatch.setattr(evidence, "run", lambda a: got.setdefault("edgar_db", a.edgar_db) and 0)
    evidence.main(["run", "--db", "x.duckdb", "--edgar-db", "e.duckdb", "--label", "main"])
    assert got["edgar_db"] == "e.duckdb"
