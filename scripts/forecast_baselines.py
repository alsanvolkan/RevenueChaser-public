"""Forecast baselines CLI (#25 HR-1): the ``RC-Forecast-Baselines`` nightly and the attended first fill.

``--nightly`` recomputes only the baseline rows still open (chained on RC-Forecast-Outcomes, so the two raw.duckdb
writers of the night do not overlap); ``--full`` recomputes every date (the first fill, or after a rule change).
``--dry-run`` computes and counts without writing. Reads ``forecast_ledger``, the equity-universe change history and
the stored bars, writes ``forecast_baselines`` only (``core/baselines.py``), no network call.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from RevenueChaser.core import baselines, ledger


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="forecast_baselines", description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--nightly", action="store_true", help="recompute the rows that are not closed yet")
    mode.add_argument("--full", action="store_true", help="recompute every date")
    ap.add_argument("--dry-run", action="store_true", help="compute and count only, write nothing")
    ap.add_argument("--report", default=None, help="write the result JSON to this path")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2

    try:
        res = baselines.run(full=bool(args.full), write=not args.dry_run, run_id=ledger.new_run_id())
    except Exception as exc:  # noqa: BLE001 - the task's log and exit code carry the reason
        print(f"forecast baselines failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.report:
        Path(args.report).write_text(json.dumps(res, default=str), encoding="utf-8")
    print(json.dumps(res, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
