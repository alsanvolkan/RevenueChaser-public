"""The skeleton both forecast writer CLIs share (#110): ``scripts/forecast_outcomes.py`` and
``scripts/forecast_baselines.py`` are each a ``WriterCli`` and a call to ``main``.

The parser (``--nightly`` | ``--full``, ``--dry-run``, ``--report``), the one-writer lock and its refusal, the report
and the exit codes live here once, so a change to any of them is made in one place. Exit codes: 0 done, 1 the writer
raised (the reason on stderr), 2 a bad command line, ``BUSY_EXIT`` another run holds the lock and nothing was written.
A run refused for the lock still writes a ``{"status": "busy"}`` report, so yesterday's success JSON cannot be read as
tonight's (S-3). ``--dry-run`` writes nothing, so it takes no lock and still answers while the task runs.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from RevenueChaser.core import job_lock, ledger

BUSY_EXIT = 3


@dataclass(frozen=True)
class WriterCli:
    prog: str                       # the module name, e.g. "forecast_outcomes"
    what: str                       # how stderr names it, e.g. "forecast outcomes"
    lock: str                       # the ``job_lock`` name
    run: Callable[..., dict]        # called as ``run(full=, write=, run_id=)``; a lambda, so a patched writer is seen
    description: str
    nightly_help: str
    full_help: str


def write_report(path: str | None, res: dict) -> None:
    """Write ``res`` to ``--report``: the result of a run, or the ``busy`` record of one that wrote nothing (S-3)."""
    if path:
        Path(path).write_text(json.dumps(res, default=str), encoding="utf-8")


def _compute(cli: WriterCli, args) -> int:
    """The run itself: compute, write the report, print the result JSON."""
    try:
        res = cli.run(full=bool(args.full), write=not args.dry_run, run_id=ledger.new_run_id())
    except Exception as exc:  # noqa: BLE001 - the task's log and exit code carry the reason
        print(f"{cli.what} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    write_report(args.report, res)
    print(json.dumps(res, default=str))
    return 0


def main(cli: WriterCli, argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=cli.prog, description=cli.description)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--nightly", action="store_true", help=cli.nightly_help)
    mode.add_argument("--full", action="store_true", help=cli.full_help)
    ap.add_argument("--dry-run", action="store_true", help="compute and count only, write nothing")
    ap.add_argument("--report", default=None, help="write the result JSON to this path")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2

    if args.dry_run:                       # writes nothing: no lock, so it answers while the task runs
        return _compute(cli, args)
    with job_lock.acquire(cli.lock, wait=0) as got:
        if not got:
            pid = dict(job_lock.holders()).get(cli.lock, 0)
            who = f" (pid {pid})" if pid else ""
            print(f"{cli.what}: another run{who} holds the {cli.lock} lock; nothing was written.", file=sys.stderr)
            # Over whatever the last success left there: a reader must not read yesterday's JSON as tonight's (S-3).
            write_report(args.report, {"status": "busy", "holder_pid": pid})
            return BUSY_EXIT
        return _compute(cli, args)
