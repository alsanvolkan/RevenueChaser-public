"""Shrink the store files: rewrite them without their free blocks.

    python -m RevenueChaser.scripts.compact_store            # all three files
    python -m RevenueChaser.scripts.compact_store --db edgar

DuckDB re-uses the blocks that DELETE / ``replace_all`` free, but never returns
them to the OS, so ``raw.duckdb``, ``edgar.duckdb`` and ``discord.duckdb`` only ever grow. This
applies the schema manifest first (so a pending compressed-column rebuild
happens before the copy), then runs ``rawstore.compact`` per file: a
``COPY FROM DATABASE`` into a fresh file, per-table row counts verified, then
swapped in. The previous file is kept as ``<name>.duckdb.pre-compact`` — delete
it once you are happy.

Stop the dashboard and scheduled jobs first; it refuses to run otherwise.
"""
from __future__ import annotations

import argparse
import sys

from RevenueChaser.core import rawstore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compact .store/raw.duckdb, edgar.duckdb and discord.duckdb.")
    ap.add_argument("--db", choices=["raw", "edgar", "discord", "both"], default="both")
    args = ap.parse_args(argv)
    try:
        rawstore.ensure_all_tables()
        for db in (["raw", "edgar", "discord"] if args.db == "both" else [args.db]):
            r = rawstore.compact(db)
            print(f"{db}: {r['before_bytes'] / 1e6:.0f} MB -> {r['after_bytes'] / 1e6:.0f} MB "
                  f"({r['tables']} tables; previous file: {r['previous']})")
    except rawstore.RawStoreError as exc:
        print(f"compact FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
