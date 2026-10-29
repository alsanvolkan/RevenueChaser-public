"""Serving files: built as a new file, checked, then swapped over the live one; read only through ``open_read`` (D1).

One builder process writes. Each kind (``market``; ``spine`` since PR 5; ``prices`` since PR 7; ``ownership`` since
PR 16) is built into ``<kind>.<build_id>.duckdb``
beside the live ``<kind>.duckdb`` under ``config.MARKET_DIR``; ``BuildFile.swap`` closes the writer (after a
``CHECKPOINT``, so no ``.wal`` is left), opens the new file read-only, checks its ``serving_schema_version`` row and
the row count of every expected table, and only then moves it into place. On any failure — a check, or an exception inside the
``build()`` block — the new file is deleted and the live file is not touched (test T3).

The swap never leaves the live name missing: the previous file is first hard-linked to ``<kind>.prev.duckdb``
(copied where the file system has no hard links), then ``os.replace`` moves the new file over the live name in one
step. A reader that opened the old file keeps reading it; the next ``open_read`` gets the new one.

The dashboard and the boards open serving files with ``open_read`` only: read-only, the path resolved on every
call, never a connection kept across requests. Serving files are never compacted; they are rebuilt (F10 B).
"""
from __future__ import annotations

import os
import shutil
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

import duckdb

from ..core import config

# The serving schema of each kind. A change to a kind's tables bumps its number in its own PR (box rule).
SERVING_SCHEMA_VERSION = {"market": 1, "spine": 1, "ownership": 1}

# PR 7 (G2): ``prices.duckdb``, the daily bars, corporate actions, shares and market cap (``build/prices/``).
SERVING_SCHEMA_VERSION["prices"] = 1

KINDS = tuple(SERVING_SCHEMA_VERSION)
VERSION_TABLE = "serving_schema_version"
# A table may lose at most this share of its live rows in one build without an explicit allow_shrink. The datasets
# only ever add or replace rows, so any real shrink is a build from a partial or missing Parquet tree.
MAX_SHRINK = 0.01


class SwapRefused(RuntimeError):
    """The new build failed its checks; it was deleted and the live file left as it was."""


class ServingFileMissing(FileNotFoundError):
    """No live serving file of this kind yet (nothing has been built)."""


def _kind(kind: str) -> str:
    if kind not in SERVING_SCHEMA_VERSION:
        raise ValueError(f"unknown serving kind {kind!r}; known: {KINDS}")
    return kind


def live_path(kind: str) -> Path:
    return Path(config.MARKET_DIR) / f"{_kind(kind)}.duckdb"


def prev_path(kind: str) -> Path:
    return Path(config.MARKET_DIR) / f"{_kind(kind)}.prev.duckdb"


def build_path(kind: str, build_id: str) -> Path:
    return Path(config.MARKET_DIR) / f"{_kind(kind)}.{build_id}.duckdb"


def new_build_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def open_read(kind: str) -> duckdb.DuckDBPyConnection:
    """A read-only connection to the live ``kind`` file, resolved now. The caller closes it (``with`` works).

    DuckDB caches one database instance per path in a process (S7): a connection kept open across a swap goes on
    reading the build it opened. Open and close per request, as ``read`` does.
    """
    path = live_path(kind)
    if not path.exists():
        raise ServingFileMissing(f"no {kind} serving file at {path} yet")
    return duckdb.connect(str(path), read_only=True)


