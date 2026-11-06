"""What is due: ``run`` without ``--dataset`` queues every due dataset before it drains the queue.

A dataset is due (``is_due``) when nothing holds it and its last run is old enough:

- an attended dataset (WRDS: a person logs in and confirms Duo) is never due: it runs only from the CLI (PR 5);
  with ``RC_WRDS_UNATTENDED=1`` the WRDS datasets are not attended and follow their cadence, but a WRDS dataset whose
  last run failed is not due again until a person's run succeeds (#344: no hourly login retries);
- a dataset that ``starts_by_hand`` (every spine dataset) is not due until it has built once (a ``changed`` or
  ``unchanged`` check from a CLI run or a Fetch), so the first hourly pass after a deploy does not start it before
  the attended steps, and a first attempt that failed is not retried every hour before a person has seen it work;
- a pause in ``dataset_schedule`` holds it: its request is not queued; ``base.run_request`` skips a request of a
  paused dataset that was queued anyway (a Fetch, the CLI's), with a ``skipped: paused`` check row (PR #222);
- so does ``dataset_schedule.next_due`` while it is later than today in New York (a hold until that day);
- never built (no ``dataset_check`` row), or a last check that ``failed`` or was ``skipped`` (the day cap writes one,
  and so does the pause skip): due; except a failure of a ``DAY_CAPPED`` provider's dataset (Alpha Vantage) that
  holds no raw file for the day yet, due again only on the next New York day (with the day's raw file held a rerun
  costs no call, so a failed build or swap retries on the next pass);
- ``daily`` (the default, and what an unknown cadence reads as): due once the dataset's own ``default_as_of()`` has
  reached the last check's ``next_check_due`` (the next session after the as-of it ran for), so the SPX slice waits
  for the next complete session and the market series (as-of: today) run on the first pass of each trading day.
  A check with no ``next_check_due`` (past the calendar's table) is due once the as-of is newer than the one it ran
  for, so a calendar run out does not make a dataset due every hour;
- its kind's automation (``ops.kind_automation``, set on the Settings page) decides what the hourly pass queues:
  ``off`` (the default) queues nothing for the kind, ``raw`` / ``parquet`` queue the same partial run a Fetch's ▾
  asks for, ``full`` the whole run. Under a partial mode a check the partial run itself wrote (a ``skipped`` with
  its stage note) counts as the run for the cadence, so the hourly pass does not queue the dataset again every
  hour; under ``full`` it stays due, so the next pass builds the rest. A person's Fetch is not touched by any of it;
- ``weekly`` / ``monthly``: due 7 / 30 days after the last check's ``checked_on``. The cadence is the
  ``dataset_schedule`` row's, else the dataset's own ``cadence`` attribute (the spine's weekly sources), else daily.

The request goes in through ``ops.enqueue_once`` (check and insert in one ``BEGIN IMMEDIATE``), so a dataset that
already has a request queued or running gets no second one, whoever queued the first.
"""
from __future__ import annotations

import sys
from datetime import date

from ...core import calendar
from .. import ops
from . import base

CADENCES = ("daily", "weekly", "monthly")
DEFAULT_CADENCE = "daily"
CYCLE_DAYS = {"weekly": 7, "monthly": 30}
REQUESTED_BY = "schedule"
ATTENDED_WHY = "attended: run from the CLI on the server (a person logs in; WRDS_USERNAME must be set)"
WRDS_FAILED_WHY = ("the last WRDS run failed: it waits for a person (no hourly retry, the last snapshot stays); "
                   "a newer run by a person (Fetch or CLI) that does not fail clears it")
#: Providers whose day is too small to retry a failure every hourly pass (Alpha Vantage: 25 calls a day, shared by
#: the earnings calendar and the listing status): a failed check is due again on the next New York day (#349 S6).
DAY_CAPPED = frozenset({"alphavantage"})
BY_HAND_WHY = "starts by hand: not due until a first run from the CLI or a Fetch has built it"
#: The stage notes a partial run leaves (``base.skip_note``), by the modes whose run they complete: a raw + Parquet
#: run also completes a raw-only mode, a raw-only run does not complete a raw + Parquet one.
STAGE_DONE = {"raw": (base.NO_PARQUET_NOTE, base.NO_DUCKDB_NOTE), "parquet": (base.NO_DUCKDB_NOTE,)}


def _stage_done(check: dict, mode: str) -> bool:
    return check["result"] == "skipped" and str(check.get("note") or "").startswith(STAGE_DONE.get(mode, ()))


def _day_raw_held(ds) -> bool:
    """True when ``ds`` already holds its raw file for ``default_as_of()``: a rerun reuses it at no call, so a failed
    build or swap may retry on the next hourly pass (review S6a of #349). A dataset with no ``raw_path`` holds none."""
    raw_path = getattr(sys.modules[type(ds).__module__], "raw_path", None)
    return bool(raw_path and raw_path(ds.default_as_of()).exists())


def is_due(ds, mode: str = "full") -> tuple[bool, str]:
    """``(due, why)`` for dataset ``ds``, from its ``dataset_schedule`` row and its newest ``dataset_check`` row;
    ``mode`` is its kind's automation (``ops.KIND_MODES``)."""
    if getattr(ds, "attended", False):
        return False, ATTENDED_WHY
    if mode == "off":                       # after attended: the Settings row reads "by hand only" first too
        return False, f"automation off for {ds.kind}"
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
    if "wrds" in getattr(ds, "providers", ()) and check["result"] == "failed":
        return False, WRDS_FAILED_WHY           # RC_WRDS_UNATTENDED: one failure never becomes an hourly login
    if check["result"] == "failed" and DAY_CAPPED & set(getattr(ds, "providers", ())) \
            and str(check.get("checked_on"))[:10] >= today and not _day_raw_held(ds):
        return False, "failed today: again on the next New York day (the provider allows 25 calls a day)"
    if check["result"] in ("failed", "skipped") and not _stage_done(check, mode):
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
            mode = ops.kind_mode(datasets[name].kind)
            due, why = is_due(datasets[name], mode)
        except Exception as e:  # noqa: BLE001 - a calendar past its table for one dataset must not stop the rest
            out.append((name, f"due check failed: {type(e).__name__}: {e}"[:300], None, True))
            continue
        if not due:
            out.append((name, f"not due ({why})", None, False))
            continue
        try:
            rid = ops.enqueue_once(name, base.STAGE_PARAMS[mode] or None, by=REQUESTED_BY)
        except ops.BUSY_ERRORS as e:
            # ops.sqlite held past busy_timeout, or Redis not answering (as in _run_queue): this dataset waits for
            # the next pass.
            out.append((name, f"due ({why}), not queued: ops store busy: {e}"[:300], None, True))
            continue
        out.append((name, f"due ({why}), " + (f"queued as request {rid}" if rid else "already queued or running"),
                    rid, False))
    return out
