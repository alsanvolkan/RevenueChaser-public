"""CLI for the identity master: ``python -m RevenueChaser.core.identity --refresh [--limit N] ...``.

Exit codes: ``0`` when the run finished, ``1`` when it recorded an error, ``BUSY_EXIT`` (3) when another identity
refresh holds the ``"identity"`` lock — nothing was fetched or written then.
"""
from __future__ import annotations

import argparse
import sys

from . import refresh as identity_refresh

BUSY_EXIT = 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m RevenueChaser.core.identity", description=__doc__)
    ap.add_argument("--refresh", action="store_true", help="run one identity refresh (fetch deltas, rebuild the master)")
    ap.add_argument("--limit", type=int, default=None, help="only the first N universe tickers")
    ap.add_argument("--sources", default=None,
                    help="comma list from: " + ", ".join(sorted(identity_refresh.GROUPS)) + " (default: all)")
    ap.add_argument("--yahoo-limit", type=int, default=None, help="max Yahoo sector lookups this run (default 200; 0 = no cap)")
    args = ap.parse_args(argv)
    if not args.refresh:
        ap.print_help()
        return 2
    if args.limit is not None and args.limit <= 0:
        ap.error("--limit must be positive")
    sources = [s.strip() for s in args.sources.split(",") if s.strip()] if args.sources else None
    rec = identity_refresh.refresh(limit=args.limit, sources=sources, yahoo_limit=args.yahoo_limit,
                                   progress=lambda m: print(m, file=sys.stderr, flush=True))
    if rec.get("status") == "busy":                 # another refresh holds the lock: nothing was written
        print(identity_refresh.summary(rec), file=sys.stderr)
        return BUSY_EXIT
    print(identity_refresh.summary(rec))
    for name, s in rec["sources"].items():
        if s.get("selected"):
            print(f"  {name:16s} {s['status']:8s} rows={s['rows']} fetched={s['fetched']} {s['seconds']}s"
                  + (f"  {s['error']}" if s.get("error") else ""))
    print(f"run {rec['run_id']} status={rec['status']}")
    return 0 if rec["status"] != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
