"""What is due: ``run`` without ``--dataset`` queues every due dataset before it drains the queue.

A dataset is due (``is_due``) when nothing holds it and its last run is old enough:

- an attended dataset (WRDS: a person logs in and confirms Duo) is never due: it runs only from the CLI (PR 5);
- a ``by_request`` dataset (the ledger's freeze, PR 10) is never due: only a board's freeze queues it;
- a dataset that ``starts_by_hand`` (every spine dataset) is not due until it has built once (a ``changed`` or
  ``unchanged`` check from a CLI run or a Fetch), so the first hourly pass after a deploy does not start it before
  the attended steps, and a first attempt that failed is not retried every hour before a person has seen it work;
- a pause in ``dataset_schedule`` holds it: its request is not queued; ``base.run_request`` skips a request of a
  paused dataset that was queued anyway (a Fetch, the CLI's), with a ``skipped: paused`` check row (PR #222);
- so does ``dataset_schedule.next_due`` while it is later than today in New York (a hold until that day);
- never built (no ``dataset_check`` row), or a last check that ``failed`` or was ``skipped`` (the day cap writes one,
  and so does the pause skip): due;
- ``daily`` (the default, and what an unknown cadence reads as): due once the dataset's own ``default_as_of()`` has
  reached the last check's ``next_check_due`` (the next session after the as-of it ran for), so the SPX slice waits
  for the next complete session and the market series (as-of: today) run on the first pass of each trading day.
  A check with no ``next_check_due`` (past the calendar's table) is due once the as-of is newer than the one it ran
  for, so a calendar run out does not make a dataset due every hour;
- ``weekly`` / ``monthly``: due 7 / 30 days after the last check's ``checked_on``. The cadence is the
  ``dataset_schedule`` row's, else the dataset's own ``cadence`` attribute (the spine's weekly sources), else daily.

The request goes in through ``ops.enqueue_once`` (check and insert in one ``BEGIN IMMEDIATE``), so a dataset that
already has a request queued or running gets no second one, whoever queued the first.
"""
from __future__ import annotations

import sqlite3
from datetime import date

from ...core import calendar
from .. import ops

CADENCES = ("daily", "weekly", "monthly")
DEFAULT_CADENCE = "daily"
CYCLE_DAYS = {"weekly": 7, "monthly": 30}
REQUESTED_BY = "schedule"
ATTENDED_WHY = "attended: run from the CLI on the server (a person logs in; WRDS_USERNAME must be set)"
BY_REQUEST_WHY = "by request only: a board's freeze queues it (PR 10)"
BY_HAND_WHY = "starts by hand: not due until a first run from the CLI or a Fetch has built it"


def is_due(ds) -> tuple[bool, str]:
    """``(due, why)`` for dataset ``ds``, from its ``dataset_schedule`` row and its newest ``dataset_check`` row."""
    if getattr(ds, "attended", False):
        return False, ATTENDED_WHY
    if getattr(ds, "by_request", False):
        return False, BY_REQUEST_WHY
    if getattr(ds, "starts_by_hand", False) and not ops.ever_built(ds.name):
        return False, BY_HAND_WHY
    schedule = ops.schedule_get(ds.name) or {}
    if schedule.get("paused_at") is not None:
        return False, f"paused by {schedule.get('paused_by') or '?'}"
    today = calendar.today_ny().isoformat()
    hold = schedule.get("next_due")
    if hold and str(hold) > today:
        return False, f"held until {hold}"
    check = ops.latest_check(ds.name)
    if check is None:
        return True, "never built"
    if check["result"] in ("failed", "skipped"):
        return True, f"the last check {'failed' if check['result'] == 'failed' else 'was skipped'}"
    cadence = schedule.get("cadence") if schedule.get("cadence") in CADENCES else getattr(ds, "cadence", None)
    cadence = cadence if cadence in CADENCES else DEFAULT_CADENCE
    if cadence == "daily":
        nxt = check.get("next_check_due")
        as_of = ds.default_as_of().isoformat()
        if not nxt:
            # No next session (past the calendar's table, base.next_due): once per newer as-of, never every hour.
            ran = check.get("source_as_of")
            if not ran:
                return True, "the last check names no as-of"
            ran = str(ran)[:10]
            if as_of > ran:
                return True, f"no next session known; as-of {as_of} is newer"
            return False, f"no next session known; ran for {ran}"
        return (True, f"next check {nxt} reached") if as_of >= nxt else (False, f"next check {nxt}")
    age = (calendar.today_ny() - date.fromisoformat(str(check["checked_on"])[:10])).days
    if age >= CYCLE_DAYS[cadence]:
        return True, f"{cadence}: last checked {age} days ago"
    return False, f"{cadence}: last checked {age} days ago"


def enqueue_due(datasets: dict | None = None) -> list[tuple[str, str, int | None, bool]]:
    """Queue every due dataset once; ``(name, why, request id or None, error)`` per dataset, in name order.

    No id: not due, already queued or running, or an error (``why`` says which). ``error`` is True when the due check
    raised or ``ops.sqlite`` stayed locked past its busy timeout; one dataset's error does not stop the others.
    """
    if datasets is None:
        from . import DATASETS as datasets
    out = []
    for name in sorted(datasets):
        try:
            due, why = is_due(datasets[name])
        except Exception as e:  # noqa: BLE001 - a calendar past its table for one dataset must not stop the rest
            out.append((name, f"due check failed: {type(e).__name__}: {e}"[:300], None, True))
            continue
        if not due:
            out.append((name, f"not due ({why})", None, False))
            continue
        try:
            rid = ops.enqueue_once(name, None, by=REQUESTED_BY)
        except sqlite3.OperationalError as e:
            # ops.sqlite held past busy_timeout (as in _run_queue): this dataset waits for the next pass.
            out.append((name, f"due ({why}), not queued: ops.sqlite busy: {e}"[:300], None, True))
            continue
        out.append((name, f"due ({why}), " + (f"queued as request {rid}" if rid else "already queued or running"),
                    rid, False))
    return out
