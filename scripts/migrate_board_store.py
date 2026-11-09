"""Copy a board's tables from the old store file into its own ``boards/<board>.duckdb`` (DECISIONS S9).

A board PR moves a table by changing its MANIFEST ``db`` to the board; this script then carries the rows across,
on the live store, with the dashboard stopped (plan 2026-11-08 §3)::

    python -m RevenueChaser.scripts.migrate_board_store relatedness --dry-run
    python -m RevenueChaser.scripts.migrate_board_store relatedness              # copy; twice is the same as once
    python -m RevenueChaser.scripts.migrate_board_store relatedness --rename-legacy   # old tables -> legacy_<name>
    python -m RevenueChaser.scripts.migrate_board_store relatedness --reverse    # rollback: board rows -> legacy_

The copy is by PRIMARY KEY (``INSERT OR IGNORE``; board rows win), or as a multiset (``EXCEPT ALL``) for a table
without one, so a second run adds nothing. The report says per table what was copied, skipped (key already in the
board) and which old-only columns were left out (they stay in ``legacy_<name>``). ``--from`` names the old file (``raw`` by default, ``history`` for the ``*_history`` move).
Nothing is ever deleted: the old tables stay as ``legacy_<name>`` until avo says "sil". This script is the one
place a board table is written whole (plan §10 rule 3).
"""
from __future__ import annotations

import argparse
import json
import sys

from RevenueChaser.core import rawstore


def _tables(board: str) -> list[str]:
    if board not in rawstore.BOARD_DBS:
        raise SystemExit(f"{board!r} is not a board file; known: {', '.join(rawstore.BOARD_DBS)}")
    return sorted(n for n, s in rawstore.MANIFEST.items() if s.get("db") == board)


def _cols(conn, schema: str, table: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_catalog = ? AND table_name = ? "
        "ORDER BY ordinal_position", [schema, table]).fetchall()]


def _count(conn, ref: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {ref}").fetchone()[0]


def _attach(conn, source: str, read_only: bool) -> None:
    path = rawstore._db_path(source).as_posix().replace("'", "''")
    conn.execute(f"ATTACH '{path}' AS src" + (" (READ_ONLY)" if read_only else ""))


def _copy(conn, src: str, dst: str, cols: list[str], has_pk: bool) -> None:
    """Rows of ``src`` not yet in ``dst``, in one transaction: by PRIMARY KEY where the table has one, otherwise as
    a multiset (``EXCEPT ALL``), so a second run adds nothing either way."""
    c = ", ".join(f'"{x}"' for x in cols)
    sql = (f"INSERT OR REPLACE INTO {dst} ({c}) SELECT {c} FROM {src}" if has_pk and dst.startswith("src.")
           else f"INSERT OR IGNORE INTO {dst} ({c}) SELECT {c} FROM {src}" if has_pk
           else f"INSERT INTO {dst} ({c}) SELECT {c} FROM {src} EXCEPT ALL SELECT {c} FROM {dst}")
    conn.execute("BEGIN")
    try:
        conn.execute(sql)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def migrate(board: str, source: str = "raw", dry_run: bool = False) -> list[dict]:
    """Copy each of ``board``'s tables from ``source``; one row per table: source/before/copied/after counts."""
    tables = _tables(board)
    if dry_run:
        with rawstore.connect(read_only=True, db=source) as conn:
            have = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
            return [{"table": t, "source": _count(conn, t) if t in have else None, "before": None, "copied": 0,
                     "after": None} for t in tables]
    out = []
    with rawstore.connect(db=board) as conn:
        for t in tables:
            rawstore._ensure_table(conn, t)   # before the ATTACH: its probe reads every catalog's ``main``
        _attach(conn, source, read_only=True)
        try:
            src_cat = "src"
            board_cat = conn.execute("SELECT current_database()").fetchone()[0]
            for t in tables:
                before = _count(conn, t)
                src_cols = _cols(conn, src_cat, t)
                if not src_cols:
                    out.append({"table": t, "source": None, "before": before, "copied": 0, "after": before})
                    continue
                n_src = _count(conn, f"src.{t}")
                board_cols = _cols(conn, board_cat, t)
                cols = [c for c in board_cols if c in src_cols]
                _copy(conn, f"src.{t}", t, cols, rawstore._has_primary_key(rawstore.MANIFEST[t]))
                after = _count(conn, t)
                out.append({"table": t, "source": n_src, "before": before, "copied": after - before, "after": after,
                            "skipped": n_src - (after - before),
                            "left_out": [c for c in src_cols if c not in board_cols]})
        finally:
            conn.execute("DETACH src")
    return out


def rename_legacy(board: str, source: str = "raw") -> list[str]:
    """Rename each moved table in ``source`` to ``legacy_<name>``, so a forgotten writer of the old path fails."""
    done = []
    with rawstore.connect(db=source) as conn:
        have = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        for t in _tables(board):
            if t in have and f"legacy_{t}" not in have:
                conn.execute(f"ALTER TABLE {t} RENAME TO legacy_{t}")
                done.append(t)
    return done


def reverse(board: str, source: str = "raw") -> list[dict]:
    """Rollback half: copy every board row back over ``legacy_<name>`` in ``source`` (rows written after the deploy
    included, ``llm_cache`` is paid for). The caller then renames ``legacy_<name>`` back BEFORE the old checkout
    starts: an old ``ensure_all_tables`` would make an empty ``<name>`` in raw and the rename-back would fail."""
    out = []
    with rawstore.connect(db=board) as conn:
        _attach(conn, source, read_only=False)
        try:
            board_cat = conn.execute("SELECT current_database()").fetchone()[0]
            for t in _tables(board):
                legacy = f"legacy_{t}"
                dst_cols = _cols(conn, "src", legacy)
                if not dst_cols or not _cols(conn, board_cat, t):
                    continue
                cols = [c for c in _cols(conn, board_cat, t) if c in dst_cols]
                _copy(conn, t, f"src.{legacy}", cols, rawstore._has_primary_key(rawstore.MANIFEST[t]))
                out.append({"table": t, "copied": _count(conn, t)})
        finally:
            conn.execute("DETACH src")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Copy a board's tables into boards/<board>.duckdb.")
    ap.add_argument("board")
    ap.add_argument("--from", dest="source", default="raw", choices=["raw", "history", "edgar"])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--rename-legacy", action="store_true")
    g.add_argument("--reverse", action="store_true")
    a = ap.parse_args(argv)
    if a.rename_legacy:
        res = rename_legacy(a.board, a.source)
    elif a.reverse:
        res = reverse(a.board, a.source)
    else:
        res = migrate(a.board, a.source, dry_run=a.dry_run)
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
