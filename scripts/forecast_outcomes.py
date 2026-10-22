"""Forecast outcomes CLI (#380 S0): the ``RC-Forecast-Outcomes`` nightly and the attended first fill.

``--nightly`` recomputes every ledger pick's outcome rows that are not closed yet (chained on RC-Daily-Bars, so it
reads tonight's D01 bars); ``--full`` recomputes every row (the first fill, or after a rule change). ``--dry-run``
computes and counts without writing. Reads ``forecast_ledger`` and the stored bars, writes ``forecast_outcomes``
only (``core/outcomes.py``), no network call.

One writer at a time (``job_lock`` name ``"forecast_outcomes"``, #26): a run that holds the lock makes this exit
``BUSY_EXIT`` with one line on stderr and no write. ``--dry-run`` writes nothing, so it takes no lock and still
answers while the task runs.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from RevenueChaser.core import job_lock, ledger, outcomes

BUSY_EXIT = 3


def _compute(args) -> int:
    """The run itself: compute, write the report, print the result JSON."""
    try:
        res = outcomes.run(full=bool(args.full), write=not args.dry_run, run_id=ledger.new_run_id())
    except Exception as exc:  # noqa: BLE001 - the task's log and exit code carry the reason
        print(f"forecast outcomes failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.report:
        Path(args.report).write_text(json.dumps(res, default=str), encoding="utf-8")
    print(json.dumps(res, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="forecast_outcomes", description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--nightly", action="store_true", help="recompute the rows that are not closed yet")
    mode.add_argument("--full", action="store_true", help="recompute every row")
    ap.add_argument("--dry-run", action="store_true", help="compute and count only, write nothing")
    ap.add_argument("--report", default=None, help="write the result JSON to this path")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2

    if args.dry_run:                       # writes nothing: no lock, so it answers while the task runs
        return _compute(args)
    with job_lock.acquire("forecast_outcomes", wait=0) as got:
        if not got:
            pid = dict(job_lock.holders()).get("forecast_outcomes", 0)
            who = f" (pid {pid})" if pid else ""
            print(f"forecast outcomes: another run{who} holds the forecast_outcomes lock; nothing was written.",
                  file=sys.stderr)
            return BUSY_EXIT
        return _compute(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
