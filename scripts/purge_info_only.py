"""Remove the ``info_only`` rows AG writes into ``ticker_payload_cache`` (#150 U13); the code-rollback step.

    python -m RevenueChaser.scripts.purge_info_only            # count only
    python -m RevenueChaser.scripts.purge_info_only --apply    # delete them

A flagged ``info_only`` row has ``annual: []``. Code from before U13 reads such a row as a full payload (it has no
``info_only`` check), so after a revert Financials and the old AG would show a name with no statements.
Run this after the revert, with the dashboard stopped and a fresh backup. A name loses nothing it had: its row is
fetched again by the next full fetch. A row that merged a refreshed ``.info`` into a full payload carries no flag and
stays (the old code reads it as the full payload it is). ``restore_store --latest raw`` is the other way back; it
also drops every write since that backup.
"""
from __future__ import annotations

import argparse
import sys

from RevenueChaser.core import rawstore

_WHERE = "json_extract(payload, '$.info_only') = 'true'"


def count() -> int:
    return rawstore.query(f"SELECT count(*) AS n FROM ticker_payload_cache WHERE {_WHERE}")[0]["n"]


def purge() -> int:
    """Delete every ``info_only`` row; return how many there were."""
    peers = rawstore._peer_pids(rawstore.DB_PATH)
    if peers:
        raise RuntimeError(f"store in use by RevenueChaser process(es) {peers}; stop them first")
    n = count()
    if n:
        rawstore.execute(f"DELETE FROM ticker_payload_cache WHERE {_WHERE}")
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Remove info_only rows from ticker_payload_cache.")
    ap.add_argument("--apply", action="store_true", help="delete them (default: only count)")
    args = ap.parse_args(argv)
    if not args.apply:
        print(f"{count()} info_only row(s) in ticker_payload_cache (use --apply to delete)")
        return 0
    try:
        n = purge()
    except RuntimeError as exc:
        print(f"purge FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"deleted {n} info_only row(s); {rawstore.count('ticker_payload_cache')} row(s) left")
    return 0


if __name__ == "__main__":
    sys.exit(main())
