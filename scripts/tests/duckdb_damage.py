"""Test helper: damage a DuckDB file the way a bad disk or a bad copy does (review M3, #275).

One flipped byte in a used data block leaves the file header and the catalog readable, so ``duckdb.connect``
succeeds and the damage only shows when a query reads that block: ``duckdb.IOException: Corrupt database file:
computed checksum ... does not match``. That error is not a ``BackupError`` and not an ``OSError``."""
from __future__ import annotations

from pathlib import Path

import duckdb

_HEADERS = 3 * 4096          # DuckDB: the main header and the two database headers, then the blocks
_BLOCK = 262144              # DuckDB's default block size; every block carries its own checksum


def _reads_without_error(path: Path) -> bool:
    conn = duckdb.connect(str(path), read_only=True)
    try:
        for (name,) in conn.execute("SELECT table_name FROM information_schema.tables "
                                    "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'").fetchall():
            conn.execute(f'SELECT count(*), coalesce(sum(hash(t)::HUGEINT), 0) FROM "{name}" t').fetchone()
    except duckdb.IOException:
        return False
    finally:
        conn.close()
    return True


def damage_data_block(path: Path) -> int:
    """Flip one byte of ``path`` so that it still opens but a query over its tables raises ``duckdb.IOException``.

    Returns the offset it flipped. Fails the test when no byte of the file does that (an empty store)."""
    path = Path(path)
    raw = path.read_bytes()
    probe = path.with_name(path.name + ".probe")
    # One byte per block is enough: the checksum covers the whole block. 64 is past the block's own checksum.
    offsets = [*range(_HEADERS + 64, len(raw), _BLOCK), *range(_HEADERS + 8, len(raw), 4096)]
    try:
        for offset in offsets:
            trial = bytearray(raw)
            trial[offset] ^= 0xFF
            probe.write_bytes(bytes(trial))
            try:
                if not _reads_without_error(probe):
                    path.write_bytes(bytes(trial))
                    return offset
            except duckdb.Error:           # this byte broke the catalog: the file would not even open
                continue
    finally:
        probe.unlink(missing_ok=True)
    raise AssertionError(f"no byte of {path.name} can be flipped so that it opens and fails on a query")
