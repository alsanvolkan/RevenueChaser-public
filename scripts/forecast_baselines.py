"""Forecast baselines CLI (#25 HR-1): the ``RC-Forecast-Baselines`` nightly and the attended first fill.

``--nightly`` recomputes only the baseline rows still open (chained on RC-Forecast-Outcomes, so the two raw.duckdb
writers of the night do not overlap); ``--full`` recomputes every date (the first fill, or after a rule change).
``--dry-run`` computes and counts without writing. Reads ``forecast_ledger``, the equity-universe change history and
the stored bars, writes ``forecast_baselines`` only (``core/baselines.py``), no network call.

One writer at a time (``job_lock`` name ``"forecast_baselines"``, #26): a run that holds the lock makes this exit
``BUSY_EXIT`` with one line on stderr and no table write; ``--report`` still gets a ``{"status": "busy"}`` record, so
yesterday's success JSON cannot be read as tonight's (S-3). ``--dry-run`` writes nothing, so it takes no lock and
still answers while the task runs.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from RevenueChaser.core import baselines, job_lock, ledger

BUSY_EXIT = 3


def _write_report(path: str | None, res: dict) -> None:
    """Write ``res`` to ``--report``: the result of a run, or the ``busy`` record of one that wrote nothing (S-3)."""
    if path:
        Path(path).write_text(json.dumps(res, default=str), encoding="utf-8")


def _compute(args) -> int:
    """The run itself: compute, write the report, print the result JSON."""
    try:
        res = baselines.run(full=bool(args.full), write=not args.dry_run, run_id=ledger.new_run_id())
    except Exception as exc:  # noqa: BLE001 - the task's log and exit code carry the reason
        print(f"forecast baselines failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    _write_report(args.report, res)
    print(json.dumps(res, default=str))
    return 0


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

    if args.dry_run:                       # writes nothing: no lock, so it answers while the task runs
        return _compute(args)
    with job_lock.acquire("forecast_baselines", wait=0) as got:
        if not got:
            pid = dict(job_lock.holders()).get("forecast_baselines", 0)
            who = f" (pid {pid})" if pid else ""
            print(f"forecast baselines: another run{who} holds the forecast_baselines lock; nothing was written.",
                  file=sys.stderr)
            # Over whatever the last success left there: a reader must not read yesterday's JSON as tonight's (S-3).
            _write_report(args.report, {"status": "busy", "holder_pid": pid})
            return BUSY_EXIT
        return _compute(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
