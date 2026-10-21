"""#368: stale ``.rc-proc-<pid>`` markers are swept, and a reused PID is not a peer.

``stop.ps1`` kills the dashboard, so the ``atexit`` hook that removes the presence
marker never runs and one leftover accumulates per restart. Registering now
sweeps the directory's other markers: one whose PID is not alive, or whose
recorded start time no longer matches the live process's (the PID was reused,
the #355 rule), is removed. Only marker files are touched — never a ``.duckdb``,
its ``.wal`` or a lock file.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from RevenueChaser.core import rawstore


@pytest.fixture
def store_dir(rawstore_tmp_db):
    """The temp store's folder, with this process's own registration cleared."""
    d = Path(rawstore_tmp_db).parent
    rawstore._PRESENT_IN.discard(str(d))
    rawstore._presence_file(d, os.getpid()).unlink(missing_ok=True)
    yield d
    rawstore._PRESENT_IN.discard(str(d))
    rawstore._presence_file(d, os.getpid()).unlink(missing_ok=True)


def _marker(store_dir: Path, pid: int, text: str) -> Path:
    f = rawstore._presence_file(store_dir, pid)
    f.write_text(text, encoding="ascii")
    return f


def _live_proc():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_a_dead_pids_marker_is_removed_at_registration(store_dir):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    m = _marker(store_dir, proc.pid, f"{proc.pid} 12345")
    rawstore._register_presence(store_dir / "raw.duckdb")
    assert not m.exists()


def test_an_old_format_live_marker_is_kept(store_dir):
    proc = _live_proc()
    try:
        m = _marker(store_dir, proc.pid, str(proc.pid))
        rawstore._register_presence(store_dir / "raw.duckdb")
        assert m.exists(), "a live PID's old-format marker must be left alone"
    finally:
        proc.kill()
        proc.wait()


def test_a_reused_pid_marker_is_removed_and_not_a_peer(store_dir, monkeypatch):
    proc = _live_proc()
    try:
        m = _marker(store_dir, proc.pid, f"{proc.pid} 111")
        monkeypatch.setattr(rawstore.procs, "process_start_time", lambda pid: "222")
        assert rawstore._peer_pids(store_dir / "raw.duckdb") == []
        assert not m.exists(), "a reused PID's marker must be swept, not reported as a peer"
    finally:
        proc.kill()
        proc.wait()


def test_a_reused_pid_marker_is_removed_at_registration(store_dir, monkeypatch):
    proc = _live_proc()
    try:
        m = _marker(store_dir, proc.pid, f"{proc.pid} 111")
        monkeypatch.setattr(rawstore.procs, "process_start_time", lambda pid: "222")
        rawstore._register_presence(store_dir / "raw.duckdb")
        assert not m.exists()
    finally:
        proc.kill()
        proc.wait()


def test_a_matching_start_time_is_still_a_peer(store_dir):
    proc = _live_proc()
    try:
        start = rawstore.procs.process_start_time(proc.pid)
        m = _marker(store_dir, proc.pid, f"{proc.pid} {start}")
        assert rawstore._peer_pids(store_dir / "raw.duckdb") == [proc.pid]
        assert m.exists()
    finally:
        proc.kill()
        proc.wait()


def test_the_sweep_never_touches_a_duckdb_or_its_sidecars(store_dir):
    duck = store_dir / "raw.duckdb"
    duck.write_bytes(b"not a real database")
    wal = store_dir / "raw.duckdb.wal"
    wal.write_bytes(b"")
    lock = store_dir / "raw.duckdb.lock"
    lock.write_bytes(b"")
    rawstore._register_presence(store_dir / "raw.duckdb")
    assert duck.read_bytes() == b"not a real database"
    assert wal.exists() and lock.exists()


def test_the_process_writes_its_own_marker_in_the_new_format(store_dir):
    rawstore._register_presence(store_dir / "raw.duckdb")
    f = rawstore._presence_file(store_dir, os.getpid())
    parts = f.read_text(encoding="ascii").split()
    assert parts[0] == str(os.getpid())
    assert len(parts) == 2, f"expected '<pid> <start time>', got {parts!r}"
    assert parts[1] == rawstore.procs.process_start_time(os.getpid())