def _remove(path: Path) -> None:
    for p in (path, path.with_name(path.name + ".wal")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


class BuildFile:
    """One build in progress: its read-write connection, path and id. Made by :func:`build`."""

    def __init__(self, kind: str, build_id: str, path: Path, con: duckdb.DuckDBPyConnection):
        self.kind, self.build_id, self.path, self.con = kind, build_id, path, con
        self.swapped = False

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None

    def swap(self, expected_counts: dict[str, int], checks: Iterable[dict] = (), *,
             allow_shrink: bool = False) -> Path:
        """Check this build and move it over the live file; :class:`SwapRefused` (file deleted) when a check fails.

        ``expected_counts`` maps every table the build must hold to its row count; ``checks`` are the build's
        ``dataset_check`` rows, and one whose result is ``failed`` refuses the swap. Every table of the live file
        must be in the build and may not empty or shrink by more than ``MAX_SHRINK`` (B2: the expected counts come
        from the build itself, so only the live file can show a build made from a missing Parquet tree), unless
        ``allow_shrink`` (a request's explicit ``"allow_shrink": true``).
        """
        self.con.execute("CHECKPOINT")
        self.close()
        try:
            _verify(self.kind, self.path, expected_counts, list(checks))
            if not allow_shrink:
                _verify_against_live(self.kind, self.path)
        except Exception as e:
            _remove(self.path)
            raise SwapRefused(f"{self.kind} build {self.build_id} refused: {e}") from e
        live, prev = live_path(self.kind), prev_path(self.kind)
        if live.exists():
            _remove(prev)
            try:
                os.link(live, prev)
            except OSError:
                shutil.copy2(live, prev)
        os.replace(self.path, live)
        self.swapped = True
        return live


def _verify(kind: str, path: Path, expected_counts: dict[str, int], checks: list[dict]) -> None:
    failed = [c.get("dataset") for c in checks if c.get("result") == "failed"]
    if failed:
        raise ValueError(f"failed dataset checks: {failed}")
    if path.with_name(path.name + ".wal").exists():
        raise ValueError("the build file still has a .wal after its checkpoint")
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute(f"SELECT kind, version FROM {VERSION_TABLE}").fetchall()
        if rows != [(kind, SERVING_SCHEMA_VERSION[kind])]:
            raise ValueError(f"{VERSION_TABLE} is {rows}, expected [({kind!r}, {SERVING_SCHEMA_VERSION[kind]})]")
        tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
        for table, want in expected_counts.items():
            if table not in tables:
                raise ValueError(f"table {table} is missing")
            got = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            if got != want:
                raise ValueError(f"{table} has {got} rows, expected {want}")
    finally:
        con.close()


def sweep_builds(kind: str) -> list[Path]:
    """Remove the build files of ``kind`` a killed builder left (``<kind>.<build_id>.duckdb`` and its ``.wal``);
    never the live or the ``prev`` file. Called by the builder at start, while it holds the ``rcdata_build`` lock,
    so no build of its own is in progress. Returns what it removed."""
    keep = {live_path(kind).name, prev_path(kind).name}
    removed = []
    for p in sorted(Path(config.MARKET_DIR).glob(f"{_kind(kind)}.*.duckdb*")):
        stem = p.name[:-4] if p.name.endswith(".wal") else p.name
        if stem in keep or not stem.endswith(".duckdb"):
            continue
        p.unlink(missing_ok=True)
        removed.append(p)
    return removed


def _counts(path: Path) -> dict[str, int]:
    con = duckdb.connect(str(path), read_only=True)
    try:
        names = [r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = "
                                           "current_database()").fetchall() if r[0] != VERSION_TABLE]
        return {n: int(con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0]) for n in names}
    finally:
        con.close()


def _verify_against_live(kind: str, path: Path) -> None:
    live = live_path(kind)
    if not live.exists():
        return
    old, new = _counts(live), _counts(path)
    for table, before in sorted(old.items()):
        if table not in new:
            raise ValueError(f"table {table} of the live file is missing from the build")
        after = new[table]
        if before > 0 and after == 0:
            raise ValueError(f"{table} empties: {before} rows live, 0 in the build")
        if after < before * (1 - MAX_SHRINK):
            raise ValueError(f"{table} shrinks from {before} to {after} rows (more than {MAX_SHRINK:.0%})")


@contextmanager
def build(kind: str) -> Iterator[BuildFile]:
    """A new, stamped build file of ``kind``; deleted unless ``swap`` moved it into place inside the block.

    The only read-write handle the builder ever opens on a serving file is this one, on its own new file.
    """
    build_id = new_build_id()
    path = build_path(kind, build_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    b = BuildFile(kind, build_id, path, con)
    try:
        con.execute(f"CREATE TABLE {VERSION_TABLE} (kind TEXT, version INTEGER, built_at DOUBLE, build_id TEXT)")
        con.execute(f"INSERT INTO {VERSION_TABLE} VALUES (?, ?, ?, ?)",
                    (kind, SERVING_SCHEMA_VERSION[kind], time.time(), build_id))
        yield b
    finally:
        b.close()
        if not b.swapped:
            _remove(path)
