"""The run log: what ``job_runs`` holds about the jobs this store has seen (``/view/runs``, ``GET /api/runs``).

Two writers fill the table and until now nothing read it (#26 PL5-b): the dashboard's own jobs
(``app/jobs._record``, #282 J4) and the scheduled tasks (``core/automations.run_task``, #129 A1). This module
is the only reader, and it is read-only — no route here starts, retries or deletes anything.

It reports a row as stored rather than as it would like it to be:

* a row written before #83 carries no ``trigger`` or ``source`` (and a dashboard row never has a
  ``registry_name``), so those come back ``None`` instead of being inferred from the lane the row does carry;
* a row whose end write was skipped stays ``running`` until ``app.jobs.reconcile_lost_runs`` closes it as
  ``lost`` (#82: process gone, or older than its bound), so a ``running`` row is listed with its age and not
  quietly re-labelled.

The page explains both in a footnote.

``COLUMNS`` is what the page shows, and nothing else is read: the payload travels on every poll. That leaves
out four columns the table declares. ``pid`` and ``lane`` have writers but no reader here — a stale-run
judgement from ``pid`` belongs to ``reconcile_lost_runs`` (#82). ``counts`` is written by ``app/jobs._record``
only when a runner returns a counts dict (none does today) and ``core/automations.run_task`` passes none, so it
is NULL in practice; this reader should select it in the same change that gives a runner counts.
``registry_name`` repeats ``source`` for every row a scheduled task writes.

Filtering happens in SQL, not on the page. A page-side filter over the newest ``limit`` rows would answer
"when did this last fail?" with "never" as soon as ``limit`` clean runs had happened since the failure.
"""
from __future__ import annotations

import json
import logging

from markupsafe import Markup

from RevenueChaser.core import rawstore, templating
from RevenueChaser.core.html_safe import script_json

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
MAX_QUERY = 100
POLL_PATH = "/api/runs"
# What "Problems only" means: a run that ended badly. A `running` row is not a problem — it may still be
# going; a stale one is closed as ``lost`` by ``app.jobs.reconcile_lost_runs`` (#82), not judged here.
PROBLEM_STATUSES = ("error", "cancelled", "lost")
# Read on every poll, so it holds what the page shows and no more (see the module docstring).
COLUMNS = ("run_id", "module", "source", "trigger", "params",
           "queued_at", "started_at", "finished_at", "status", "error", "message")
_JSON_COLUMNS = ("params",)
# A run that never started sorts on the moment it was queued, so a queued or lost job stays at the top
# where it can be seen, instead of falling to the bottom under a NULL.
_ORDER = "coalesce(started_at, queued_at, 0) DESC"


def clamp_limit(value) -> int:
    """How many rows to read: a bad or missing value falls back, a silly one is capped."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


def clean_query(value) -> str:
    """The name filter as a bounded string; anything unusable is no filter at all."""
    return value.strip()[:MAX_QUERY] if isinstance(value, str) else ""


def _where(problems: bool, query: str) -> tuple[str, list]:
    """The SQL filter both the page of rows and the total count run, so the two always agree.

    ``query`` is matched with ``LIKE`` on the job and the task name, bound as a parameter. ``%`` and ``_``
    in it keep their wildcard meaning, which is useful in a search box and cannot reach the SQL text.
    """
    clauses, params = [], []
    if problems:
        clauses.append(f"status IN ({', '.join(['?'] * len(PROBLEM_STATUSES))})")
        params.extend(PROBLEM_STATUSES)
    if query:
        clauses.append("(lower(coalesce(module, '')) LIKE ? OR lower(coalesce(source, '')) LIKE ?)")
        params += [f"%{query.lower()}%"] * 2
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _shape(row: dict) -> dict:
    """Decode the JSON columns and add the duration the page shows; nothing else is derived."""
    for col in _JSON_COLUMNS:
        value = row.get(col)
        if isinstance(value, str):
            try:
                row[col] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass          # keep the text as it is: the page renders it as text either way
    started, finished = row.get("started_at"), row.get("finished_at")
    # A negative span means the two stamps disagree (a clock step between them), which is not a duration.
    seconds = finished - started if started is not None and finished is not None else None
    row["duration_s"] = seconds if seconds is not None and seconds >= 0 else None
    return row


def read(limit=DEFAULT_LIMIT, problems: bool = False, query=None) -> dict:
    """What ``/api/runs`` returns: the newest runs matching the filter, and how many match in all.

    ``total`` counts the same filter as ``rows``, so "12 of 37" means 12 of the 37 runs that match and never
    12 of every run ever recorded. A missing table or store file reads as no rows (``rawstore.query`` is
    lenient), so the tab opens on a fresh machine instead of erroring.
    """
    n = clamp_limit(limit)
    q = clean_query(query)
    where, params = _where(problems, q)
    rows = [_shape(r) for r in rawstore.query(
        f"SELECT {', '.join(COLUMNS)} FROM job_runs{where} ORDER BY {_ORDER} LIMIT {n}", list(params))]
    total = rawstore.query(f"SELECT count(*) AS n FROM job_runs{where}", list(params))
    return {"rows": rows, "total": total[0]["n"] if total else 0, "shown": len(rows),
            "limit": n, "problems": bool(problems), "query": q}


def page_payload() -> dict:
    """What the Runs page is rendered from: the first page of rows, so the first view needs no poll."""
    return {"initial": read()}


def render_page(payload: dict) -> str:
    # script_json output is the one trusted fragment: Markup keeps autoescape from turning its quotes into
    # entities, which an inline <script> would not decode. Every value in it reaches the page as textContent.
    # The page-size control offers what ``clamp_limit`` accepts: the default page first, then the cap.
    return templating.render("runs.html.j2", limits=(DEFAULT_LIMIT, MAX_LIMIT),
                             initial_json=Markup(script_json(payload.get("initial") or {})))
