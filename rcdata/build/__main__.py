"""``python -m RevenueChaser.rcdata.build`` — the builder CLI.

    run [--once] [--dataset X [--as-of YYYY-MM-DD] [--allow-shrink]]
                                        take queued requests in order (FIFO) and run each; without --dataset, every
                                        dataset that is due (``due.py``) is queued first (by "schedule", once);
                                        with --dataset, a dataset with nothing queued (or any, with --as-of) is
                                        queued first (by "cli"); --once stops when the queue is empty
    backfill <dataset> [<dataset> ...] [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--allow-shrink]
                                        queue one backfill request per dataset and run them now, in the order given,
                                        in this one process (so the WRDS datasets share one login); names may be
                                        separated by commas too; --from is required by a dataset that has no start
                                        of its own (the market datasets); the spine's start at their source's
                                        beginning

It refuses to start while a job lock is held in the store directory or another builder runs (exit 3), and prints
one line per dataset run: the ``dataset_check`` fields. Exit 0 when every run it made ended ok or skipped, 1 when
one failed or a dataset's due step failed (its due check raised, or ops.sqlite stayed locked while queueing it).
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
from . import DATASETS, base, due

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
    run.add_argument("--as-of", dest="as_of", help="with --dataset: the as-of date to queue (default: the dataset's own, for "
                     "uw/spot_expiry_strike the latest complete session; a later day is refused)")
    shrink_help = ("let this build swap in a smaller table of this dataset's own (a source removed on purpose); "
                   "without it a table that empties or loses more than 1%% of its rows refuses the swap, and "
                   "another dataset's table keeps that guard even with it")
    run.add_argument("--allow-shrink", action="store_true", help="with --dataset: " + shrink_help)
    bf = sub.add_parser("backfill")
    bf.add_argument("datasets", nargs="+", metavar="dataset")
    bf.add_argument("--from", dest="start")
    bf.add_argument("--to", dest="end")
    bf.add_argument("--allow-shrink", action="store_true", help=shrink_help)
    args = p.parse_args(argv)
    if args.cmd == "run" and not args.dataset and (args.as_of or args.allow_shrink):
        # They only shape the request queued for --dataset; without it they would be dropped silently (F5).
        p.error("--as-of and --allow-shrink need --dataset")
    if args.cmd == "backfill":
        args.datasets = [n.strip() for a in args.datasets for n in a.split(",") if n.strip()]
        unknown = [n for n in args.datasets if n not in DATASETS]
        if unknown or not args.datasets:
            p.error(f"unknown dataset(s) {unknown}; known: {', '.join(sorted(DATASETS))}")
        need = [n for n in args.datasets if getattr(DATASETS[n], "backfill_needs_from", True)]
        if need and not args.start:
            p.error(f"--from is required for {', '.join(need)}")

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
            params = {"mode": "backfill", **({"from": args.start} if args.start else {}),
                      **({"to": args.end} if args.end else {}),
                      **({"allow_shrink": True} if args.allow_shrink else {})}
            worst = 0
            for name in args.datasets:
                rid = ops.enqueue(name, params, by="cli")
                req = ops.next_request(name)
                while req is not None and req["id"] != rid:      # an older request of this dataset goes first
                    print(base.format_check(base.run_request(req)), flush=True)
                    req = ops.next_request(name)
                row = base.run_request(req)
                print(base.format_check(row), flush=True)
                worst = max(worst, int(row["result"] == "failed"))
            return worst
        if args.dataset and (args.as_of or args.allow_shrink or not ops.queued(args.dataset)):
            params = {**({"as_of": args.as_of} if args.as_of else {}),
                      **({"allow_shrink": True} if args.allow_shrink else {})}
            ops.enqueue(args.dataset, params or None, by="cli")
        due_failed = 0
        if not args.dataset:
            for name, why, _, error in due.enqueue_due():
                print(f"{name}: {why}", file=sys.stderr, flush=True)
                due_failed = due_failed or int(error)
        # A due step that failed still drains the queue, then fails the pass, so an hourly error is not an exit 0.
        return max(_run_queue(args.dataset, args.once), due_failed)


if __name__ == "__main__":
    sys.exit(main())
