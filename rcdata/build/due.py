"""What is due: ``run`` without ``--dataset`` queues every due dataset before it drains the queue.

A dataset is due (``is_due``) when nothing holds it and its last run is old enough:

- a pause in ``dataset_schedule`` holds it (the request it would make is not queued; ``base`` skips one queued anyway);
- so does ``dataset_schedule.next_due`` while it is later than today in New York (a hold until that day);
- never built (no ``dataset_check`` row), or a last check that ``failed`` or was ``skipped`` (a day cap, a pause): due;
- ``daily`` (the default, and what an unknown cadence reads as): due once the dataset's own ``default_as_of()`` has
  reached the last check's ``next_check_due`` (the next session after the as-of it ran for), so the SPX slice waits
  for the next complete session and the market series (as-of: today) run on the first pass of each trading day;
- ``weekly`` / ``monthly``: due 7 / 30 days after the last check's ``checked_on``.

The request goes in through ``ops.enqueue_once`` (check and insert in one ``BEGIN IMMEDIATE``), so a dataset that
already has a request queued or running gets no second one, whoever queued the first.
"""
from __future__ import annotations

from datetime import date

from ...core import calendar
from .. import ops

CADENCES = ("daily", "weekly", "monthly")
DEFAULT_CADENCE = "daily"
CYCLE_DAYS = {"weekly": 7, "monthly": 30}
REQUESTED_BY = "schedule"


def is_due(ds) -> tuple[bool, str]:
    """``(due, why)`` for dataset ``ds``, from its ``dataset_schedule`` row and its newest ``dataset_check`` row."""
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
    cadence = schedule.get("cadence") if schedule.get("cadence") in CADENCES else DEFAULT_CADENCE
    if cadence == "daily":
        nxt = check.get("next_check_due")
        if not nxt:
            return True, "the last check names no next day"
        as_of = ds.default_as_of().isoformat()
        return (True, f"next check {nxt} reached") if as_of >= nxt else (False, f"next check {nxt}")
    age = (calendar.today_ny() - date.fromisoformat(str(check["checked_on"])[:10])).days
    if age >= CYCLE_DAYS[cadence]:
        return True, f"{cadence}: last checked {age} days ago"
    return False, f"{cadence}: last checked {age} days ago"


def enqueue_due(datasets: dict | None = None) -> list[tuple[str, str, int | None]]:
    """Queue every due dataset once; ``(name, why, request id or None)`` per dataset, in name order.

    None: not due, already queued or running, or its due check raised (``why`` says which); one dataset's error does
    not stop the others.
    """
    if datasets is None:
        from . import DATASETS as datasets
    out = []
    for name in sorted(datasets):
        try:
            due, why = is_due(datasets[name])
        except Exception as e:  # noqa: BLE001 - a calendar past its table for one dataset must not stop the rest
            out.append((name, f"due check failed: {type(e).__name__}: {e}"[:300], None))
            continue
        if not due:
            out.append((name, f"not due ({why})", None))
            continue
        rid = ops.enqueue_once(name, None, by=REQUESTED_BY)
        out.append((name, f"due ({why}), " + (f"queued as request {rid}" if rid else "already queued or running"),
                    rid))
    return out
