"""Index lists CLI (#302, D24): the ``RC-Index-Lists`` weekly task and the attended first SmallCap 600 fill.

``--weekly`` fetches every stored slug once (three UW requests; sp600 from the identity cache's last accepted
S&P 1500 snapshot) and writes each list that clears its floor; the store records joins and leaves in
``index_constituents_changes``. ``--first-fill sp600`` also writes SmallCap 600 when it has no rows yet; that first
fill needs the user's OK for the run. One step, one lock (``core/index_registry.refresh_lists``); the dashboard's
``index_lists`` job runs the same step.
"""
from __future__ import annotations

import argparse
import json
import sys

from RevenueChaser.core import index_registry, ledger


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="index_lists", description=__doc__)
    ap.add_argument("--weekly", action="store_true", required=True, help="write every stored index list")
    ap.add_argument("--first-fill", choices=sorted(index_registry.FIRST_FILL_GATED), action="append", default=[],
                    help="also write this gated list when it has no rows yet (needs the user's OK for the run)")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2

    res = index_registry.refresh_lists(run_id=ledger.new_run_id(), first_fill=args.first_fill)
    print(json.dumps(res, default=str))
    print(index_registry.summary_line(res), file=sys.stderr)
    if res.get("locked") or index_registry.lists_ok(res):
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
