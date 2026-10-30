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
                                        (``backfill``, ``attended``, ``run --dataset X``) wait ``DEFAULT_WAIT_S``
                                        by default and the hourly pass (``run`` with no dataset) waits none,
                                        because its timer brings the next pass anyway

The store takes one writer at a time, so the start waits that long for the job locks in the store directory, for
another builder, and for the builder lock itself; it exits 3 only when the wait runs out. It prints one line per
dataset run: the ``dataset_check`` fields. Exit 0 when every run it made ended ok or skipped, 1 when one failed or
a dataset's due step failed (its due check raised, or ops.sqlite stayed locked while queueing it).
"""
from __future__ import annotations

import argparse
import contextlib
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
# The commands a person runs at a terminal, ``run --dataset X`` among them: they wait their turn, where the hourly
# pass (``run`` with no dataset) leaves it to its timer.
PERSON_COMMANDS = ("backfill", "attended")


def at_a_terminal() -> bool:
    """True when a person could answer a prompt here (the WRDS login and the Duo push)."""
    return sys.stdin is not None and sys.stdin.isatty()


def default_wait_s(cmd: str, dataset: str | None = None) -> float:
    """How long ``cmd`` waits for the writer turn when ``--wait`` was not given."""
    return DEFAULT_WAIT_S if cmd in PERSON_COMMANDS or dataset else 0.0


def _own_requests_only(dataset: str) -> str | None:
    """``base.ATTENDED_BY`` for an attended dataset, else None: what a CLI run passes as ``ops``' ``by``.

    A person's own ``backfill`` / ``run --dataset`` runs their own request, not one a page left for the attended
    runner, which would spend their WRDS login and the day cap on somebody else's params (review SHOULD-2).
    Elsewhere the older request still goes first, as it always has.
    """
    from . import DATASETS
    ds = DATASETS.get(dataset)
    return base.ATTENDED_BY if ds is not None and base.attended(ds) else None


def _attended_queued(dataset: str | None) -> bool:
    """True while an attended request waits (of ``dataset``, when given)."""
    names = base.attended_names() if dataset is None else {dataset}
    return any(r["status"] == "queued" and r["dataset"] in names
               for r in ops.queue_view(finished=0)["active"])


def _say_waiting(problem: str, said: list) -> None:
    """Name what the turn is waiting for, and say it again whenever that changes (N7). ``said`` is the one-cell
    memory of what was printed last, shared by the gate and the lock so neither repeats the other."""
    if problem != said[0]:
        print(f"waiting for the writer turn: {problem}", file=sys.stderr, flush=True)
        said[0] = problem


def _wait_for_turn(wait: float, said: list | None = None) -> str | None:
    """None once nothing else holds the store, or the problem that still stands when ``wait`` seconds are up.

    The problem is read again every ``WAIT_POLL_S`` and printed through ``_say_waiting``, so a waiting terminal
    always names the holder it is actually waiting for. Nothing is queued or run before it clears.
    """
    deadline = time.monotonic() + max(0.0, wait)
    said = said if said is not None else [None]
    while True:
        problem = base.idle_problem()
        if problem is None:
            return None
        if time.monotonic() >= deadline:
            return problem
        _say_waiting(problem, said)
        time.sleep(min(WAIT_POLL_S, max(0.0, deadline - time.monotonic())))


def _take_the_turn(stack: contextlib.ExitStack, wait: float) -> tuple[bool, str | None]:
    """Hold the builder lock with nothing else on the store: ``(True, None)``, or ``(False, why)`` when the wait
    ran out.

    The gate (``base.idle_problem``) and the lock are one loop, so a job lock taken while the lock was being
    waited for is seen before the builder starts, and the window between them stays as short as it was when
    neither waited (N1). A lock another builder holds is named while waiting too, like a gate holder.

    ``ops.ensure`` is inside, after the gate, as the first write of the ``run`` and ``backfill`` paths — but it is
    no longer the first touch of ``ops.sqlite`` for ``attended``, whose queue check runs before the turn
    (``ops._open`` ensures the file on a process's first use). That file is not the DuckDB writer this gate
    protects, and the dashboard ensures it on every request.
    """
    deadline = time.monotonic() + max(0.0, wait)
    said: list = [None]
    while True:
        problem = _wait_for_turn(max(0.0, deadline - time.monotonic()), said)
        if problem is None:
            ops.ensure()
            if stack.enter_context(job_lock.acquire(base.BUILDER_LOCK, wait=0)):
                return True, None
            stack.close()                       # drop that failed acquire before the next try
            problem = "another builder holds the lock"
        if time.monotonic() >= deadline:
            return False, problem
        _say_waiting(problem, said)
        time.sleep(min(WAIT_POLL_S, max(0.0, deadline - time.monotonic())))


def _run_queue(dataset: str | None, once: bool, *, skip=(), only=None, by=None,
               attended_ok: bool = False) -> int:
    worst = 0
    while True:
        try:
            req = ops.next_request(dataset, skip=skip, only=only, by=by)
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
                             f"{DEFAULT_WAIT_S:.0f} for a person's command, 0 for the hourly run)")
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
    if args.cmd == "attended":
        if not at_a_terminal():
            # N4: the WRDS login and the Duo push need a person. Refusing a runner with no terminal turns the
            # convention into a guard, so a mis-wired timer cannot reach them; the requests stay queued.
            print("builder not started: attended needs a terminal (a person logs in to WRDS and confirms Duo)",
                  file=sys.stderr)
            return 3
        if not _attended_queued(args.dataset):
            # N2: otherwise it would sit on the writer turn for the whole wait and then exit without a word.
            print("no attended request queued", file=sys.stderr)
            return 0
    wait = default_wait_s(args.cmd, getattr(args, "dataset", None)) if args.wait is None else max(0.0, args.wait)
    with contextlib.ExitStack() as stack:
        got, problem = _take_the_turn(stack, wait)
        if not got:
            print(f"builder not started: {problem}", file=sys.stderr)
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
                mine = _own_requests_only(name)
                rid = ops.enqueue(name, params, by="cli")
                req = ops.next_request(name, by=mine)
                while req is not None and req["id"] != rid:      # an older request of this dataset goes first
                    print(base.format_check(base.run_request(req)), flush=True)
                    req = ops.next_request(name, by=mine)
                row = base.run_request(req)
                print(base.format_check(row), flush=True)
                worst = max(worst, int(row["result"] == "failed"))
            return worst
        mine = _own_requests_only(args.dataset) if args.dataset else None
        if args.dataset and (args.as_of or args.allow_shrink or scope or not ops.queued(args.dataset, by=mine)):
            params = {**({"as_of": args.as_of} if args.as_of else {}),
                      **({"allow_shrink": True} if args.allow_shrink else {}), **scope}
            ops.enqueue(args.dataset, params or None, by="cli")
        due_failed = 0
        if not args.dataset:
            for name, why, _, error in due.enqueue_due():
                print(f"{name}: {why}", file=sys.stderr, flush=True)
                due_failed = due_failed or int(error)
        # A due step that failed still drains the queue, then fails the pass, so an hourly error is not an exit 0.
        # Without --dataset this is the hourly pass, which leaves the attended requests queued for the terminal
        # that may run them.
        skip = () if args.dataset else base.attended_names()
        return max(_run_queue(args.dataset, args.once, skip=skip, by=mine), due_failed)


if __name__ == "__main__":
    sys.exit(main())
