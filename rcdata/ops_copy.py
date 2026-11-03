"""``python -m RevenueChaser.rcdata.ops_copy`` — copy ``ops.sqlite`` into Redis, once (master plan Order 3).

Run it with the app stopped (nothing may write the file after it is read) and ``RC_REDIS_URL`` set. The source is
``<RC_MARKET_DIR>/ops.sqlite`` unless ``--sqlite`` names another file; the target is the Redis namespace of the same
``RC_MARKET_DIR`` (``ops_redis.namespace``), so source and target come from one setting.

- The file is opened read-only (``file:...?mode=ro``) and never written, renamed or deleted.
- ``--dry-run`` prints the rows per table and the open issues per dataset and kind, and writes nothing.
- A namespace that already holds keys is refused (exit 2), unless ``--force``.
- Every row is copied as it is, ids and resolved rows included; nothing is filtered (the price issues stay).
- Afterwards every table is read back from Redis and compared with the file, row for row; any difference is printed
  and the exit code is 1. 0 means the copy is verified.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import ops

# Every table but the schema stamp, which is written by ``ensure`` once the rows are in.
COPIED = tuple(t for t in ops.TABLES if t != "ops_schema")


def _open_read_only(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _table_rows(con: sqlite3.Connection, table: str) -> list[dict]:
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
        return []
    return [dict(r) for r in con.execute(f"SELECT * FROM {table} ORDER BY {', '.join(ops._primary_key(table))}")]


def _open_kinds(rows: list[dict]) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for r in rows:
        if r["resolved_on"] is None:
            out[(r["dataset"], r["kind"])] = out.get((r["dataset"], r["kind"]), 0) + 1
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m RevenueChaser.rcdata.ops_copy", description=__doc__.split("\n")[0])
    p.add_argument("--sqlite", type=Path, default=None, help="the ops.sqlite to copy (default: <RC_MARKET_DIR>/ops.sqlite)")
    p.add_argument("--dry-run", action="store_true", help="count what would be copied and write nothing")
    p.add_argument("--force", action="store_true", help="copy even though Redis already holds this market root's keys")
    args = p.parse_args(argv)
    if ops.backend() != "redis":
        print(f"refused: {ops.REDIS_URL_ENV} is not set; it names the Redis to copy into", flush=True)
        return 2
    path = Path(args.sqlite or ops.ops_path())
    if not path.is_file():
        print(f"refused: no file at {path}", flush=True)
        return 2
    from . import ops_redis
    con = _open_read_only(path)
    try:
        found = con.execute("SELECT max(version) FROM ops_schema").fetchone()[0]
        if found != ops.OPS_SCHEMA_VERSION:
            print(f"refused: {path} is ops schema {found}; this checkout copies {ops.OPS_SCHEMA_VERSION}", flush=True)
            return 2
        source = {t: _table_rows(con, t) for t in COPIED}
    finally:
        con.close()

    ns, client = ops_redis.namespace(), ops_redis._client()
    held = sum(1 for _ in client.scan_iter(match=f"{ns}:*", count=1000))
    print(f"source: {path} (ops schema {found})", flush=True)
    print(f"target: namespace {ns}, {held} key(s) there now, {client.dbsize()} in the database", flush=True)
    for t in COPIED:
        print(f"{t}: {len(source[t])}", flush=True)
    kinds = _open_kinds(source["build_issues"])
    for (dataset, kind), n in sorted(kinds.items()):
        print(f"open {dataset} {kind}: {n}", flush=True)
    print(f"open issues: {sum(kinds.values())}", flush=True)
    if args.dry_run:
        print("dry run: nothing written", flush=True)
        return 0
    if held and not args.force:
        print(f"refused: Redis already holds {held} key(s) of {ns}; nothing written (--force copies over them)",
              flush=True)
        return 2

    for t in COPIED:
        ops_redis.put_rows(t, source[t])
    ops_redis.ensure()

    bad = 0
    for t in COPIED:
        got = ops_redis.rows(t)
        if got != source[t]:
            bad += 1
            print(f"MISMATCH {t}: {len(got)} row(s) in Redis, {len(source[t])} in the file", flush=True)
    got_kinds = {(ds, k["kind"]): k["count"] for ds in {d for d, _ in kinds} for k in ops_redis.open_issue_kinds(ds)}
    if got_kinds != kinds:
        bad += 1
        print(f"MISMATCH open issues: Redis {sum(got_kinds.values())}, file {sum(kinds.values())}", flush=True)
    if bad:
        print(f"copy NOT verified: {bad} difference(s); the file is untouched, so the app can stay on it", flush=True)
        return 1
    print(f"copy verified: {sum(len(v) for v in source.values())} rows, {sum(kinds.values())} open issues", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
