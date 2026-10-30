"""``python -m RevenueChaser.rcdata.build`` — the builder CLI.

    run [--once] [--dataset X [--as-of YYYY-MM-DD] [--allow-shrink] [--tiers A,B] [--names KO,MSFT]]
                                        take queued requests in order (FIFO) and run each; without --dataset, every
                                        dataset that is due (``due.py``) is queued first (by "schedule", once);
                                        with --dataset, a dataset with nothing queued (or any, with --as-of) is
                                        queued first (by "cli"); --once stops when the queue is empty
    backfill <dataset> [<dataset> ...] [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--allow-shrink]
             [--tiers A,B] [--names KO,MSFT]
                                        queue one backfill request per dataset and run them now, in the order given,
                                        in this one process (so the WRDS datasets share one login); names may be
                                        separated by commas too; --from is required by a dataset that has no start
                                        of its own (the market datasets); the spine's start at their source's
                                        beginning
    --tiers / --names / --include       what the request asks for instead of the dataset's own set: the spine's
                                        tiers (A,B,C,D), an explicit ticker list, and the extra groups
                                        (anchors = the benchmarks and rotation ETFs, watchlist, cover_classes).
                                        A narrowed request gets only what it names (Karar 82); a dataset that
                                        cannot narrow its fetch takes the request and says so on its check row

    attended [--dataset X]              drain the queued requests of the attended datasets (WRDS: a person logs in
                                        and confirms Duo) and stop. The only runner that may take someone else's
                                        request for one of them, so a Fetch press waits in the queue for this
                                        terminal instead of being refused; --dataset limits it to one of them
    --wait SECONDS                      how long to wait for the store's writer turn when a job lock is held or
                                        another builder runs, instead of giving up at once. A person's commands
                                        (``backfill``, ``attended``) wait ``DEFAULT_WAIT_S`` by default and
                                        ``run`` waits none, because its timer brings the next pass anyway

The store takes one writer at a time, so the start waits that long for the job locks in the store directory, for
another builder, and for the builder lock itself; it exits 3 only when the wait runs out. It prints one line per
dataset run: the ``dataset_check`` fields. Exit 0 when every run it made ended ok or skipped, 1 when one failed or
a dataset's due step failed (its due check raised, or ops.sqlite stayed locked while queueing it).
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
# How often the start asks again whose turn it is, and how long a person's command waits for it by default. A
# backfill is started by hand and the hourly pass is minutes at most, so waiting beats exit 3 and a second attempt.
WAIT_POLL_S = 5.0
DEFAULT_WAIT_S = 1800.0
# The commands a person runs at a terminal: they wait their turn, where the hourly ``run`` leaves it to its timer.
ATTENDED_COMMANDS = ("backfill", "attended")


def default_wait_s(cmd: str) -> float:
    """How long ``cmd`` waits for the writer turn when ``--wait`` was not given."""
    return DEFAULT_WAIT_S if cmd in ATTENDED_COMMANDS else 0.0


def _wait_for_turn(wait: float) -> str | None:
    """None once nothing else holds the store, or the problem that still stands when ``wait`` seconds are up.

    The problem is read again every ``WAIT_POLL_S``; the first one is printed so a waiting terminal says what it
    waits for, and nothing is queued or run before it clears.
    """
    deadline = time.monotonic() + max(0.0, wait)
    said = False
    while True:
        problem = base.idle_problem()
        if problem is None:
            return None
        if time.monotonic() >= deadline:
            return problem
        if not said:
            print(f"waiting for the writer turn: {problem}", file=sys.stderr, flush=True)
            said = True
        time.sleep(min(WAIT_POLL_S, max(0.0, deadline - time.monotonic())))


def _run_queue(dataset: str | None, once: bool, *, skip=(), only=None, attended_ok: bool = False) -> int:
    worst = 0
    while True:
        try:
            req = ops.next_request(dataset, skip=skip, only=only)
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
            row = base.run_request(req, attended_ok=attended_ok)
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
    scope_help = ("with --dataset: ask only these spine tiers (A,B,C,D) / only these tickers, instead of the "
                  "dataset's own set; a dataset that cannot narrow its fetch says so on its check row (#265)")
    group_help = ("the extra groups to ask besides the tiers and names: anchors (the benchmarks and rotation "
                  "ETFs), watchlist, cover_classes. A narrowed request gets only what it names; a request that "
                  "names nothing is the daily run and gets all three (Karar 82)")
    run.add_argument("--tiers", help=scope_help)
    run.add_argument("--names", help=scope_help)
    run.add_argument("--include", help=group_help)
    at = sub.add_parser("attended")
    at.add_argument("--dataset", choices=sorted(base.attended_names()), help="only this attended dataset")
    bf = sub.add_parser("backfill")
    bf.add_argument("datasets", nargs="+", metavar="dataset")
    bf.add_argument("--from", dest="start")
    bf.add_argument("--to", dest="end")
    bf.add_argument("--allow-shrink", action="store_true", help=shrink_help)
    bf.add_argument("--tiers", help=scope_help)
    bf.add_argument("--names", help=scope_help)
    bf.add_argument("--include", help=group_help)
    for sp in (run, at, bf):
        sp.add_argument("--wait", type=float, default=None, metavar="SECONDS",
                        help="how long to wait for the store's writer turn before exit 3 (default: "
                             f"{DEFAULT_WAIT_S:.0f} for backfill and attended, 0 for run)")
    args = p.parse_args(argv)
    if args.cmd == "run" and not args.dataset and (args.as_of or args.allow_shrink or args.tiers or args.names
                                                   or args.include):
        # They only shape the request queued for --dataset; without it they would be dropped silently (F5).
        p.error("--as-of, --allow-shrink, --tiers, --names and --include need --dataset")
    scope = {k: [n.strip() for n in v.split(",") if n.strip()]
             for k, v in (("tiers", getattr(args, "tiers", None)), ("names", getattr(args, "names", None)),
                          ("include", getattr(args, "include", None))) if v}
    try:
        asked = base.parse_scope(scope)                 # the builder's own reader, so the CLI cannot differ from it
    except ValueError as e:
        p.error(str(e))
    scope = {k: list(v) for k, v in (("tiers", asked.tiers), ("names", asked.names),
                                     ("include", asked.include)) if v}
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
    wait = default_wait_s(args.cmd) if args.wait is None else max(0.0, args.wait)
    deadline = time.monotonic() + wait
    problem = _wait_for_turn(wait)
    if problem:
        print(f"builder not started: {problem}", file=sys.stderr)
        return 3
    ops.ensure()
    # The turn can still be taken between the gate and the lock, so the lock gets what is left of the wait.
    with job_lock.acquire(base.BUILDER_LOCK, wait=max(0.0, deadline - time.monotonic())) as got:
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
        if args.cmd == "attended":
            # A person is at this terminal (the WRDS login and Duo), so this runner may run what others queued.
            return _run_queue(args.dataset, once=True, only=base.attended_names(), attended_ok=True)
        if args.cmd == "backfill":
            params = {"mode": "backfill", **({"from": args.start} if args.start else {}),
                      **({"to": args.end} if args.end else {}),
                      **({"allow_shrink": True} if args.allow_shrink else {}), **scope}
            worst = 0
            for name in args.datasets:
                rid = ops.enqueue(name, params, by="cli")
                req = ops.next_request(name)
                while req is not None and req["id"] != rid:      # an older request of this dataset goes first
                    print(base.format_check(base.run_request(req, attended_ok=True)), flush=True)
                    req = ops.next_request(name)
                row = base.run_request(req, attended_ok=True)
                print(base.format_check(row), flush=True)
                worst = max(worst, int(row["result"] == "failed"))
            return worst
        if args.dataset and (args.as_of or args.allow_shrink or scope or not ops.queued(args.dataset)):
            params = {**({"as_of": args.as_of} if args.as_of else {}),
                      **({"allow_shrink": True} if args.allow_shrink else {}), **scope}
            ops.enqueue(args.dataset, params or None, by="cli")
        due_failed = 0
        if not args.dataset:
            for name, why, _, error in due.enqueue_due():
                print(f"{name}: {why}", file=sys.stderr, flush=True)
                due_failed = due_failed or int(error)
        # A due step that failed still drains the queue, then fails the pass, so an hourly error is not an exit 0.
        # Without --dataset this is the hourly pass: it leaves the attended requests queued for the terminal that
        # may run them. With one, a person named it, so this runner is that terminal.
        skip = () if args.dataset else base.attended_names()
        return max(_run_queue(args.dataset, args.once, skip=skip, attended_ok=bool(args.dataset)), due_failed)


if __name__ == "__main__":
    sys.exit(main())
