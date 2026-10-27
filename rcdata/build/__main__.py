"""``python -m RevenueChaser.rcdata.build`` — the builder CLI.

    run [--once] [--dataset X [--as-of YYYY-MM-DD]]
                                        take queued requests in order (FIFO) and run each; with --dataset, a dataset
                                        with nothing queued (or any, with --as-of) is queued first (by "cli");
                                        --once stops when the queue is empty
    backfill <dataset> --from YYYY-MM-DD [--to YYYY-MM-DD]
                                        queue one backfill request and run it now

It refuses to start while a job lock is held in the store directory or another builder runs (exit 3), and prints
one line per dataset run: the ``dataset_check`` fields. Exit 0 when every run it made ended ok or skipped, 1 when
one failed.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
import sys
import time
from pathlib import Path

from ...core import config, job_lock, rawstore
from .. import ops, serving
from . import DATASETS, base

POLL_S = 30.0


def _run_queue(dataset: str | None, once: bool) -> int:
    worst = 0
    while True:
        try:
            req = ops.next_request(dataset)
        except sqlite3.OperationalError as e:
            # ops.sqlite held past busy_timeout (a long backup, a stuck writer): wait and ask again, never die.
            print(f"ops.sqlite busy: {e}; retrying", file=sys.stderr, flush=True)
            worst = 1
            time.sleep(POLL_S)
            continue
        if req is None:
            if once:
                return worst
            time.sleep(POLL_S)
            continue
        try:
            row = base.run_request(req)
        except sqlite3.OperationalError as e:
            # The run could not write its rows (ops.sqlite locked past busy_timeout): the request is failed when the
            # file lets us, and the loop goes on with the next one.
            print(f"{req['dataset']}: ops.sqlite busy during the run: {e}", file=sys.stderr, flush=True)
            worst = 1
            try:
                ops.finish_request(req["id"], "failed", note=f"ops.sqlite busy: {e}"[:300])
            except sqlite3.OperationalError:
                pass                     # still running in the file; the next builder start fails it (S2)
            time.sleep(POLL_S)
            continue
        print(base.format_check(row), flush=True)
        if row["result"] == "failed":
            worst = 1


def _terminate(signum, frame) -> None:
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m RevenueChaser.rcdata.build")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--dataset", choices=sorted(DATASETS))
    run.add_argument("--as-of", dest="as_of", help="with --dataset: the as-of date to queue (default: today's)")
    bf = sub.add_parser("backfill")
    bf.add_argument("dataset", choices=sorted(DATASETS))
    bf.add_argument("--from", dest="start", required=True)
    bf.add_argument("--to", dest="end")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, query string included, and FRED's key is in the query.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # systemd stops the unit with SIGTERM: as SystemExit it unwinds through the run's finally, which writes the call
    # counter, instead of ending the process where it stands.
    signal.signal(signal.SIGTERM, _terminate)
    problem = base.idle_problem()
    if problem:
        print(f"builder not started: {problem}", file=sys.stderr)
        return 3
    ops.ensure()
    with job_lock.acquire(base.BUILDER_LOCK, wait=0) as got:
        if not got:
            print("builder not started: another builder holds the lock", file=sys.stderr)
            return 3
        rawstore._register_presence(Path(config.MARKET_DIR) / ops.FILE_NAME)
        for kind in serving.KINDS:
            for stale in serving.sweep_builds(kind):
                print(f"removed the stale build file {stale.name}", file=sys.stderr, flush=True)
        orphans = ops.fail_orphans()
        if any(orphans):
            print(f"failed {orphans[0]} request(s) and {orphans[1]} run(s) a dead builder left running",
                  file=sys.stderr, flush=True)
        if args.cmd == "backfill":
            params = {"mode": "backfill", "from": args.start, **({"to": args.end} if args.end else {})}
            rid = ops.enqueue(args.dataset, params, by="cli")
            req = ops.next_request(args.dataset)
            while req is not None and req["id"] != rid:      # an older request of this dataset goes first
                print(base.format_check(base.run_request(req)), flush=True)
                req = ops.next_request(args.dataset)
            row = base.run_request(req)
            print(base.format_check(row), flush=True)
            return 1 if row["result"] == "failed" else 0
        if args.dataset and (args.as_of or not ops.queued(args.dataset)):
            ops.enqueue(args.dataset, {"as_of": args.as_of} if args.as_of else None, by="cli")
        return _run_queue(args.dataset, args.once)


if __name__ == "__main__":
    sys.exit(main())
